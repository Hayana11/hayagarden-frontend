"""P0 cold-storm fix, Part B: durable history-rewrite epoch idempotence.

Covers T12-T17 from the spec:
  T12 first activation advances the epoch exactly once
  T13 effects_pending / replay_only retry does not advance the epoch
  T14 multiple consecutive replay retries still leave exactly one epoch
  T15 a different rewrite advances to a different epoch
  T16 an older rewrite's late retry cannot roll back a newer epoch
  T17 legacy mutation semantics (delete / branch_switch) are unchanged
  T19 marker persist fail → newer rewrite → old retry must not mint a third epoch
  T21 split index persist fail after global epoch → B then A retry must not mint E3
"""
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
from chat.cc_history_rewrite import (
    current_history_rewrite_epoch,
    note_durable_history_rewrite,
)
from chat import cc_history_rewrite as chr


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


class RewriteEpochIdempotenceTests(unittest.TestCase):
    """Real ``invalidate_cc_resident_for_history_rewrite`` (only the loopback
    bridge is stubbed) so ``note_durable_history_rewrite`` / the staging
    ``history_epoch`` marker run exactly as they do in production."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'epoch.db')
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
        self.bridge_calls = []

        def fake_bridge(method, path, body=None, timeout=5):
            self.bridge_calls.append((method, path, body))
            return {'ok': True, 'invalidated': True}

        self.patches = [
            mock.patch.object(app_module, 'DB_PATH', self.db_path),
            mock.patch.object(app_module, 'get_db', self.get_db),
            mock.patch.object(app_module, '_gw_json_request', side_effect=fake_bridge),
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

    def _staging(self, rewrite_id):
        conn = self.get_db()
        row = rw.load(conn, rewrite_id)
        conn.close()
        return row

    def _prep_and_ready_edit(self, msg_id, new_content, candidate):
        prep = self.client.post(
            '/api/chat/edit', json={'msg_id': msg_id, 'content': new_content},
        ).get_json()
        rid = prep['rewrite_id']
        conn = self.get_db()
        rw.store_candidate(conn, rid, content=candidate)
        conn.commit()
        conn.close()
        return rid

    # T12 -----------------------------------------------------------------
    def test_t12_first_activation_advances_epoch_once(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        rid = self._prep_and_ready_edit(u1, "U1'", "A1'")

        before = current_history_rewrite_epoch()
        self.assertEqual(before, '')
        fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin.status_code, 200)

        after = current_history_rewrite_epoch()
        self.assertTrue(after)
        self.assertNotEqual(after, before)
        staging = self._staging(rid)
        self.assertEqual(rw.history_epoch_of(staging), after)

    # T13 -------------------------------------------------------------------
    def test_t13_effects_retry_does_not_advance_epoch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        rid = self._prep_and_ready_edit(u1, "U1'", "A1'")

        with mock.patch.object(
            rw, 'replay_side_effects_after_activate', side_effect=RuntimeError('boom'),
        ):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin1.status_code, 200)
        self.assertTrue(fin1.get_json().get('effects_pending'))
        epoch_after_first = current_history_rewrite_epoch()
        self.assertTrue(epoch_after_first)
        staging_after_first = self._staging(rid)
        self.assertEqual(rw.history_epoch_of(staging_after_first), epoch_after_first)
        bridge_calls_after_first = len(self.bridge_calls)

        fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
        self.assertEqual(fin2.status_code, 200)
        self.assertFalse(fin2.get_json().get('effects_pending'))

        self.assertEqual(current_history_rewrite_epoch(), epoch_after_first)
        staging_after_second = self._staging(rid)
        self.assertEqual(rw.history_epoch_of(staging_after_second), epoch_after_first)
        # The retry must not re-invoke the eager-kill bridge either.
        self.assertEqual(len(self.bridge_calls), bridge_calls_after_first)

    # T14 -------------------------------------------------------------------
    def test_t14_multiple_replay_retries_leave_one_epoch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        rid = self._prep_and_ready_edit(u1, "U1'", "A1'")

        wake_n = {'n': 0}

        def flaky_wake(_get_db, ids):
            wake_n['n'] += 1
            if wake_n['n'] < 3:
                raise RuntimeError('wake db down')
            return len(ids or [])

        seen_epochs = []
        with mock.patch('chat.context_continuity.consume_wake_ids', side_effect=flaky_wake):
            for _ in range(3):
                fin = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid})
                self.assertEqual(fin.status_code, 200)
                seen_epochs.append(current_history_rewrite_epoch())

        self.assertEqual(len(set(seen_epochs)), 1)
        staging = self._staging(rid)
        self.assertEqual(rw.history_epoch_of(staging), seen_epochs[0])
        self.assertEqual(staging['status'], rw.STATUS_EFFECTS_DONE)

    # T15 -------------------------------------------------------------------
    def test_t15_different_rewrite_advances_to_different_epoch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')

        rid1 = self._prep_and_ready_edit(u1, "U1'", "A1'")
        fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1.status_code, 200)
        e1 = current_history_rewrite_epoch()

        # Editing U1 archives/deletes everything at or after it; insert U2 only
        # *after* that so its own edit's prepare fingerprint is against the
        # live post-edit tip (mirrors normal sequential usage).
        u2 = self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        rid2 = self._prep_and_ready_edit(u2, "U2'", "A2'")
        fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid2})
        self.assertEqual(fin2.status_code, 200)
        e2 = current_history_rewrite_epoch()

        self.assertNotEqual(e1, e2)
        self.assertEqual(rw.history_epoch_of(self._staging(rid1)), e1)
        self.assertEqual(rw.history_epoch_of(self._staging(rid2)), e2)

    # T16 (hard test) ---------------------------------------------------
    def test_t16_old_retry_cannot_roll_back_newer_epoch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        u2 = self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')

        rid1 = self._prep_and_ready_edit(u1, "U1'", "A1'")
        with mock.patch.object(
            rw, 'replay_side_effects_after_activate', side_effect=RuntimeError('boom'),
        ):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1.status_code, 200)
        self.assertTrue(fin1.get_json().get('effects_pending'))
        e1 = current_history_rewrite_epoch()
        self.assertTrue(e1)

        # A newer, independent rewrite commits and advances the epoch further.
        # (edit U2 lives after U1' tail-wise once id-order is preserved; use a
        # fresh insert to keep this rewrite's prepare fingerprint valid.)
        u3 = self._insert('hayana', 'U3')
        rid2 = self._prep_and_ready_edit(u3, "U3'", "A3'")
        fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid2})
        self.assertEqual(fin2.status_code, 200)
        e2 = current_history_rewrite_epoch()
        self.assertTrue(e2)
        self.assertNotEqual(e1, e2)

        # Old rewrite's effects retry must not roll the global epoch back to e1.
        fin1_retry = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1_retry.status_code, 200)
        self.assertFalse(fin1_retry.get_json().get('effects_pending'))
        self.assertEqual(current_history_rewrite_epoch(), e2)
        # The old rewrite's own marker stays whatever it captured on first
        # activation — it is never rewritten to the newer epoch either.
        self.assertEqual(rw.history_epoch_of(self._staging(rid1)), e1)

    # T17 -------------------------------------------------------------------
    def test_t17_legacy_mutations_still_advance_epoch_every_call(self):
        self._insert('hayana', 'U1')
        a1 = self._insert('assistant', 'A1')
        response = self.client.post('/api/chat/delete', json={'msg_id': a1})
        self.assertEqual(response.status_code, 200)
        e1 = current_history_rewrite_epoch()
        self.assertTrue(e1)

        u2 = self._insert('hayana', 'U2')
        a2 = self._insert(
            'assistant', "A2'",
            branches=json.dumps([{'content': 'A2'}, {'content': "A2'"}]),
            branch_idx=1,
        )
        switched = self.client.post(
            '/api/chat/branch/switch', json={'msg_id': a2, 'direction': -1},
        )
        self.assertEqual(switched.status_code, 200)
        e2 = current_history_rewrite_epoch()
        self.assertTrue(e2)
        self.assertNotEqual(e1, e2)

        # A second delete is again a genuinely new mutation -> new epoch again.
        response2 = self.client.post('/api/chat/delete', json={'msg_id': u2})
        self.assertEqual(response2.status_code, 200)
        e3 = current_history_rewrite_epoch()
        self.assertNotEqual(e3, e2)

    # T19 (hard test) ---------------------------------------------------
    def test_t19_marker_persist_fail_then_newer_rewrite_retry_no_third_epoch(self):
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')

        rid1 = self._prep_and_ready_edit(u1, "U1'", "A1'")
        real_persist = rw.persist_history_epoch_if_absent
        persist_calls = {'n': 0}

        def fail_first_persist(conn, rewrite_id, epoch):
            persist_calls['n'] += 1
            if persist_calls['n'] == 1:
                return False
            return real_persist(conn, rewrite_id, epoch)

        with mock.patch.object(rw, 'persist_history_epoch_if_absent', side_effect=fail_first_persist):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1.status_code, 200)
        self.assertTrue(fin1.get_json().get('effects_pending'))
        e1 = current_history_rewrite_epoch()
        self.assertTrue(e1)
        self.assertEqual(rw.history_epoch_of(self._staging(rid1)), '')

        u2 = self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        rid2 = self._prep_and_ready_edit(u2, "U2'", "A2'")
        fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid2})
        self.assertEqual(fin2.status_code, 200)
        e2 = current_history_rewrite_epoch()
        self.assertTrue(e2)
        self.assertNotEqual(e1, e2)
        bridge_calls_after_b = len(self.bridge_calls)

        fin1_retry = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1_retry.status_code, 200)
        self.assertFalse(fin1_retry.get_json().get('effects_pending'))
        self.assertEqual(current_history_rewrite_epoch(), e2)
        self.assertEqual(rw.history_epoch_of(self._staging(rid1)), e1)
        self.assertEqual(len(self.bridge_calls), bridge_calls_after_b)

    # T21 (hard test) ---------------------------------------------------
    def test_t21_split_index_persist_fail_no_third_epoch(self):
        """Simulate the pre-unified crash: global epoch E1/key=A committed but the
        idempotency index entry for A was never persisted. B must still advance
        to E2, and A's retry must reuse E1 — never mint E3."""
        u1 = self._insert('hayana', 'U1')
        self._insert('assistant', 'A1')
        rid1 = self._prep_and_ready_edit(u1, "U1'", "A1'")
        key_a = 'rewrite:%s' % rid1
        minted = {}

        def split_write_without_index(state):
            minted['epoch'] = str(state.get('epoch') or '')
            minted['reason'] = state.get('reason')
            minted['ts'] = state.get('ts')
            # Old dual-file failure mode: global epoch lands, index does not.
            with open(self.epoch_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'epoch': minted['epoch'],
                    'reason': minted['reason'],
                    'idempotency_key': key_a,
                    'ts': minted['ts'],
                }, handle)

        with mock.patch.object(chr, '_atomic_write_durable_state', side_effect=split_write_without_index):
            fin1 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1.status_code, 200)
        e1 = minted.get('epoch') or current_history_rewrite_epoch()
        self.assertTrue(e1)

        u2 = self._insert('hayana', 'U2')
        self._insert('assistant', 'A2')
        rid2 = self._prep_and_ready_edit(u2, "U2'", "A2'")
        fin2 = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid2})
        self.assertEqual(fin2.status_code, 200)
        e2 = current_history_rewrite_epoch()
        self.assertTrue(e2)
        self.assertNotEqual(e1, e2)
        bridge_calls_after_b = len(self.bridge_calls)

        fin1_retry = self.client.post('/api/chat/edit/finalize', json={'rewrite_id': rid1})
        self.assertEqual(fin1_retry.status_code, 200)
        self.assertEqual(current_history_rewrite_epoch(), e2)
        self.assertEqual(rw.history_epoch_of(self._staging(rid1)), e1)
        self.assertEqual(len(self.bridge_calls), bridge_calls_after_b)

        # Unified state must now contain both keys atomically.
        with open(self.epoch_path, encoding='utf-8') as handle:
            durable = json.load(handle)
        self.assertEqual(durable['epoch'], e2)
        self.assertEqual(durable['idempotency_index'][key_a]['epoch'], e1)
        self.assertEqual(durable['idempotency_index']['rewrite:%s' % rid2]['epoch'], e2)


if __name__ == '__main__':
    unittest.main()
