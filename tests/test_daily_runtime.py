"""P-CONTEXT-DAILY-SOFT-WINDOW-R1 resident integration tests — no model calls."""
from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_store
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.daily_context import ConflictError, DeferredError


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


def _insert(db_path: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)',
        (author, content, created_at),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)


class _FakeResident:
    generation = 1

    def __init__(self):
        self._alive = False
        self._cold = True
        self.last_state_snapshot = {'mood': 'calm'}
        self.sent: list[str] = []
        self.killed = 0

    def ensure_alive(self, system_text, env):
        self._alive = True
        cold = self._cold
        self._cold = False
        return cold

    def _alive_fn(self):
        return self._alive

    def _kill(self, quiet=True):
        self._alive = False
        self.killed += 1

    def send_turn(self, content, commit_meta=None):
        self.sent.append(str(content))
        yield ('text', 'daily reply')
        yield ('done', ('daily reply', '', {'input_tokens': 3, 'output_tokens': 5}, {}))


class _ToolResident(_FakeResident):
    def send_turn(self, content, commit_meta=None):
        yield ('tool_use', {'id': 't1', 'name': 'mcp__home__light_on', 'args': {}})
        yield ('done', ('', '', {}, {}))


class _FailingResident(_FakeResident):
    def send_turn(self, content, commit_meta=None):
        self.sent.append(str(content))
        raise RuntimeError('provider exploded')


class DailyRuntimeFlagOffTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_prepare_rejects_when_disabled(self):
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            with self.assertRaises(dr.DailyRuntimeError):
                dr.prepare_daily_turn(user_message_id=1)

    def test_flag_off_no_daily_module_side_effects(self):
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(dc.enabled())
        dr.reset_bindings_for_tests()
        self.assertIsNone(dr._BINDING_KEY)

    def test_flag_off_no_daily_rows_on_legacy_path(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            with mock.patch.object(config_store, 'get_bool', return_value=False):
                conn = sqlite3.connect(db)
                n = conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0]
                conn.close()
                self.assertEqual(n, 0)
        finally:
            os.unlink(db)


class DailyRuntimeLeaseTests(unittest.TestCase):
    def test_single_lease_under_concurrency(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='lease', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])
            owners = []

            def worker(i):
                try:
                    dc.acquire_resident_turn_lease(
                        cid, gen, lease_owner='o%d' % i, request_message_id=100 + i, db_path=db,
                    )
                    return 'ok', i
                except ConflictError:
                    return 'conflict', i

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = [f.result() for f in [pool.submit(worker, i) for i in range(8)]]
            ok = [r for r in results if r[0] == 'ok']
            self.assertEqual(len(ok), 1)
            owners.append(ok[0][1])
            dc.release_resident_turn_lease(cid, gen, lease_owner='o%d' % owners[0], db_path=db)
            self.assertFalse(dc.is_resident_turn_active(cid, gen, db_path=db))
        finally:
            os.unlink(db)

    def test_owner_mismatch_release(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='rel', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='owner-a', request_message_id=1, db_path=db,
            )
            self.assertFalse(
                dc.release_resident_turn_lease(cid, gen, lease_owner='owner-b', db_path=db),
            )
            self.assertTrue(dc.is_resident_turn_active(cid, gen, db_path=db))
            self.assertTrue(
                dc.release_resident_turn_lease(cid, gen, lease_owner='owner-a', db_path=db),
            )
        finally:
            os.unlink(db)

    def test_expired_lease_takeover(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='exp', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])
            past = datetime.datetime(2026, 7, 27, 9, 0, 0)
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='old', request_message_id=1,
                ttl_seconds=1, db_path=db, now=past,
            )
            future = past + datetime.timedelta(seconds=10)
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='new', request_message_id=2,
                db_path=db, now=future,
            )
            self.assertTrue(dc.is_resident_turn_active(cid, gen, db_path=db, now=future))
        finally:
            os.unlink(db)


class DailyRuntimeTurnTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def _prepare(self, db, uid, *, resident=None, alive=False):
        def alive_fn(key):
            return alive and dr._BINDING_KEY == key
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('STATE', 'snapshot', {'k': 'v'})):
            return dr.prepare_daily_turn(
                user_message_id=uid,
                db_path=db,
                now=_FIXED_NOW,
                resident_alive_fn=alive_fn,
                resident=resident,
                static_system='STATIC',
            )

    def test_cold_turn_cursor_after_assistant(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'hello', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            self.assertTrue(plan.is_cold)
            self.assertEqual(plan.manifest.get('turn_kind'), 'cold')
            resident = _FakeResident()
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC',
            ))
            self.assertTrue(any(e[0] == 'done' for e in events))
            aid = _insert(db, 'assistant', 'reply', '2026-07-27 10:00:01')
            out = dr.complete_daily_turn(plan, assistant_message_id=aid)
            self.assertEqual(out['cursor_after'], aid)
            self.assertTrue(out['lease_released'])
            self.assertEqual(
                dc.get_resident_history_cursor(plan.context_id, plan.resident_generation, db_path=db),
                aid,
            )
        finally:
            os.unlink(db)

    def test_hot_turn_replays_only_after_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            m1 = _insert(db, 'hayana', 'one', '2026-07-27 09:00:00')
            a1 = _insert(db, 'assistant', 'r1', '2026-07-27 09:00:01')
            uid = _insert(db, 'hayana', 'two', '2026-07-27 10:00:00')
            plan1 = self._prepare(db, m1)
            dr.complete_daily_turn(plan1, assistant_message_id=a1)
            dr.bind_resident_key(plan1.resident_key)
            plan2 = self._prepare(db, uid, alive=True)
            self.assertEqual(plan2.manifest.get('turn_kind'), 'hot')
            hist_ids = [m['message_id'] for m in plan2.assembly.get('current_day_history') or []]
            self.assertNotIn(uid, hist_ids)
            self.assertTrue(all(i > a1 for i in hist_ids) or not hist_ids)
        finally:
            os.unlink(db)

    def test_respawn_increments_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            gen0 = plan.resident_generation
            dr.abort_daily_turn(plan, error_code='provider_failure')
            refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=db)
            self.assertEqual(int(refreshed['resident_generation']), gen0 + 1)
        finally:
            os.unlink(db)

    def test_tool_call_fail_closed(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'tool?', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _ToolResident()
            with self.assertRaises(dr.DailyWindowToolFencePending):
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
            dr.abort_daily_turn(
                plan, error_code='DailyWindowToolFencePending', close_resident_obj=resident,
            )
            self.assertFalse(dc.is_resident_turn_active(
                plan.context_id, plan.resident_generation, db_path=db,
            ))
        finally:
            os.unlink(db)

    def test_provider_failure_respawns_without_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'fail', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _FailingResident()
            with self.assertRaises(RuntimeError):
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
            dr.handle_provider_failure(plan, error_code='provider_failure', resident=resident)
            self.assertIsNone(
                dc.get_resident_history_cursor(plan.context_id, plan.resident_generation, db_path=db),
            )
            self.assertGreater(resident.killed, 0)
        finally:
            os.unlink(db)

    def test_empty_response_no_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'empty', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            with self.assertRaises(dr.DailyRuntimeError):
                dr.handle_provider_success(plan, assistant_message_id=99, raw_text='   ')
            self.assertIsNone(
                dc.get_resident_history_cursor(plan.context_id, plan.resident_generation, db_path=db),
            )
        finally:
            os.unlink(db)

    def test_cursor_cas_conflict_respawns(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'cas', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            aid = _insert(db, 'assistant', 'ok', '2026-07-27 10:00:01')
            dc.advance_resident_history_cursor(
                plan.context_id, plan.resident_generation, aid, db_path=db,
            )
            plan.cursor_before = None
            out = dr.complete_daily_turn(plan, assistant_message_id=aid + 1)
            self.assertFalse(out.get('cursor_cas_success', True))
            refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=db)
            self.assertGreater(int(refreshed['resident_generation']), plan.resident_generation)
        finally:
            os.unlink(db)


class DailyRuntimeRolloverTests(unittest.TestCase):
    def test_0359_vs_0400_chat_day(self):
        ts1 = datetime.datetime(2026, 7, 27, 3, 59, 59)
        ts2 = datetime.datetime(2026, 7, 27, 4, 0, 0)
        self.assertEqual(dc.chat_day_for_timestamp(ts1), '2026-07-26')
        self.assertEqual(dc.chat_day_for_timestamp(ts2), '2026-07-27')

    def test_rollover_deferred_when_old_lease_active(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='roll', local_day='2026-07-26', db_path=db,
                now=datetime.datetime(2026, 7, 26, 12, 0, 0),
            )
            dc.acquire_resident_turn_lease(
                int(old_ctx['id']), int(old_ctx['resident_generation']),
                lease_owner='busy', request_message_id=1, db_path=db,
                now=datetime.datetime(2026, 7, 27, 3, 59, 0),
            )
            uid = _insert(db, 'hayana', 'new day', '2026-07-27 04:00:01')
            with mock.patch.object(config_store, 'get_bool', return_value=True):
                with self.assertRaises(DeferredError):
                    dr.prepare_daily_turn(
                        user_message_id=uid,
                        chat_id='roll',
                        db_path=db,
                        now=datetime.datetime(2026, 7, 27, 4, 0, 1),
                    )
        finally:
            os.unlink(db)


class DailyRuntimeRegressionTests(unittest.TestCase):
    def test_clean_window_shadow_unchanged(self):
        from chat import clean_window_shadow as cws
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(cws.enabled())

    def test_resident_key_format(self):
        key = dr.make_resident_key(chat_id='default', context_epoch=2, resident_generation=3)
        self.assertEqual(key, 'daily:default:2:3')


if __name__ == '__main__':
    unittest.main()
