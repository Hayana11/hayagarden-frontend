"""Activation-atomic regen/edit: old active transcript survives until success."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
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

    def test_10_edit_finalize_rejects_when_active_grew(self):
        """prepare → another normal turn → finalize must 409 and keep later msgs."""
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        # Concurrent normal turn advances the active tip.
        u3 = self._insert('hayana', 'U3')
        a3 = self._insert('assistant', 'A3')
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'")
        conn.commit()
        conn.close()
        before = self._active()
        fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 409)
        self.assertEqual(fin.get_json().get('code'), 'stale_rewrite')
        self.assertEqual(self._active(), before)
        self.assertIn(('hayana', 'U3'), self._active())
        self.assertIn(('assistant', 'A3'), self._active())
        conn = self.get_db()
        st = rw.load(conn, rid)
        live_ids = [r[0] for r in rw.active_transcript(conn)]
        conn.close()
        self.assertEqual(st['status'], rw.STATUS_STALE)
        self.assertIn(u3, live_ids)
        self.assertIn(a3, live_ids)
        self.assertEqual(self.invalidation_calls, [])

    def test_11_regen_finalize_rejects_after_branch_switch(self):
        """prepare regen → branch switch → finalize must 409, keep new selection."""
        self._insert('hayana', 'U1')
        a1 = self._insert(
            'assistant', 'A1',
            branches=json.dumps([
                {'content': 'A1', 'thinking': '', 'tool_calls': ''},
                {'content': "A1-alt", 'thinking': '', 'tool_calls': ''},
            ]),
            branch_idx=0,
        )
        prep = self.client.post('/api/chat/regen/prepare', json={'msg_id': a1}).get_json()
        rid = prep['rewrite_id']
        switched = self.client.post(
            '/api/chat/branch/switch', json={'msg_id': a1, 'direction': 1},
        )
        self.assertEqual(switched.status_code, 200)
        self.assertEqual(switched.get_json()['branch_idx'], 1)
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1-stale-candidate")
        conn.commit()
        conn.close()
        fin = self.client.post('/api/chat/regen/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 409)
        self.assertEqual(fin.get_json().get('code'), 'stale_rewrite')
        snap = self._snap_assistant(a1)
        self.assertEqual(snap['content'], 'A1-alt')
        self.assertEqual(snap['branch_idx'], 1)
        self.assertNotEqual(snap['content'], 'A1-stale-candidate')
        conn = self.get_db()
        st = rw.load(conn, rid)
        conn.close()
        self.assertEqual(st['status'], rw.STATUS_STALE)

    def test_12_source_aware_prefix_keeps_history_before_distant_edit(self):
        """Edit far behind tip: prefix fetch must keep id < source, not tip window."""
        ids = []
        for i in range(1, 21):
            ids.append(self._insert('hayana', f'U{i}'))
            ids.append(self._insert('assistant', f'A{i}'))
        # Edit U2 (early). Tip window of 5 would miss everything before source.
        source_id = ids[2]  # U2 is 3rd insert? Wait: U1,A1,U2,... → index 2 is U2
        self.assertEqual(source_id, 3)
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': source_id, 'content': "U2'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        staging = rw.load(conn, rid)
        conn.close()

        def get_db():
            return self.get_db()

        rows, available = rw.fetch_rewrite_prefix_rows(
            get_db,
            source_id=int(staging['source_message_id']),
            fetch_limit=5,
            history_where='1=1',
        )
        overlay = rw.apply_history_overlay(rows, staging)
        contents = [r['content'] for r in overlay]
        # Tip-window bug would fetch U18..A20 then filter id<3 → only U2'.
        # Source-aware prefix keeps the real pre-fork window (U1/A1) + edit.
        self.assertEqual(available, 2)
        self.assertEqual(contents, ['U1', 'A1', "U2'"])
        self.assertNotIn('U20', contents)
        self.assertNotIn('A20', contents)

    def test_13_edit_activate_write_fence_keeps_concurrent_send(self):
        """Concurrent /api/chat/send during assert→DELETE must not be deleted.

        BEGIN IMMEDIATE holds the write lock across fingerprint + mutate, so a
        racing INSERT blocks until activate commits, then lands *after* the
        rewrite and survives.
        """
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'")
        conn.commit()
        conn.close()

        started = threading.Event()
        finished = threading.Event()
        inserted = {}

        def race_hook(_conn):
            def other():
                started.set()
                c2 = self.get_db()
                try:
                    cur = c2.execute(
                        "INSERT INTO chat_messages (author, content) VALUES ('hayana', 'RACE')"
                    )
                    c2.commit()
                    inserted['id'] = int(cur.lastrowid)
                    inserted['ok'] = True
                except Exception as exc:
                    inserted['error'] = str(exc)
                finally:
                    c2.close()
                    finished.set()

            t = threading.Thread(target=other, daemon=True)
            t.start()
            self.assertTrue(started.wait(2.0))
            # Other thread should be blocked on the IMMEDIATE write lock.
            time.sleep(0.25)
            self.assertFalse(finished.is_set())

        prev_hook = rw._activation_fence_hook
        rw._activation_fence_hook = race_hook
        try:
            fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        finally:
            rw._activation_fence_hook = prev_hook
        self.assertEqual(fin.status_code, 200)
        self.assertTrue(finished.wait(5.0))
        self.assertTrue(inserted.get('ok'))
        self.assertIn('id', inserted)
        active = self._active()
        self.assertEqual(active[0], ('hayana', "U1'"))
        self.assertEqual(active[1], ('assistant', "A1'"))
        self.assertIn(('hayana', 'RACE'), active)

    def test_14_finalize_replays_frozen_side_effects(self):
        """Claims frozen at candidate READY are consumed only after activate."""
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        effects = {
            'wake_ids': [11, 12],
            'one_shot_claims': {'feedback_ids': [3], 'dream_id': 7},
            'session_memo_user': "U1'",
            'session_memo_assistant': "A1'",
            'turn_key': 'turn-abc',
            'conversation_id': 'hayana-chat',
        }
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", side_effects=effects)
        conn.commit()
        staged = rw.load(conn, rid)
        conn.close()
        self.assertEqual(rw.side_effects_of(staged)['wake_ids'], [11, 12])

        wake_calls = []
        oneshot_calls = []
        memo_calls = []
        moments_calls = []
        with mock.patch(
            'chat.context_continuity.consume_wake_ids',
            side_effect=lambda get_db, ids: wake_calls.append(list(ids)),
        ), mock.patch(
            'chat.system_builder.consume_cc_one_shot_claims',
            side_effect=lambda get_db, claims, strict=False: oneshot_calls.append(dict(claims)),
        ), mock.patch.object(
            rw, '_write_session_memo_best_effort',
            side_effect=lambda u, a: memo_calls.append((u, a)),
        ), mock.patch(
            'moments_persistence.after_assistant_persisted',
            side_effect=lambda **kw: moments_calls.append(kw),
        ), mock.patch(
            'emotion_engine.score_async',
        ):
            fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 200)
        self.assertEqual(wake_calls, [[11, 12]])
        self.assertEqual(oneshot_calls, [{'feedback_ids': [3], 'dream_id': 7}])
        self.assertEqual(memo_calls, [("U1'", "A1'")])
        self.assertEqual(len(moments_calls), 1)
        self.assertEqual(moments_calls[0]['turn_data']['turn_key'], 'turn-abc')
        self.assertEqual(
            moments_calls[0]['assistant_message_id'],
            fin.get_json()['assistant_message_id'],
        )
        conn = self.get_db()
        st = rw.load(conn, rid)
        conn.close()
        self.assertEqual(st['status'], rw.STATUS_EFFECTS_DONE)

    def test_15_finalize_resume_replays_without_remutating(self):
        """activated_needs_replay → retry finalize only replays, never DELETE again."""
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        effects = {
            'wake_ids': [42],
            'one_shot_claims': {'feedback_ids': [9], 'dream_id': None},
            'session_memo_user': "U1'",
            'session_memo_assistant': "A1'",
            'turn_key': 'turn-resume',
            'conversation_id': 'hayana-chat',
        }
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", side_effects=effects)
        conn.commit()
        conn.close()

        # First finalize: mutate transcript, then simulate replay crash before effects_done.
        with mock.patch.object(
            rw, 'replay_side_effects_after_activate', side_effect=RuntimeError('boom'),
        ), mock.patch('emotion_engine.score_async'):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin1.status_code, 200)
        body1 = fin1.get_json()
        self.assertTrue(body1.get('effects_pending'))
        self.assertEqual(body1.get('code'), 'effects_pending')
        after_activate = self._active()
        self.assertEqual(after_activate, [('hayana', "U1'"), ('assistant', "A1'")])
        conn = self.get_db()
        st1 = rw.load(conn, rid)
        conn.close()
        self.assertEqual(st1['status'], rw.STATUS_ACTIVATED_NEEDS_REPLAY)

        # Concurrent tip growth after activate must not be wiped by resume.
        race_id = self._insert('hayana', 'AFTER')
        wake_calls = []
        with mock.patch(
            'chat.context_continuity.consume_wake_ids',
            side_effect=lambda get_db, ids: wake_calls.append(list(ids)),
        ), mock.patch(
            'chat.system_builder.consume_cc_one_shot_claims',
        ), mock.patch.object(
            rw, '_write_session_memo_best_effort',
        ), mock.patch(
            'moments_persistence.after_assistant_persisted',
        ), mock.patch(
            'emotion_engine.score_async',
        ):
            fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin2.status_code, 200)
        self.assertFalse(fin2.get_json().get('effects_pending'))
        self.assertEqual(wake_calls, [[42]])
        # Resume must not re-DELETE: AFTER tip survives.
        self.assertEqual(
            self._active(),
            [('hayana', "U1'"), ('assistant', "A1'"), ('hayana', 'AFTER')],
        )
        self.assertIn(race_id, [r[0] for r in rw.active_transcript(self.get_db())])
        conn = self.get_db()
        st2 = rw.load(conn, rid)
        conn.close()
        self.assertEqual(st2['status'], rw.STATUS_EFFECTS_DONE)
        # Third call is idempotent done.
        fin3 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin3.status_code, 200)
        self.assertFalse(fin3.get_json().get('effects_pending'))
        self.assertEqual(
            self._active(),
            [('hayana', "U1'"), ('assistant', "A1'"), ('hayana', 'AFTER')],
        )

    def test_16_epoch_ensured_on_replay_only_after_first_invalidate_fails(self):
        """If epoch fails after activation commit, resume must still note epoch."""
        from chat.cc_history_rewrite import (
            current_history_rewrite_epoch,
            note_durable_history_rewrite,
        )

        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", side_effects={
            'wake_ids': [],
            'one_shot_claims': {},
            'session_memo_user': "U1'",
            'session_memo_assistant': "A1'",
            'turn_key': '',
            'conversation_id': 'hayana-chat',
        })
        conn.commit()
        conn.close()

        inv_calls = []

        def flaky_invalidate(reason):
            inv_calls.append(reason)
            if len(inv_calls) == 1:
                raise RuntimeError('epoch write failed')
            note_durable_history_rewrite(reason)
            return True

        with mock.patch.object(
            app_module, 'invalidate_cc_resident_for_history_rewrite',
            side_effect=flaky_invalidate,
        ), mock.patch('emotion_engine.score_async'):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin1.status_code, 200)
        self.assertTrue(fin1.get_json().get('effects_pending'))
        self.assertEqual(self._active(), [('hayana', "U1'"), ('assistant', "A1'")])
        self.assertEqual(current_history_rewrite_epoch(), '')
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_ACTIVATED_NEEDS_REPLAY)
        conn.close()

        with mock.patch.object(
            app_module, 'invalidate_cc_resident_for_history_rewrite',
            side_effect=flaky_invalidate,
        ), mock.patch('emotion_engine.score_async'):
            fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin2.status_code, 200)
        self.assertFalse(fin2.get_json().get('effects_pending'))
        self.assertTrue(current_history_rewrite_epoch())
        self.assertEqual(len(inv_calls), 2)
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_EFFECTS_DONE)
        conn.close()

    def test_17_wake_consume_failure_blocks_effects_done(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", side_effects={
            'wake_ids': [7],
            'one_shot_claims': {},
            'turn_key': '',
        })
        conn.commit()
        conn.close()

        wake_n = {'n': 0}

        def flaky_wake(_get_db, ids):
            wake_n['n'] += 1
            if wake_n['n'] == 1:
                raise RuntimeError('wake db down')
            return len(ids or [])

        with mock.patch(
            'chat.context_continuity.consume_wake_ids', side_effect=flaky_wake,
        ), mock.patch('emotion_engine.score_async'):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertTrue(fin1.get_json().get('effects_pending'))
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_ACTIVATED_NEEDS_REPLAY)
        conn.close()

        with mock.patch(
            'chat.context_continuity.consume_wake_ids', side_effect=flaky_wake,
        ), mock.patch('emotion_engine.score_async'):
            fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertFalse(fin2.get_json().get('effects_pending'))
        self.assertEqual(wake_n['n'], 2)
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_EFFECTS_DONE)
        conn.close()

    def test_18_feedback_consume_failure_blocks_effects_done(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': u1, 'content': "U1'"},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content="A1'", side_effects={
            'wake_ids': [],
            'one_shot_claims': {'feedback_ids': [3], 'dream_id': None},
            'turn_key': '',
        })
        conn.commit()
        conn.close()

        fb_n = {'n': 0}

        def flaky_feedback(ids):
            fb_n['n'] += 1
            if fb_n['n'] == 1:
                raise RuntimeError('feedback db down')
            return len(ids or [])

        with mock.patch(
            'command_store.consume_feedback', side_effect=flaky_feedback,
        ), mock.patch('emotion_engine.score_async'):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertTrue(fin1.get_json().get('effects_pending'))
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_ACTIVATED_NEEDS_REPLAY)
        conn.close()

        with mock.patch(
            'command_store.consume_feedback', side_effect=flaky_feedback,
        ), mock.patch('emotion_engine.score_async'):
            fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertFalse(fin2.get_json().get('effects_pending'))
        self.assertEqual(fb_n['n'], 2)
        conn = self.get_db()
        self.assertEqual(rw.load(conn, rid)['status'], rw.STATUS_EFFECTS_DONE)
        conn.close()


class RelayStagedPromptSideEffectTests(unittest.TestCase):
    def test_build_system_allow_side_effects_false_does_not_drain_feedback(self):
        from chat.system_builder import build_system
        with mock.patch('gateway.get_db') as get_db, mock.patch(
            'command_store.drain_feedback',
        ) as drain, mock.patch(
            'command_store.peek_feedback', return_value=(['x'], [1]),
        ):
            conn = mock.MagicMock()
            conn.execute.return_value.fetchall.return_value = []
            conn.execute.return_value.fetchone.return_value = None
            get_db.return_value = conn
            build_system(allow_side_effects=False, split_dynamic=True)
        drain.assert_not_called()


if __name__ == '__main__':
    unittest.main()
