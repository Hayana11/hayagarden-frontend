"""P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0 unit tests — no model calls."""
from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import daily_context as dc
from chat import daily_history as dh


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
            content TEXT NOT NULL,
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


def _insert(db_path: str, author: str, content: str, created_at: str, tool_calls: str = '') -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, tool_calls, created_at) VALUES (?,?,?,?)',
        (author, content, tool_calls, created_at),
    )
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return int(mid)


class ChatDayBoundaryTests(unittest.TestCase):
    def test_0359_previous_day(self):
        ts = datetime.datetime(2026, 7, 27, 3, 59, 59)
        self.assertEqual(dc.chat_day_for_timestamp(ts), '2026-07-26')

    def test_0400_new_day(self):
        ts = datetime.datetime(2026, 7, 27, 4, 0, 0)
        self.assertEqual(dc.chat_day_for_timestamp(ts), '2026-07-27')

    def test_half_open_window(self):
        day, start, end, next_start = dc.chat_day_window('2026-07-26')
        self.assertEqual(start, '2026-07-26 04:00:00')
        self.assertEqual(end, '2026-07-27 03:59:59')
        self.assertEqual(next_start, '2026-07-27 04:00:00')

    def test_boundary_zero_when_no_messages(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            bid = dc.get_boundary_message_id(conn, local_day='2026-07-27')
            conn.close()
            self.assertEqual(bid, 0)
        finally:
            os.unlink(db)

    def test_boundary_last_before_0400(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            a = _insert(db, 'hayana', 'early', '2026-07-27 03:50:00')
            _insert(db, 'hayana', 'after', '2026-07-27 04:10:00')
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            bid = dc.get_boundary_message_id(conn, local_day='2026-07-27')
            conn.close()
            self.assertEqual(bid, a)
        finally:
            os.unlink(db)

    def test_timezone_validation(self):
        self.assertEqual(dc.validate_timezone('Asia/Shanghai'), 'Asia/Shanghai')
        with self.assertRaises(ValueError):
            dc.validate_timezone('UTC')


class ConcurrentIdempotentTests(unittest.TestCase):
    def test_repeat_get_or_create_same_row(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            a = dc.get_or_create_daily_context(
                chat_id='c1', local_day='2026-07-27', db_path=db,
            )
            b = dc.get_or_create_daily_context(
                chat_id='c1', local_day='2026-07-27', db_path=db,
            )
            self.assertEqual(a['id'], b['id'])
            self.assertEqual(a['context_epoch'], b['context_epoch'])
            conn = sqlite3.connect(db)
            n = conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0]
            conn.close()
            self.assertEqual(n, 1)
        finally:
            os.unlink(db)

    def test_concurrent_create_single_row(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            results = []

            def worker():
                results.append(dc.get_or_create_daily_context(
                    chat_id='race', local_day='2026-07-27', db_path=db,
                ))

            with ThreadPoolExecutor(max_workers=8) as pool:
                futs = [pool.submit(worker) for _ in range(8)]
                for f in futs:
                    f.result()
            ids = {r['id'] for r in results}
            epochs = {r['context_epoch'] for r in results}
            self.assertEqual(len(ids), 1)
            self.assertEqual(len(epochs), 1)
            conn = sqlite3.connect(db)
            n = conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0]
            conn.close()
            self.assertEqual(n, 1)
        finally:
            os.unlink(db)

    def test_epoch_monotonic(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            d1 = dc.get_or_create_daily_context(chat_id='e', local_day='2026-07-26', db_path=db)
            d2 = dc.get_or_create_daily_context(chat_id='e', local_day='2026-07-27', db_path=db)
            self.assertEqual(d1['context_epoch'] + 1, d2['context_epoch'])
        finally:
            os.unlink(db)

    def test_illegal_transition_rejected(self):
        with self.assertRaises(dc.InvalidTransitionError):
            dc.assert_transition(dc.STATUS_FINALIZED, dc.STATUS_PROVISIONAL)
        with self.assertRaises(dc.InvalidTransitionError):
            dc.assert_transition(dc.STATUS_ABSENT, dc.STATUS_FINALIZED)

    def test_lease_timeout_recoverable(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='l', local_day='2026-07-27', db_path=db)
            # Force ABSENT-like path: set status to FAILED then acquire.
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE daily_contexts SET status=?, lease_owner='old', "
                "lease_expires_at='2020-01-01 00:00:00' WHERE id=?",
                (dc.STATUS_FAILED_RETRYABLE, ctx['id']),
            )
            conn.commit()
            conn.close()
            got = dc.acquire_compaction_lease(int(ctx['id']), 'new', db_path=db)
            self.assertEqual(got['status'], dc.STATUS_COMPACTING)
            self.assertEqual(got['lease_owner'], 'new')
        finally:
            os.unlink(db)

    def test_provider_busy_deferred(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            with self.assertRaises(dc.DeferredError):
                dc.get_or_create_daily_context(
                    chat_id='b', local_day='2026-07-27', db_path=db, provider_busy=True,
                )
        finally:
            os.unlink(db)

    def test_compaction_failure_still_provisional(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='f', local_day='2026-07-27', db_path=db)
            conn = sqlite3.connect(db)
            conn.execute(
                'UPDATE daily_contexts SET status=? WHERE id=?',
                (dc.STATUS_COMPACTING, ctx['id']),
            )
            conn.commit()
            conn.close()
            failed = dc.release_compaction_to_provisional(
                int(ctx['id']), failed=True, db_path=db,
            )
            self.assertEqual(failed['status'], dc.STATUS_FAILED_RETRYABLE)
            recovered = dc.release_compaction_to_provisional(
                int(ctx['id']), failed=False, db_path=db,
            )
            self.assertEqual(recovered['status'], dc.STATUS_PROVISIONAL)
        finally:
            os.unlink(db)


class CarryoverTests(unittest.TestCase):
    def _seed_prev_day(self, db: str) -> list[int]:
        # Prev chat day 2026-07-26 → window [07-26 04:00, 07-27 04:00)
        ids = []
        ids.append(_insert(db, 'hayana', 'u1', '2026-07-26 10:00:00'))
        ids.append(_insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00'))
        ids.append(_insert(db, 'system', 'sys', '2026-07-26 10:02:00'))
        ids.append(_insert(db, 'hayana', 'u2 [[SAVE]]', '2026-07-26 10:03:00'))
        ids.append(_insert(db, 'hayana', '【唤醒】wake', '2026-07-26 10:04:00'))
        ids.append(_insert(db, 'hayana', 'u3', '2026-07-26 10:05:00'))
        ids.append(_insert(db, 'fyodor', 'a2', '2026-07-26 10:06:00'))
        ids.append(_insert(db, 'hayana', 'u4', '2026-07-26 10:07:00'))
        ids.append(_insert(db, 'fyodor', 'a3', '2026-07-26 10:08:00'))
        ids.append(_insert(db, 'hayana', 'u5', '2026-07-26 10:09:00'))
        # Boundary for 2026-07-27 is last id before 04:00 — insert one more before boundary.
        return ids

    def test_only_allowed_counts(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = dc.get_or_create_daily_context(chat_id='c', local_day='2026-07-27', db_path=db)
            with self.assertRaises(ValueError):
                dc.select_carryover(int(ctx['id']), 4, db_path=db)
        finally:
            os.unlink(db)

    def test_select_last_n_excludes_system_save_wake(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = dc.get_or_create_daily_context(chat_id='c', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            contents = [c['content_preview'] for c in cands]
            self.assertNotIn('sys', contents)
            self.assertTrue(all('SAVE' not in c for c in contents))
            self.assertTrue(all('唤醒' not in c for c in contents))
            result = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(result['carryover_count'], 3)
            msgs = dc.get_selected_carryover_messages(int(ctx['id']), db_path=db)
            self.assertEqual(len(msgs), 3)
            self.assertEqual([m['message_id'] for m in msgs], result['selected_message_ids'])
            roles = [m['role'] for m in msgs]
            self.assertTrue(all(r in ('user', 'assistant') for r in roles))
            # Last three eligible should be a2, u4, a3, u5 → last 3 = u4,a3,u5 or a2,u4,a3 depending on filter.
            # Eligible: u1,a1,u3,a2,u4,a3,u5 → last 3 = u4,a3,u5
            self.assertEqual([m['content'] for m in msgs], ['u4', 'a3', 'u5'])
        finally:
            os.unlink(db)

    def test_zero_finalizes(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='z', local_day='2026-07-27', db_path=db)
            result = dc.finalize_zero_carryover(int(ctx['id']), db_path=db)
            self.assertEqual(result['carryover_count'], 0)
            refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=db)
            self.assertEqual(refreshed['status'], dc.STATUS_FINALIZED)
            self.assertTrue(refreshed['selection_finalized_at'])
        finally:
            os.unlink(db)

    def test_locked_after_finalize(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = dc.get_or_create_daily_context(chat_id='k', local_day='2026-07-27', db_path=db)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 5, db_path=db)
        finally:
            os.unlink(db)

    def test_first_user_message_auto_zero(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = dc.get_or_create_daily_context(chat_id='a', local_day='2026-07-27', db_path=db)
            _insert(db, 'hayana', 'first today', '2026-07-27 09:00:00')
            # Locked by first user message presence.
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 3, db_path=db)

            # Separate day: auto-finalize while still provisional / unlocked.
            ctx2 = dc.get_or_create_daily_context(chat_id='a2', local_day='2026-07-28', db_path=db)
            out = dc.maybe_auto_finalize_zero_on_first_user_message(int(ctx2['id']), db_path=db)
            self.assertIsNotNone(out)
            self.assertEqual(out['carryover_count'], 0)
        finally:
            os.unlink(db)

    def test_shortfall_returns_all_eligible(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'only', '2026-07-26 12:00:00')
            _insert(db, 'fyodor', 'two', '2026-07-26 12:01:00')
            ctx = dc.get_or_create_daily_context(chat_id='s', local_day='2026-07-27', db_path=db)
            result = dc.select_carryover(int(ctx['id']), 10, db_path=db)
            self.assertEqual(result['carryover_count'], 2)
        finally:
            os.unlink(db)


class HistoryAssemblyTests(unittest.TestCase):
    def test_excludes_unselected_pre_boundary(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            old1 = _insert(db, 'hayana', 'old-unselected', '2026-07-26 11:00:00')
            for i in range(5):
                _insert(db, 'hayana', 'mid-%d' % i, '2026-07-26 11:%02d:00' % (i + 1))
            old_last = _insert(db, 'fyodor', 'old-selected-tail', '2026-07-26 11:10:00')
            ctx = dc.get_or_create_daily_context(chat_id='h', local_day='2026-07-27', db_path=db)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            today = _insert(db, 'hayana', 'today', '2026-07-27 10:00:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='h',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='STATIC',
                    is_cold=True,
                    db_path=db,
                )
            carry_ids = built['manifest']['carryover_message_ids']
            self.assertNotIn(old1, carry_ids)
            self.assertIn(old_last, carry_ids)
            self.assertEqual(len(carry_ids), 3)
            hist_ids = [m['message_id'] for m in built['current_day_history']]
            self.assertIn(today, hist_ids)
            self.assertNotIn(old1, hist_ids)
            self.assertFalse(built['manifest']['legacy_cold_once_injected'])
            self.assertFalse(built['manifest']['auto_recall_injected'])
            self.assertFalse(built['manifest']['relationship_context_injected'])
            self.assertFalse(built['manifest']['pre_boundary_history_injected'])
            self.assertFalse(built['manifest']['diary_summary_injected'])
        finally:
            os.unlink(db)

    def test_hot_turn_skips_handoff_and_carryover(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'x', '2026-07-26 11:00:00')
            _insert(db, 'fyodor', 'y', '2026-07-26 11:01:00')
            _insert(db, 'hayana', 'z', '2026-07-26 11:02:00')
            ctx = dc.get_or_create_daily_context(chat_id='hot', local_day='2026-07-27', db_path=db)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            with mock.patch('chat.daily_history._build_state_text', return_value=('S', 'delta', {'k': 'v'})):
                built = dh.build_daily_window_context(
                    chat_id='hot',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='STATIC',
                    is_cold=False,
                    db_path=db,
                )
            self.assertFalse(built['manifest']['handoff_injected_this_turn'])
            self.assertFalse(built['manifest']['carryover_injected_this_turn'])
            self.assertEqual(built['carryover_messages'], [])
        finally:
            os.unlink(db)

    def test_missing_handoff_static_only_no_fallback(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='m', local_day='2026-07-27', db_path=db)
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='m',
                    daily_context=ctx,
                    static_system='STATIC_ONLY',
                    is_cold=True,
                    db_path=db,
                )
            self.assertEqual(built['static'], 'STATIC_ONLY')
            self.assertEqual(built['day_handoff'], '')
            self.assertEqual(built['manifest']['handoff_status'], dc.HANDOFF_ABSENT)
            self.assertFalse(built['manifest']['legacy_cold_once_injected'])
        finally:
            os.unlink(db)


class ResidentFenceTests(unittest.TestCase):
    def test_respawn_bumps_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='r', local_day='2026-07-27', db_path=db)
            self.assertEqual(int(ctx['resident_generation']), 1)
            nxt = dc.respawn_daily_resident(int(ctx['id']), db_path=db)
            self.assertEqual(int(nxt['resident_generation']), 2)
            # New day gets new epoch, generation starts at 1.
            d2 = dc.get_or_create_daily_context(chat_id='r', local_day='2026-07-28', db_path=db)
            self.assertEqual(int(d2['resident_generation']), 1)
            self.assertGreater(int(d2['context_epoch']), int(ctx['context_epoch']))
        finally:
            os.unlink(db)

    def test_epoch_fence_blocks_stale_write(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='f', local_day='2026-07-27', db_path=db)
            token = dc.make_epoch_token(
                chat_id='f',
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )
            self.assertTrue(dc.is_epoch_current(token, db_path=db))
            dc.respawn_daily_resident(int(ctx['id']), db_path=db)
            self.assertFalse(dc.is_epoch_current(token, db_path=db))
            called = []
            ok, _ = dc.commit_if_epoch_current(token, lambda: called.append(1), db_path=db)
            self.assertFalse(ok)
            self.assertEqual(called, [])
        finally:
            os.unlink(db)

    def test_morning_greeting_cas(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='g', local_day='2026-07-27', db_path=db)
            dc.set_morning_greeting_message_id(int(ctx['id']), 99, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.set_morning_greeting_message_id(int(ctx['id']), 100, db_path=db)
        finally:
            os.unlink(db)


class HandoffStorageTests(unittest.TestCase):
    def test_store_and_validate(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            content = {
                'source_day': '2026-07-26',
                'source_epoch': 1,
                'boundary_message_id': 10,
                'topics': ['休息'],
                'confirmed_facts': ['用户表示会好好吃早饭'],
                'decisions': [],
                'open_loops': [],
                'explicit_user_requests': [],
                'last_topic': '用户询问休息安排',
            }
            sha = hashlib.sha256(b'fixture').hexdigest()
            row = dc.store_day_handoff(
                chat_id='h',
                source_day='2026-07-26',
                content=content,
                boundary_message_id=10,
                source_first_message_id=1,
                source_last_message_id=10,
                source_message_count=10,
                source_sha256=sha,
                db_path=db,
            )
            self.assertEqual(row['status'], dc.HANDOFF_READY)
            # Reject assistant voice style.
            bad = dict(content)
            bad['last_topic'] = '我轻轻把她揽进怀里'
            with self.assertRaises(ValueError):
                dc.store_day_handoff(
                    chat_id='h',
                    source_day='2026-07-26',
                    content=bad,
                    boundary_message_id=10,
                    source_first_message_id=1,
                    source_last_message_id=10,
                    source_message_count=10,
                    source_sha256=hashlib.sha256(b'bad').hexdigest(),
                    db_path=db,
                )
        finally:
            os.unlink(db)


class FlagAndRegressionGuardTests(unittest.TestCase):
    def test_default_flag_off(self):
        import config_store
        self.assertEqual(config_store._DEFAULTS.get('DAILY_SOFT_WINDOW_ENABLED'), '0')
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertFalse(dc.enabled())

    def test_clean_window_default_unchanged(self):
        import config_store
        self.assertEqual(config_store._DEFAULTS.get('CC_CLEAN_WINDOW_SHADOW_ENABLED'), '0')


class ApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        _insert(self.db, 'hayana', 'hello', '2026-07-26 12:00:00')
        self.app = None

    def tearDown(self):
        os.unlink(self.db)

    def test_routes_disabled_by_default(self):
        from daily_context_routes import daily_context_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(daily_context_bp)
        client = app.test_client()
        with mock.patch('chat.daily_context.enabled', return_value=False):
            r = client.get('/api/daily-context/current')
            self.assertEqual(r.status_code, 404)

    def test_select_carryover_ok(self):
        from daily_context_routes import daily_context_bp
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(daily_context_bp)
        client = app.test_client()
        with mock.patch('chat.daily_context.enabled', return_value=True):
            with mock.patch('daily_context_routes._load_token', return_value='tok'):
                with mock.patch('daily_context_routes._db_path_override', return_value=self.db):
                    r = client.post(
                        '/api/daily-context/select-carryover',
                        json={'count': 0, 'chat_id': 'api'},
                        headers={'Authorization': 'Bearer tok'},
                    )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['carryover_count'], 0)


if __name__ == '__main__':
    unittest.main()
