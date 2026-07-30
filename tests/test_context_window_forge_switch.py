"""Focused contract tests for seamless Forge context-window switch (step5)."""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import context_window as cw
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.context_window_forge import (
    CarryoverUnforgeableError,
    build_events_from_selected_messages,
    forge_target_session_from_db,
)
from tools.claude_forge_core import load_jsonl, sha256_file
from tools.claude_forge_validator import validate_forged_transcript


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            content TEXT,
            thinking TEXT,
            tool_calls TEXT,
            cache_info TEXT,
            choices TEXT,
            image_url TEXT,
            created_at TEXT,
            source_kind TEXT
        )'''
    )
    conn.commit()
    conn.close()


def _insert(db: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at, source_kind) VALUES (?,?,?,?)',
        (author, content, created_at, 'chat'),
    )
    mid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return mid


def _map(db: str, context_id: int, context_epoch: int, message_id: int, role: str) -> None:
    dc.record_daily_message_context(
        message_id,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=1,
        role=role,
        db_path=db,
    )


def _seed_rounds(db: str, ctx: dict, n: int) -> list[int]:
    ids: list[int] = []
    base = datetime.datetime(2026, 7, 27, 10, 0, 0)
    for i in range(n):
        ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
        ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
        u = _insert(db, 'hayana', 'user-%d' % i, ts_u)
        a = _insert(db, 'fyodor', 'asst-%d' % i, ts_a)
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), a, 'assistant')
        ids.extend([u, a])
    return ids


class ForgeSwitchContractTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-switch-')
        self.hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        dr.reset_bindings_for_tests()
        # Flag on for prepare_daily_turn gate tests.
        self._flag_patch = mock.patch(
            'chat.daily_context.enabled', return_value=True,
        )
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_A_reserve_blocks_prepare_daily_turn(self):
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        self.assertEqual(intent['status'], cw.INTENT_RESERVED)
        u = _insert(self.db, 'hayana', 'blocked', '2026-07-27 11:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError) as ar:
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        self.assertEqual(ar.exception.error_code, 'switch_in_progress')

    def test_B_db_forge_counts(self):
        _seed_rounds(self.db, self.ctx, 12)
        for count in (0, 3, 5, 10):
            with self.subTest(count=count):
                messages = []
                conn = dc._connect(self.db)
                try:
                    # Replicate reserve selection without leaving active intents.
                    from chat.context_window import (
                        _collect_context_formal_messages,
                        _select_rounds_locked,
                    )
                    rows = _collect_context_formal_messages(
                        conn,
                        context_id=int(self.ctx['id']),
                        context_epoch=int(self.ctx['context_epoch']),
                    )
                    _rounds, selected, _rc = _select_rounds_locked(rows, count)
                    if count == 0:
                        self.assertEqual(selected, [])
                    else:
                        self.assertEqual(len(selected), count * 2)
                    forged = forge_target_session_from_db(
                        conn,
                        selected_message_ids=selected,
                        cwd=self.hooks.forge_cwd,
                        claude_home=self.hooks.claude_home,
                    )
                finally:
                    conn.close()
                events = load_jsonl(forged.jsonl_path)
                vr = validate_forged_transcript(
                    events, session_id=forged.target_session_id,
                )
                self.assertTrue(vr.ok, vr.errors)
                if count == 0:
                    self.assertEqual(len(events), 2)
                    self.assertIn('context-window-boundary', events[0]['message']['content'])
                else:
                    self.assertEqual(len(events), count * 2)
                    for evt in events:
                        self.assertIn(evt['type'], ('user', 'assistant'))
                        self.assertNotIn('usage', evt.get('message') or {})

    def test_C_staged_health_jsonl_unchanged(self):
        req = str(uuid.uuid4())
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        path = Path(self.hooks.claude_home) / 'projects'
        # find jsonl
        files = list(Path(self.hooks.claude_home).rglob('*.jsonl') )
        self.assertTrue(files)
        digest = sha256_file(files[0])
        # offline prepare again must keep bytes
        intent = {
            'target_session_id': out['claude_session_id'],
            'target_jsonl_sha256': digest,
        }
        staged = self.hooks.prepare_staged(intent, files[0])
        self.assertEqual(sha256_file(files[0]), digest)
        self.hooks.discard_staged(staged)

    def test_D_forge_failure_keeps_source(self):
        # Force unforgeable by locking a nonexistent message id via direct intent forge path
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        # Corrupt selected ids
        conn = dc._connect(self.db)
        try:
            conn.execute(
                'UPDATE context_switch_intents SET selected_message_ids_json=? WHERE request_id=?',
                (json.dumps([999999]), req),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(cw.CarryoverMessageUnforgeableError):
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
        open_ctx = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(open_ctx['id']), int(self.ctx['id']))
        self.assertIsNone(open_ctx.get('closed_at'))

    def test_D_staged_failure_keeps_source_and_old_resident(self):
        killed = {'n': 0}

        class BoomHooks(cw.SwitchHooks):
            pass

        def prepare_staged(intent, forge_path):
            raise cw.SwitchFailedError('staged_exited_during_health_window')

        def take_handoff(staged, result):
            return None

        def discard_staged(staged):
            killed['n'] += 1

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=self.hooks.forge_cwd,
            claude_home=self.hooks.claude_home,
        )
        old = mock.MagicMock()
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=dc.make_resident_key(
                chat_id='default',
                context_epoch=int(self.ctx['context_epoch']),
                resident_generation=1,
            ),
            context_id=int(self.ctx['id']),
            context_epoch=int(self.ctx['context_epoch']),
            resident_generation=1,
            bound_cursor_message_id=None,
            process_generation=1,
            tool_profile='daily',
        ))
        with self.assertRaises(cw.SwitchFailedError):
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=str(uuid.uuid4()),
                db_path=self.db,
                hooks=hooks,
            )
        open_ctx = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(open_ctx['id']), int(self.ctx['id']))
        self.assertIsNotNone(dr.get_local_binding())
        old._kill.assert_not_called()

    def test_E_handoff_pending_blocks_turns(self):
        req = str(uuid.uuid4())
        # Manually move to handoff_pending after a successful forge/ready/commit
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        # Force status back to handoff_pending to simulate crash before committed mark
        conn = dc._connect(self.db)
        try:
            conn.execute(
                'UPDATE context_switch_intents SET status=? WHERE request_id=?',
                (cw.INTENT_HANDOFF_PENDING, req),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(cw.has_active_switch_intent(db_path=self.db))
        u = _insert(self.db, 'hayana', 'race', '2026-07-27 12:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError):
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        # recover
        recovered = cw.complete_handoff_pending_recovery(
            request_id=req, hooks=self.hooks, db_path=self.db,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered['target_context_id'], out['target_context_id'])
        conn = dc._connect(self.db)
        try:
            st = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?', (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st, cw.INTENT_COMMITTED)

    def test_G_committed_required_for_first_turn(self):
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        for status in (
            cw.INTENT_RESERVED,
            cw.INTENT_FORGING,
            cw.INTENT_READY,
            cw.INTENT_COMMITTING,
            cw.INTENT_HANDOFF_PENDING,
        ):
            conn = dc._connect(self.db)
            try:
                conn.execute(
                    'UPDATE context_switch_intents SET status=? WHERE request_id=?',
                    (status, req),
                )
                conn.commit()
            finally:
                conn.close()
            u = _insert(self.db, 'hayana', 'x-%s' % status, '2026-07-27 13:00:00')
            with self.assertRaises(dr.SwitchInProgressRuntimeError):
                dr.prepare_daily_turn(user_message_id=u, db_path=self.db)

    def test_H_same_request_does_not_reforge(self):
        _seed_rounds(self.db, self.ctx, 3)
        req = str(uuid.uuid4())
        forge_calls = {'n': 0}
        import chat.context_window_forge as forge_mod
        real_forge = forge_mod.forge_target_session_from_db

        def counting_forge(*args, **kwargs):
            forge_calls['n'] += 1
            return real_forge(*args, **kwargs)

        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting_forge):
            out1 = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
            n_after_first = forge_calls['n']
            out2 = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
        self.assertEqual(out1['target_context_id'], out2['target_context_id'])
        self.assertEqual(forge_calls['n'], n_after_first)
        self.assertGreaterEqual(n_after_first, 1)

    def test_I_idempotency_mismatch(self):
        req = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        # New open context is target; mismatch on count against committed intent
        with self.assertRaises(cw.IdempotencyMismatchError):
            cw.reserve_or_load_intent(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
            )


class FrontendRequestIdTests(unittest.TestCase):
    def test_controller_reuses_request_id_source(self):
        # Source-level assertion: pendingRequestId field exists in controller source.
        src = Path(ROOT) / 'app/src/lib/manualContextWindowController.ts'
        text = src.read_text(encoding='utf-8')
        self.assertIn('pendingRequestId', text)
        self.assertIn('if (!this.pendingRequestId)', text)
        self.assertIn('crypto.randomUUID()', text)


class UnforgeableOrderTests(unittest.TestCase):
    def test_trailing_user_unforgeable(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        u = _insert(db, 'hayana', 'only-user', '2026-07-27 10:00:00')
        conn = dc._connect(db)
        try:
            with self.assertRaises(CarryoverUnforgeableError):
                build_events_from_selected_messages(
                    conn,
                    selected_message_ids=[u],
                    target_session_id=str(uuid.uuid4()),
                    cwd='/tmp',
                )
        finally:
            conn.close()
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
