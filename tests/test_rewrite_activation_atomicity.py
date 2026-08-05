"""Activation-atomic regen/edit: old active transcript survives until success."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from chat import rewrite_staging as rw


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        '''
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL DEFAULT 'user',
            content TEXT NOT NULL,
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            branches TEXT DEFAULT '',
            branch_idx INTEGER DEFAULT 0,
            cache_info TEXT DEFAULT '',
            choices TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            file_url TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE chat_edit_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fork_msg_id INTEGER,
            original_content TEXT,
            messages_json TEXT
        );
        '''
    )
    rw.ensure_schema(conn)
    conn.commit()


class RewriteActivationAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'atomic.db')
        self.epoch_path = str(Path(self.tmp.name) / 'epoch')
        self.lock_path = str(Path(self.tmp.name) / 'lock')
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        _schema(conn)
        conn.close()

        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        self.get_db = get_db
        self.invalidation_calls = []

        def fake_invalidate(reason):
            self.invalidation_calls.append(reason)
            from chat.cc_history_rewrite import note_durable_history_rewrite
            note_durable_history_rewrite(reason)
            return True

        self.patches = [
            mock.patch.object(app_module, 'DB_PATH', self.db_path),
            mock.patch.object(app_module, 'get_db', self.get_db),
            mock.patch.object(
                app_module, 'invalidate_cc_resident_for_history_rewrite',
                side_effect=fake_invalidate,
            ),
            mock.patch.dict(os.environ, {
                'CC_HISTORY_REWRITE_EPOCH_PATH': self.epoch_path,
                'CC_HISTORY_REWRITE_LOCK_PATH': self.lock_path,
            }, clear=False),
        ]
        for p in self.patches:
            p.start()
        self.client = app_module.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _insert(self, author, content, **extra):
        conn = self.get_db()
        cur = conn.execute(
            'INSERT INTO chat_messages (author, content, thinking, tool_calls, branches, branch_idx) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (
                author, content, extra.get('thinking', ''), extra.get('tool_calls', ''),
                extra.get('branches', ''), extra.get('branch_idx', 0),
            ),
        )
        conn.commit()
        row_id = int(cur.lastrowid)
        conn.close()
        return row_id

    def _active(self):
        conn = self.get_db()
        rows = rw.active_transcript(conn)
        conn.close()
        return [(a, c) for _i, a, c in rows]

    def _snap_assistant(self, mid):
        conn = self.get_db()
        row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()
        conn.close()
        return dict(row)

    def test_1_regen_provider_failure_preserves_old_answer(self):
        u1 = self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1', thinking='t1', tool_calls='[]')
        before = self._snap_assistant(a1)
        prep = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1}).get_json()
        self.assertTrue(prep['ok'])
        rid = prep['rewrite_id']
        # Simulate stream failure: mark failed, never finalize.
        conn = self.get_db()
        rw.mark_failed(conn, rid, 'provider error')
        conn.commit()
        conn.close()
        fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 400)
        self.assertEqual(self._active(), [('hayana', 'U1'), ('assistant', 'A1')])
        after = self._snap_assistant(a1)
        for key in ('id', 'content', 'thinking', 'tool_calls', 'branch_idx', 'branches'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(u1, 1)

    def test_2_regen_success_activates_new_variant(self):
        self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        prep = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1}).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", thinking='t2')
        conn.commit()
        conn.close()
        # Candidate must not appear in active transcript.
        self.assertEqual(self._active(), [('hayana', 'U1'), ('assistant', 'A1')])
        fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 200)
        body = fin.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['branch_idx'], 1)
        self.assertEqual(body['total'], 2)
        self.assertEqual(self._active(), [('hayana', 'U1'), ('assistant', "A1'")])
        snap = self._snap_assistant(a1)
        branches = json.loads(snap['branches'])
        self.assertEqual([b['content'] for b in branches], ['A1', "A1'"])
        self.assertEqual(snap['branch_idx'], 1)
        self.assertIn('regen_finalize', self.invalidation_calls)

    def test_3_regen_prepare_without_generation_is_harmless(self):
        self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        before = self._active()
        before_row = self._snap_assistant(a1)
        resp = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._active(), before)
        self.assertEqual(self._snap_assistant(a1), before_row)
        self.assertEqual(self.invalidation_calls, [])

    def test_4_edit_provider_failure_preserves_entire_old_tail(self):
        u1 = self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        u2 = self._insert('hayana', 'U2')
        a2 = self._insert('assistant', 'A2')
        before = self._active()
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.mark_failed(conn, rid, 'stream failed')
        conn.commit()
        conn.close()
        fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 400)
        self.assertEqual(self._active(), before)
        self.assertEqual(
            [r[0] for r in rw.active_transcript(self.get_db())],
            [u1, a1, u2, a2],
        )

    def test_5_edit_success_atomically_activates_new_branch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        self.assertEqual(
            self._active(),
            [('hayana', 'U1'), ('assistant', 'A1'), ('hayana', 'U2'), ('assistant', 'A2')],
        )
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'")
        conn.commit()
        conn.close()
        fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 200)
        self.assertEqual(self._active(), [('hayana', "U1'"), ('assistant', "A1'")])
        conn = self.get_db()
        arch = conn.execute(
            'SELECT fork_msg_id, original_content, messages_json FROM chat_edit_branches'
        ).fetchone()
        conn.close()
        self.assertEqual(arch['fork_msg_id'], u1)
        self.assertEqual(arch['original_content'], 'U1')
        archived = json.loads(arch['messages_json'])
        self.assertEqual(
            [(r['author'], r['content']) for r in archived],
            [('hayana', 'U1'), ('assistant', 'A1'), ('hayana', 'U2'), ('assistant', 'A2')],
        )
        self.assertIn('edit', self.invalidation_calls)

    def test_6_edit_prepare_client_abandonment(self):
        self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        before = self._active()
        resp = self.client.post(
            '/api/chat/edit', json={'msg_id': 1, 'content': "U1'"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._active(), before)
        self.assertEqual(self.invalidation_calls, [])

    def test_7_assistant_persist_failure_keeps_old_active(self):
        self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        prep = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1}).get_json()
        rid = prep['rewrite_id']
        # Persist failure = never store_candidate; finalize must refuse.
        fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 400)
        self.assertEqual(self._active(), [('hayana', 'U1'), ('assistant', 'A1')])

    def test_8_finalize_failure_no_half_switch(self):
        self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        prep = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1}).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'")
        conn.commit()
        conn.close()
        with mock.patch.object(rw, 'activate_regen', side_effect=RuntimeError('boom')):
            fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 500)
        self.assertEqual(self._active(), [('hayana', 'U1'), ('assistant', 'A1')])
        conn = self.get_db()
        st = rw.load(conn, rid)
        conn.close()
        self.assertEqual(st['status'], rw.STATUS_READY)

    def test_9_normal_chat_unaffected(self):
        # Ordinary send path still inserts user rows; no rewrite staging required.
        resp = self.client.post(
            '/api/chat/send',
            json={'author': 'hayana', 'content': 'hello normal'},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get('ok'))
        mid = body.get('message_id')
        self.assertIsNotNone(mid)
        self.assertEqual(self._active(), [('hayana', 'hello normal')])
        conn = self.get_db()
        staged = conn.execute('SELECT COUNT(*) AS c FROM chat_rewrite_staging').fetchone()['c']
        conn.close()
        self.assertEqual(staged, 0)

    def test_history_overlay_regen_and_edit(self):
        rows = [
            {'id': 1, 'author': 'hayana', 'content': 'U1'},
            {'id': 2, 'author': 'assistant', 'content': 'A1'},
            {'id': 3, 'author': 'hayana', 'content': 'U2'},
        ]
        regen = {
            'operation': rw.OP_REGEN,
            'source_message_id': 2,
        }
        self.assertEqual(
            [r['content'] for r in rw.apply_history_overlay(rows, regen)],
            ['U1'],
        )
        edit = {
            'operation': rw.OP_EDIT,
            'source_message_id': 1,
            'edited_content': "U1'",
            'source_snapshot_json': json.dumps({'author': 'hayana'}),
        }
        out = rw.apply_history_overlay(rows, edit)
        self.assertEqual([(r['author'], r['content']) for r in out], [('hayana', "U1'")])


if __name__ == '__main__':
    unittest.main()
