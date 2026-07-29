"""P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0 unit tests — no model calls."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
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
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS wake_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thoughts TEXT, action TEXT, content TEXT, consumed INTEGER,
            woke_at TEXT, cache_info TEXT, wake_run_id TEXT
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


def _insert(
    db_path: str,
    author: str,
    content: str,
    created_at: str,
    *,
    tool_calls: str = '',
    source_kind: str = 'chat',
    image_url: str = '',
) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, tool_calls, source_kind, image_url, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (author, content, tool_calls, source_kind, image_url, created_at),
    )
    conn.commit()
    mid = cur.lastrowid
    conn.close()
    return int(mid)


_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)

# Pin chat-day for calls that omit explicit now= (CI may run on a later calendar day).
_real_current_chat_day = dc._current_chat_day


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


def _ctx(db: str, day: str = '2026-07-27', *, chat_id: str = 'default', **kwargs):
    return dc.get_or_create_daily_context(
        chat_id=chat_id,
        local_day=day,
        db_path=db,
        now=kwargs.pop('now', _FIXED_NOW),
        **kwargs,
    )


def _seed_handoff_contexts(db: str, source: str = '2026-07-26', target: str = '2026-07-27'):
    _insert(db, 'hayana', 'pre-boundary', '2026-07-26 20:00:00')
    _ctx(db, source, allow_backfill=True)
    _ctx(db, target)


def _cold_then_hot_build(db, ctx, *, state_mock=('', 'none', {}), **kwargs):
    ctx_id = int(ctx['id'])
    gen = int(ctx.get('resident_generation') or 1)
    with mock.patch('chat.daily_history._build_state_text', return_value=state_mock):
        cold = dh.build_daily_window_context(
            chat_id=str(ctx.get('chat_id') or 'default'),
            daily_context=ctx,
            static_system=kwargs.get('static_system', 'S'),
            is_cold=True,
            db_path=db,
            **{k: v for k, v in kwargs.items() if k != 'static_system'},
        )
        replayed = int(cold['manifest'].get('replayed_through_message_id') or 0)
        if cold['manifest'].get('cursor_advance_required'):
            advance_to = replayed
            if advance_to <= 0:
                advance_to = int(ctx.get('boundary_message_id') or 0)
            if advance_to > 0 or cold['current_day_history']:
                dc.advance_resident_history_cursor(
                    ctx_id, gen, max(advance_to, replayed), db_path=db,
                )
        refreshed = dc.get_daily_context_by_id(ctx_id, db_path=db) or ctx
        return dh.build_daily_window_context(
            chat_id=str(refreshed.get('chat_id') or 'default'),
            daily_context=refreshed,
            static_system=kwargs.get('static_system', 'S'),
            is_cold=False,
            db_path=db,
            **{k: v for k, v in kwargs.items() if k != 'static_system'},
        )


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


class ConcurrentIdempotentTests(unittest.TestCase):
    def test_repeat_get_or_create_same_row(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            a = dc.get_or_create_daily_context(chat_id='c1', local_day='2026-07-27', db_path=db)
            b = dc.get_or_create_daily_context(chat_id='c1', local_day='2026-07-27', db_path=db)
            self.assertEqual(a['id'], b['id'])
            self.assertEqual(a['status'], dc.STATUS_PROVISIONAL)
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
                for f in [pool.submit(worker) for _ in range(8)]:
                    f.result()
            self.assertEqual(len({r['id'] for r in results}), 1)
        finally:
            os.unlink(db)

    def test_normal_get_or_create_can_enter_compaction(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            # Leave ABSENT for compaction path.
            ctx = dc.get_or_create_daily_context(
                chat_id='comp', local_day='2026-07-27', db_path=db, skip_compaction=False,
            )
            self.assertEqual(ctx['status'], dc.STATUS_ABSENT)
            leased = dc.acquire_compaction_lease(int(ctx['id']), 'worker-a', db_path=db)
            self.assertEqual(leased['status'], dc.STATUS_COMPACTING)
            # Default provisional contexts can also late-compact.
            ctx2 = dc.get_or_create_daily_context(
                chat_id='comp2', local_day='2026-07-27', db_path=db,
            )
            self.assertEqual(ctx2['status'], dc.STATUS_PROVISIONAL)
            leased2 = dc.acquire_compaction_lease(int(ctx2['id']), 'worker-b', db_path=db)
            self.assertEqual(leased2['status'], dc.STATUS_COMPACTING)
            failed = dc.release_compaction_to_provisional(int(ctx2['id']), failed=True, db_path=db)
            self.assertEqual(failed['status'], dc.STATUS_FAILED_RETRYABLE)
            recovered = dc.release_compaction_to_provisional(int(ctx2['id']), failed=False, db_path=db)
            self.assertEqual(recovered['status'], dc.STATUS_PROVISIONAL)
        finally:
            os.unlink(db)

    def test_lease_timeout_recoverable_without_raw_sql(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='l', local_day='2026-07-27', db_path=db, skip_compaction=False,
            )
            dc.acquire_compaction_lease(int(ctx['id']), 'old', ttl_seconds=1, db_path=db)
            # Expire lease by rewriting expires_at via public transition fail path:
            # reclaim after forced expiry using acquire with expired stamp.
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE daily_contexts SET lease_expires_at='2020-01-01 00:00:00' WHERE id=?",
                (ctx['id'],),
            )
            conn.commit()
            conn.close()
            got = dc.acquire_compaction_lease(int(ctx['id']), 'new', db_path=db)
            self.assertEqual(got['lease_owner'], 'new')
        finally:
            os.unlink(db)


class CarryoverAndAutoFinalizeTests(unittest.TestCase):
    def _seed_prev_day(self, db: str):
        _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
        _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
        _insert(db, 'system', 'sys', '2026-07-26 10:02:00', source_kind='system')
        _insert(db, 'hayana', 'u2 [[SAVE]]', '2026-07-26 10:03:00')
        _insert(db, 'fyodor', 'wake-msg', '2026-07-26 10:04:00', source_kind='wake')
        _insert(db, 'assistant', 'job done', '2026-07-26 10:05:00',
                tool_calls='[{"name":"ws_job"}]', source_kind='workspace_job')
        _insert(db, 'hayana', 'u3', '2026-07-26 10:06:00')
        _insert(db, 'fyodor', 'a2', '2026-07-26 10:07:00')
        _insert(db, 'hayana', 'u4', '2026-07-26 10:08:00')

    def test_excludes_wake_workspace_system_save(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = _ctx(db, chat_id='c')
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            previews = [c['content_preview'] for c in cands]
            self.assertNotIn('sys', previews)
            self.assertNotIn('wake-msg', previews)
            self.assertNotIn('job done', previews)
            self.assertTrue(all('SAVE' not in p for p in previews))
            result = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(result['carryover_count'], 3)
            self.assertEqual(result['selected_round_count'], 3)
            self.assertEqual(result['selected_message_count'], 5)
            self.assertEqual(
                [m['content'] for m in dc.get_selected_carryover_messages(int(ctx['id']), db_path=db)],
                ['u1', 'a1', 'u3', 'a2', 'u4'],
            )
        finally:
            os.unlink(db)

    def test_first_user_message_already_committed_auto_finalizes_zero(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_prev_day(db)
            ctx = dc.get_or_create_daily_context(chat_id='a', local_day='2026-07-27', db_path=db)
            # Formal path: user message lands first.
            uid = _insert(db, 'hayana', 'first today', '2026-07-27 09:00:00')
            # Generic select would 409 — dedicated atomic path must succeed.
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 3, db_path=db)
            out = dc.finalize_zero_for_first_user_message(int(ctx['id']), uid, db_path=db)
            self.assertEqual(out['carryover_count'], 0)
            refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=db)
            self.assertTrue(refreshed.get('selection_finalized_at'))
            self.assertEqual(refreshed['carryover_count'], 0)

            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='a',
                    daily_context=_ctx(db, '2026-07-28', chat_id='a2', now=datetime.datetime(2026, 7, 28, 9, 0, 0)),
                    current_user_message_id=_insert(db, 'hayana', 'day2 first', '2026-07-28 09:00:00'),
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            self.assertTrue(built['manifest']['selection_finalized'])
            self.assertEqual(built['manifest']['carryover_count'], 0)
        finally:
            os.unlink(db)

    def test_formal_assistant_with_tool_calls_included(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', '查 GitHub', '2026-07-26 10:00:00')
            tool_reply = _insert(
                db, 'fyodor', '审查结果如下', '2026-07-26 10:01:00',
                tool_calls='[{"name":"search","args":{}}]',
                source_kind='chat',
            )
            ctx = _ctx(db, chat_id='tools')
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            previews = [c['content_preview'] for c in cands]
            self.assertIn('审查结果如下', previews)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            carry = dc.get_selected_carryover_messages(int(ctx['id']), db_path=db)
            self.assertTrue(any(m['message_id'] == tool_reply for m in carry))

            user = _insert(db, 'hayana', '继续', '2026-07-27 10:00:00')
            formal_tool = _insert(
                db, 'fyodor', '今日工具回复', '2026-07-27 10:01:00',
                tool_calls='[{"name":"light_on","args":{}}]',
                source_kind='chat',
            )
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='tools',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            ids = [m['message_id'] for m in built['current_day_history']]
            self.assertIn(user, ids)
            self.assertIn(formal_tool, ids)
        finally:
            os.unlink(db)

    def test_formal_chat_ws_job_start_retained(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', '开个后台任务', '2026-07-26 10:00:00')
            ws_start = _insert(
                db, 'fyodor', '已经帮你启动了。', '2026-07-26 10:01:00',
                tool_calls=json.dumps([{
                    'name': 'ws_job',
                    'args': {'action': 'start', 'command': 'echo hi'},
                    'result': '{"ok": true}',
                    'success': True,
                }]),
                source_kind='chat',
            )
            ctx = dc.get_or_create_daily_context(chat_id='wschat', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            self.assertEqual(
                [c['message_id'] for c in cands if '已经帮你启动了' in c['content_preview']],
                [ws_start],
            )
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            carry_ids = [m['message_id'] for m in dc.get_selected_carryover_messages(int(ctx['id']), db_path=db)]
            self.assertIn(ws_start, carry_ids)

            today = _insert(
                db, 'fyodor', '继续跟进', '2026-07-27 10:00:00',
                tool_calls=json.dumps([{
                    'name': 'ws_job',
                    'args': {'action': 'status', 'id': 'job-1'},
                    'result': '{"ok": true}',
                    'success': True,
                }]),
                source_kind='chat',
            )
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                cold = dh.build_daily_window_context(
                    chat_id='wschat',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
                replayed = int(cold['manifest']['replayed_through_message_id'])
                dc.advance_resident_history_cursor(
                    int(ctx['id']), int(ctx['resident_generation']), replayed, db_path=db,
                )
                built = dh.build_daily_window_context(
                    chat_id='wschat',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='S',
                    is_cold=False,
                    db_path=db,
                )
        finally:
            os.unlink(db)

    def test_workspace_job_with_tool_calls_excluded(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u', '2026-07-26 10:00:00')
            _insert(
                db, 'assistant', 'job done', '2026-07-26 10:01:00',
                tool_calls='[{"name":"ws_job"}]', source_kind='workspace_job',
            )
            _insert(db, 'fyodor', 'real', '2026-07-26 10:02:00')
            ctx = dc.get_or_create_daily_context(chat_id='ws', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            previews = [c['content_preview'] for c in cands]
            self.assertNotIn('job done', previews)
            self.assertIn('real', previews)
        finally:
            os.unlink(db)

    def test_legacy_ws_job_completion_without_source_kind_excluded(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'ask', '2026-07-26 09:59:00')
            legacy_tc = [{
                'name': 'ws_job',
                'args': {'action': 'status', 'id': 'job-old'},
                'result': '{"ok": true}',
                'success': True,
                'job': {'job_id': 'job-old', 'status': 'succeeded'},
            }]
            conn = sqlite3.connect(db)
            conn.execute(
                "INSERT INTO chat_messages (author, content, tool_calls, created_at) "
                "VALUES ('assistant', 'legacy completion', ?, '2026-07-26 10:00:00')",
                (json.dumps(legacy_tc, ensure_ascii=False),),
            )
            conn.commit()
            conn.close()
            _insert(db, 'fyodor', 'formal', '2026-07-26 10:01:00')
            ctx = dc.get_or_create_daily_context(chat_id='leg', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            previews = [c['content_preview'] for c in cands]
            self.assertNotIn('legacy completion', previews)
            self.assertIn('formal', previews)
        finally:
            os.unlink(db)

    def test_image_only_message_eligible(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', '', '2026-07-26 12:00:00', image_url='/x.png')
            _insert(db, 'fyodor', 'reply', '2026-07-26 12:01:00')
            ctx = dc.get_or_create_daily_context(chat_id='img', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            self.assertEqual(cands[0]['content_preview'], '[image]')
        finally:
            os.unlink(db)


class HistoryAssemblyFilterTests(unittest.TestCase):
    def test_current_day_excludes_wake_workspace_system(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='h', local_day='2026-07-27', db_path=db)
            dc.finalize_zero_carryover(int(ctx['id']), db_path=db)
            user = _insert(db, 'hayana', 'hi', '2026-07-27 10:00:00')
            _insert(db, 'fyodor', 'wake today', '2026-07-27 10:01:00', source_kind='wake')
            _insert(db, 'assistant', 'job', '2026-07-27 10:02:00',
                    tool_calls='[{"name":"ws_job"}]', source_kind='workspace_job')
            _insert(db, 'system', 'note', '2026-07-27 10:03:00', source_kind='system')
            formal = _insert(db, 'fyodor', 'real reply', '2026-07-27 10:04:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='h',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            ids = [m['message_id'] for m in built['current_day_history']]
            self.assertEqual(ids, [user, formal])
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
            _insert(db, 'hayana', 'today', '2026-07-27 09:00:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('S', 'delta', {'k': 'v'})):
                built = _cold_then_hot_build(
                    db, dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='STATIC',
                    state_mock=('S', 'delta', {'k': 'v'}),
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

    def test_wake_executor_message_excluded(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            from wake.executor import execute

            def get_db():
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                return conn

            execute('message', 't', '爸爸在呢，不催你。', 'normal', get_db)
            # Stamp created_at into prev chat day for candidate scan.
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE chat_messages SET created_at='2026-07-26 20:00:00' WHERE author='fyodor'"
            )
            conn.commit()
            conn.close()
            _insert(db, 'hayana', 'user ok', '2026-07-26 20:01:00')
            ctx = dc.get_or_create_daily_context(chat_id='w', local_day='2026-07-27', db_path=db)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            self.assertEqual([c['content_preview'] for c in cands], ['user ok'])
        finally:
            os.unlink(db)


class EpochFenceTests(unittest.TestCase):
    def test_respawn_between_check_and_write_is_blocked(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='f', local_day='2026-07-27', db_path=db)
            token = dc.make_epoch_token(
                chat_id='f',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )

            def writer_ops():
                return [
                    (
                        'UPDATE daily_contexts SET resident_generation=resident_generation+1 WHERE id=?',
                        (ctx['id'],),
                    ),
                    (
                        'UPDATE daily_contexts SET morning_greeting_message_id=123 WHERE id=?',
                        (ctx['id'],),
                    ),
                ]

            ok, result = dc.commit_if_epoch_current(token, writer_ops(), db_path=db)
            self.assertFalse(ok)
            self.assertEqual(result, 0)
            refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=db)
            self.assertIsNone(refreshed.get('morning_greeting_message_id'))
            self.assertEqual(int(refreshed['resident_generation']), 1)
        finally:
            os.unlink(db)

    def test_matching_token_commits(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='ok', local_day='2026-07-27', db_path=db)
            token = dc.make_epoch_token(
                chat_id='ok',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )

            ok, affected = dc.commit_if_epoch_current(token, [
                (
                    "UPDATE daily_contexts SET morning_greeting_message_id=7 WHERE id=?",
                    (ctx['id'],),
                ),
            ], db_path=db)
            self.assertTrue(ok)
            self.assertEqual(affected, 1)
            self.assertEqual(
                dc.get_daily_context_by_id(int(ctx['id']), db_path=db)['morning_greeting_message_id'],
                7,
            )
        finally:
            os.unlink(db)

    def test_respawn_bumps_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(chat_id='r', local_day='2026-07-27', db_path=db)
            self.assertEqual(int(ctx['resident_generation']), 1)
            nxt = dc.respawn_daily_resident(int(ctx['id']), db_path=db)
            self.assertEqual(int(nxt['resident_generation']), 2)
            d2 = _ctx(db, '2026-07-28', chat_id='r', now=datetime.datetime(2026, 7, 28, 10, 0, 0))
            self.assertEqual(int(d2['resident_generation']), 1)
            self.assertGreater(int(d2['context_epoch']), int(ctx['context_epoch']))
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


class HandoffValidationTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        _seed_handoff_contexts(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def _valid_content(self, **overrides):
        tgt = _ctx(self.db)
        src = _ctx(self.db, '2026-07-26', allow_backfill=True)
        data = {
            'source_day': '2026-07-26',
            'source_epoch': int(src['context_epoch']),
            'boundary_message_id': int(tgt['boundary_message_id']),
            'topics': ['休息'],
            'confirmed_facts': ['用户表示会好好吃早饭'],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': '用户询问休息安排',
        }
        data.update(overrides)
        return data

    def test_metadata_mismatch_rejected(self):
        tgt = _ctx(self.db)
        with self.assertRaises(ValueError):
            dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content=self._valid_content(source_day='2026-07-25'),
                boundary_message_id=int(tgt['boundary_message_id']),
                source_first_message_id=1,
                source_last_message_id=10,
                source_message_count=10,
                source_sha256=hashlib.sha256(b'a').hexdigest(),
                source_epoch=int(_ctx(self.db, '2026-07-26', allow_backfill=True)['context_epoch']),
                db_path=self.db,
            )

    def test_oversized_handoff_rejected(self):
        errors = dc.validate_formal_handoff_content(self._valid_content(
            confirmed_facts=['x' * (dc.MAX_ITEM_CHARS + 1)],
        ))
        self.assertTrue(any('max item length' in e for e in errors))

    def test_failed_retryable_same_sha_can_upgrade_to_ready(self):
        sha = hashlib.sha256(b'retry').hexdigest()
        src = _ctx(self.db, '2026-07-26', allow_backfill=True)
        tgt = _ctx(self.db)
        bnd = int(tgt['boundary_message_id'])
        dc.store_day_handoff(
            chat_id='default',
            source_day='2026-07-26',
            content={},
            boundary_message_id=bnd,
            source_first_message_id=bnd if bnd else 0,
            source_last_message_id=bnd if bnd else 0,
            source_message_count=1 if bnd else 0,
            source_sha256=sha,
            source_epoch=int(src['context_epoch']),
            status=dc.HANDOFF_FAILED_RETRYABLE,
            error_code='timeout',
            db_path=self.db,
        )
        upgraded = dc.store_day_handoff(
            chat_id='default',
            source_day='2026-07-26',
            content=self._valid_content(
                source_epoch=int(src['context_epoch']),
                boundary_message_id=bnd,
            ),
            boundary_message_id=bnd,
            source_first_message_id=bnd if bnd else 0,
            source_last_message_id=bnd if bnd else 0,
            source_message_count=1 if bnd else 0,
            source_sha256=sha,
            source_epoch=int(src['context_epoch']),
            status=dc.HANDOFF_READY,
            db_path=self.db,
        )
        self.assertEqual(upgraded['status'], dc.HANDOFF_READY)
        self.assertIsNotNone(upgraded.get('content_json'))

    def test_invalid_sha_rejected(self):
        with self.assertRaises(ValueError):
            dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content=self._valid_content(),
                boundary_message_id=int(_ctx(self.db)['boundary_message_id']),
                source_first_message_id=1,
                source_last_message_id=10,
                source_message_count=10,
                source_sha256='abc',
                source_epoch=1,
                db_path=self.db,
            )

    def test_ready_requires_source_epoch(self):
        tgt = _ctx(self.db)
        bnd = int(tgt['boundary_message_id'])
        with self.assertRaises(ValueError) as ctx:
            dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content=self._valid_content(source_epoch=37),
                boundary_message_id=bnd,
                source_first_message_id=bnd if bnd else 0,
                source_last_message_id=bnd if bnd else 0,
                source_message_count=1 if bnd else 0,
                source_sha256=hashlib.sha256(b'no-epoch').hexdigest(),
                source_epoch=None,
                db_path=self.db,
            )
        self.assertIn('source_epoch', str(ctx.exception))

    def test_impossible_message_count_rejected(self):
        tgt = _ctx(self.db)
        with self.assertRaises(ValueError) as ctx:
            dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content=self._valid_content(),
                boundary_message_id=int(tgt['boundary_message_id']) or 11,
                source_first_message_id=10,
                source_last_message_id=11,
                source_message_count=500,
                source_sha256=hashlib.sha256(b'bad-count').hexdigest(),
                source_epoch=int(_ctx(self.db, '2026-07-26', allow_backfill=True)['context_epoch']),
                db_path=self.db,
            )
        self.assertIn('exceeds', str(ctx.exception))

    def test_last_message_id_beyond_boundary_rejected(self):
        tgt = _ctx(self.db)
        bnd = int(tgt['boundary_message_id']) or 10
        with self.assertRaises(ValueError) as ctx:
            dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content=self._valid_content(boundary_message_id=bnd),
                boundary_message_id=bnd,
                source_first_message_id=1,
                source_last_message_id=bnd + 1,
                source_message_count=2,
                source_sha256=hashlib.sha256(b'bad-boundary').hexdigest(),
                source_epoch=int(_ctx(self.db, '2026-07-26', allow_backfill=True)['context_epoch']),
                db_path=self.db,
            )
        self.assertIn('boundary', str(ctx.exception))


class ApiRouteHardeningTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)

    def tearDown(self):
        os.unlink(self.db)

    def test_http_cannot_override_db_path(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(create_daily_context_blueprint(
            db_path=self.db, token_getter=lambda: 'tok',
        ))
        client = app.test_client()
        with mock.patch('chat.daily_context.enabled', return_value=True):
            r = client.get(
                '/api/daily-context/current?db_path=/tmp/evil.db',
                headers={'Authorization': 'Bearer tok'},
            )
        self.assertEqual(r.status_code, 200)
        # Context created in injected db, not evil path.
        conn = sqlite3.connect(self.db)
        n = conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists('/tmp/evil.db'))

    def test_no_moments_token_fallback_when_unset(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(create_daily_context_blueprint(
            db_path=self.db, token_getter=lambda: '',
        ))
        client = app.test_client()
        with mock.patch('chat.daily_context.enabled', return_value=True):
            r = client.get(
                '/api/daily-context/current',
                headers={'Authorization': 'Bearer moments-secret'},
            )
        self.assertEqual(r.status_code, 503)

    def test_routes_disabled_by_default(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(create_daily_context_blueprint(
            db_path=self.db, token_getter=lambda: 'tok',
        ))
        client = app.test_client()
        with mock.patch('chat.daily_context.enabled', return_value=False):
            r = client.get(
                '/api/daily-context/current',
                headers={'Authorization': 'Bearer tok'},
            )
        self.assertEqual(r.status_code, 404)


class FlagTests(unittest.TestCase):
    def test_defaults_remain_off(self):
        import config_store
        self.assertEqual(config_store._DEFAULTS.get('DAILY_SOFT_WINDOW_ENABLED'), '0')
        self.assertEqual(config_store._DEFAULTS.get('CC_CLEAN_WINDOW_SHADOW_ENABLED'), '0')


class R0Round4HardeningTests(unittest.TestCase):
    def test_hot_turn_fail_closed_without_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='hotfc')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                with self.assertRaises(dc.HotTurnCursorError):
                    dh.build_daily_window_context(
                        chat_id='hotfc', daily_context=ctx,
                        static_system='S', is_cold=False, db_path=db,
                    )
        finally:
            os.unlink(db)

    def test_hot_turn_only_new_messages_after_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='hotnew')
            u1 = _insert(db, 'hayana', 'first', '2026-07-27 09:00:00')
            a2 = _insert(db, 'fyodor', 'second', '2026-07-27 09:05:00')
            ctx_id = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                cold = dh.build_daily_window_context(
                    chat_id='hotnew',
                    daily_context=dc.get_daily_context_by_id(ctx_id, db_path=db),
                    current_user_message_id=u1,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            self.assertEqual(cold['current_day_history'], [])
            self.assertIsNone(dc.get_resident_history_cursor(ctx_id, gen, db_path=db))
            dc.advance_resident_history_cursor(ctx_id, gen, a2, db_path=db)
            u3 = _insert(db, 'hayana', 'third', '2026-07-27 09:10:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='hotnew',
                    daily_context=dc.get_daily_context_by_id(ctx_id, db_path=db),
                    current_user_message_id=u3,
                    static_system='S',
                    is_cold=False,
                    db_path=db,
                )
            self.assertEqual(built['current_day_history'], [])
            self.assertEqual(built['manifest']['cursor_before'], a2)
        finally:
            os.unlink(db)

    def test_resident_cursor_lifecycle_excludes_replayed_messages(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='cursorlife')
            ctx_id = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            u101 = _insert(db, 'hayana', 'user one', '2026-07-27 09:00:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                cold = dh.build_daily_window_context(
                    chat_id='cursorlife',
                    daily_context=dc.get_daily_context_by_id(ctx_id, db_path=db),
                    current_user_message_id=u101,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            self.assertEqual(cold['current_day_history'], [])
            self.assertTrue(cold['manifest']['cursor_advance_required'])
            self.assertIsNone(dc.get_resident_history_cursor(ctx_id, gen, db_path=db))
            a102 = _insert(db, 'fyodor', 'assistant one', '2026-07-27 09:01:00')
            dc.advance_resident_history_cursor(ctx_id, gen, a102, db_path=db)
            u103 = _insert(db, 'hayana', 'user two', '2026-07-27 09:02:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                hot = dh.build_daily_window_context(
                    chat_id='cursorlife',
                    daily_context=dc.get_daily_context_by_id(ctx_id, db_path=db),
                    current_user_message_id=u103,
                    static_system='S',
                    is_cold=False,
                    db_path=db,
                )
            ids = [m['message_id'] for m in hot['current_day_history']]
            self.assertNotIn(u101, ids)
            self.assertNotIn(a102, ids)
            self.assertNotIn(u103, ids)
            self.assertEqual(ids, [])
        finally:
            os.unlink(db)

    def test_cursor_advance_monotonic_and_cas(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='monocur')
            ctx_id = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            m1 = _insert(db, 'hayana', 'a', '2026-07-27 09:00:00')
            m2 = _insert(db, 'fyodor', 'b', '2026-07-27 09:01:00')
            dc.advance_resident_history_cursor(ctx_id, gen, m1, db_path=db)
            dc.advance_resident_history_cursor(ctx_id, gen, m2, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.advance_resident_history_cursor(ctx_id, gen, m1, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.advance_resident_history_cursor(
                    ctx_id, gen, m2, expected_cursor=m1, db_path=db,
                )
        finally:
            os.unlink(db)

    def test_cursor_absent_cas_race(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='casrace')
            ctx_id = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            u1 = _insert(db, 'hayana', 'user', '2026-07-27 09:00:00')
            a1 = _insert(db, 'fyodor', 'assistant', '2026-07-27 09:01:00')
            a2 = _insert(db, 'fyodor', 'assistant2', '2026-07-27 09:02:00')
            refreshed = dc.get_daily_context_by_id(ctx_id, db_path=db)
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                first = dh.build_daily_window_context(
                    chat_id='casrace',
                    daily_context=refreshed,
                    current_user_message_id=u1,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
                second = dh.build_daily_window_context(
                    chat_id='casrace',
                    daily_context=refreshed,
                    current_user_message_id=u1,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            self.assertIsNone(first['manifest']['cursor_before'])
            self.assertIsNone(second['manifest']['cursor_before'])
            dc.advance_resident_history_cursor(
                ctx_id, gen, a1, expected_cursor=None, db_path=db,
            )
            with self.assertRaises(dc.ConflictError):
                dc.advance_resident_history_cursor(
                    ctx_id, gen, a2, expected_cursor=None, db_path=db,
                )
            self.assertEqual(dc.get_resident_history_cursor(ctx_id, gen, db_path=db), a1)
        finally:
            os.unlink(db)

    def test_backfill_resident_lifecycle_no_side_effects(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _ctx(db, '2026-07-27', chat_id='bflife')
            back = _ctx(db, '2026-07-25', chat_id='bflife', allow_backfill=True)
            back_id = int(back['id'])
            gen_before = int(back['resident_generation'])
            with self.assertRaises(dc.DailyContextError) as retire_err:
                dc.retire_resident_for_rollover(back_id, db_path=db)
            self.assertIn('backfill', str(retire_err.exception).lower())
            with self.assertRaises(dc.DailyContextError) as respawn_err:
                dc.respawn_daily_resident(back_id, db_path=db)
            self.assertIn('backfill', str(respawn_err.exception).lower())
            refreshed = dc.get_daily_context_by_id(back_id, db_path=db)
            self.assertEqual(int(refreshed['resident_generation']), gen_before)
            self.assertIsNone(
                dc.get_resident_history_cursor(back_id, gen_before, db_path=db),
            )
        finally:
            os.unlink(db)

    def test_respawn_cold_like_rebuilds_handoff_carryover_state(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'prev', '2026-07-26 11:00:00')
            _insert(db, 'fyodor', 'prev reply', '2026-07-26 11:01:00')
            src = _ctx(db, '2026-07-26', chat_id='resp', allow_backfill=True)
            tgt = _ctx(db, '2026-07-27', chat_id='resp')
            dc.select_carryover(int(tgt['id']), 3, db_path=db)
            content = {
                'source_day': '2026-07-26',
                'source_epoch': int(src['context_epoch']),
                'boundary_message_id': int(tgt['boundary_message_id']),
                'topics': ['休息'],
                'confirmed_facts': ['用户表示会好好吃早饭'],
                'decisions': [],
                'open_loops': [],
                'explicit_user_requests': [],
                'last_topic': '用户询问休息安排',
            }
            dc.store_day_handoff(
                chat_id='resp',
                source_day='2026-07-26',
                content=content,
                boundary_message_id=int(tgt['boundary_message_id']),
                source_first_message_id=1,
                source_last_message_id=1,
                source_message_count=1,
                source_sha256=hashlib.sha256(b'respawn').hexdigest(),
                source_epoch=int(src['context_epoch']),
                db_path=db,
            )
            respawned = dc.respawn_daily_resident(int(tgt['id']), db_path=db)
            _insert(db, 'hayana', 'today user', '2026-07-27 10:00:00')
            with mock.patch(
                'chat.daily_history._build_state_text',
                return_value=('STATE', 'snapshot', {'k': 'v'}),
            ):
                built = dh.build_daily_window_context(
                    chat_id='resp',
                    daily_context=respawned,
                    static_system='S',
                    is_cold=False,
                    is_respawn=True,
                    db_path=db,
                )
            self.assertTrue(built['manifest']['handoff_injected_this_turn'])
            self.assertTrue(built['manifest']['carryover_injected_this_turn'])
            self.assertEqual(built['manifest']['state_mode'], 'snapshot')
            self.assertTrue(built['current_day_history'])
        finally:
            os.unlink(db)

    def test_active_backfill_epoch_collision_active_stays_current(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            active = _ctx(db, '2026-07-27', chat_id='collide')
            back = _ctx(db, '2026-07-25', chat_id='collide', allow_backfill=True)
            self.assertEqual(int(active['context_epoch']), 1)
            self.assertEqual(int(back['context_epoch']), 1)
            self.assertEqual(int(back.get('is_backfill') or 0), 1)
            active_token = dc.make_epoch_token(
                chat_id='collide',
                context_id=int(active['id']),
                context_epoch=int(active['context_epoch']),
                resident_generation=int(active['resident_generation']),
            )
            backfill_token = dc.make_epoch_token(
                chat_id='collide',
                context_id=int(back['id']),
                context_epoch=int(back['context_epoch']),
                resident_generation=int(back['resident_generation']),
                is_backfill=True,
            )
            self.assertTrue(dc.is_epoch_current(active_token, db_path=db))
            self.assertFalse(dc.is_epoch_current(backfill_token, db_path=db))
            ok, _ = dc.commit_if_epoch_current(active_token, [
                (
                    'UPDATE daily_contexts SET carryover_count=carryover_count WHERE id=?',
                    (int(active['id']),),
                ),
            ], db_path=db)
            self.assertTrue(ok)
            ok_back, _ = dc.commit_if_epoch_current(backfill_token, [
                (
                    'UPDATE daily_contexts SET carryover_count=carryover_count WHERE id=?',
                    (int(back['id']),),
                ),
            ], db_path=db)
            self.assertFalse(ok_back)
        finally:
            os.unlink(db)

    def test_fence_rejects_end_begin_and_multi_statement(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='fencebypass')
            token = dc.make_epoch_token(
                chat_id='fencebypass',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )
            for sql in ('END', 'BEGIN', 'COMMIT; SELECT 1'):
                with self.assertRaises(Exception):
                    dc.commit_if_epoch_current(token, [(sql, ())], db_path=db)
                self.assertIsNone(
                    dc.get_daily_context_by_id(int(ctx['id']), db_path=db).get(
                        'morning_greeting_message_id',
                    ),
                )
        finally:
            os.unlink(db)

    def test_fence_sql_allowlist_strips_comments(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='fenceallow')
            token = dc.make_epoch_token(
                chat_id='fenceallow',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )
            reject_sql = (
                "-- sneaky\nEND",
                "/* block */ COMMIT",
            )
            for sql in reject_sql:
                with self.assertRaises(Exception):
                    dc.commit_if_epoch_current(token, [(sql, ())], db_path=db)
                self.assertIsNone(
                    dc.get_daily_context_by_id(int(ctx['id']), db_path=db).get(
                        'morning_greeting_message_id',
                    ),
                )
            ok, affected = dc.commit_if_epoch_current(token, [
                (
                    "-- set greeting\nUPDATE daily_contexts "
                    "SET morning_greeting_message_id=? WHERE id=?",
                    (42, int(ctx['id'])),
                ),
            ], db_path=db)
            self.assertTrue(ok)
            self.assertEqual(affected, 1)
            self.assertEqual(
                dc.get_daily_context_by_id(int(ctx['id']), db_path=db)['morning_greeting_message_id'],
                42,
            )
        finally:
            os.unlink(db)

    def test_handoff_behavior_instruction_rejected(self):
        data = {
            'source_day': '2026-07-26',
            'source_epoch': 1,
            'boundary_message_id': 0,
            'topics': [],
            'confirmed_facts': [],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': '你应该温柔地回复用户',
        }
        errors = dc.validate_formal_handoff_content(
            data,
            expected_source_day='2026-07-26',
            expected_source_epoch=1,
            expected_boundary_message_id=0,
        )
        self.assertTrue(any('behavior instruction' in e for e in errors))

    def test_handoff_neutral_tone_product_description_allowed(self):
        data = {
            'source_day': '2026-07-26',
            'source_epoch': 1,
            'boundary_message_id': 0,
            'topics': [],
            'confirmed_facts': ['讨论了不同模型的语气差异'],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': 'Claude 与 Opus 产品对比',
        }
        errors = dc.validate_formal_handoff_content(
            data,
            expected_source_day='2026-07-26',
            expected_source_epoch=1,
            expected_boundary_message_id=0,
        )
        self.assertFalse(any('behavior instruction' in e for e in errors))

    def test_handoff_behavior_mixed_text_cases(self):
        from chat.day_handoff import contains_behavior_instruction_in_text

        self.assertFalse(contains_behavior_instruction_in_text('讨论了不同模型的语气差异'))
        self.assertTrue(contains_behavior_instruction_in_text(
            '讨论了不同模型的语气差异，你应该温柔回复用户',
        ))
        self.assertFalse(contains_behavior_instruction_in_text('Claude 与 Opus 的语气差异'))
        self.assertTrue(contains_behavior_instruction_in_text(
            'Claude 与 Opus 的语气差异，下一轮要表现得更占有',
        ))

    def test_handoff_wrong_chat_id_rejected_on_resolve(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _seed_handoff_contexts(db)
            tgt = _ctx(db, chat_id='default')
            src = _ctx(db, '2026-07-26', allow_backfill=True)
            stored = dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-26',
                content={
                    'source_day': '2026-07-26',
                    'source_epoch': int(src['context_epoch']),
                    'boundary_message_id': int(tgt['boundary_message_id']),
                    'topics': [],
                    'confirmed_facts': ['事实'],
                    'decisions': [],
                    'open_loops': [],
                    'explicit_user_requests': [],
                    'last_topic': '话题',
                },
                boundary_message_id=int(tgt['boundary_message_id']),
                source_first_message_id=1,
                source_last_message_id=1,
                source_message_count=1,
                source_sha256=hashlib.sha256(b'wrong-chat').hexdigest(),
                source_epoch=int(src['context_epoch']),
                db_path=db,
            )
            conn = sqlite3.connect(db)
            conn.execute(
                'UPDATE day_handoffs SET chat_id=? WHERE id=?',
                ('other', int(stored['id'])),
            )
            conn.commit()
            conn.close()
            ctx = dict(tgt)
            ctx['handoff_id'] = int(stored['id'])
            content_out, status = dc.resolve_bound_handoff(ctx, db_path=db)
            self.assertIsNone(content_out)
            self.assertEqual(status, dc.HANDOFF_FAILED_RETRYABLE)
        finally:
            os.unlink(db)

    def test_handoff_wrong_source_day_rejected_on_resolve(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _seed_handoff_contexts(db)
            tgt = _ctx(db, chat_id='default')
            wrong_day = _ctx(db, '2026-07-25', allow_backfill=True)
            consumer = dc.get_daily_context_by_id(
                int(_ctx(db, '2026-07-26', allow_backfill=True)['id']),
                db_path=db,
            )
            bnd = int(consumer['boundary_message_id'])
            stored = dc.store_day_handoff(
                chat_id='default',
                source_day='2026-07-25',
                content={
                    'source_day': '2026-07-25',
                    'source_epoch': int(wrong_day['context_epoch']),
                    'boundary_message_id': bnd,
                    'topics': [],
                    'confirmed_facts': ['事实'],
                    'decisions': [],
                    'open_loops': [],
                    'explicit_user_requests': [],
                    'last_topic': '话题',
                },
                boundary_message_id=bnd,
                source_first_message_id=bnd if bnd else 0,
                source_last_message_id=bnd if bnd else 0,
                source_message_count=1 if bnd else 0,
                source_sha256=hashlib.sha256(b'wrong-day').hexdigest(),
                source_epoch=int(wrong_day['context_epoch']),
                db_path=db,
            )
            ctx = dict(tgt)
            ctx['handoff_id'] = int(stored['id'])
            content_out, status = dc.resolve_bound_handoff(ctx, db_path=db)
            self.assertIsNone(content_out)
            self.assertEqual(status, dc.HANDOFF_FAILED_RETRYABLE)
        finally:
            os.unlink(db)

    def test_carryover_lock_during_compaction(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='comp', skip_compaction=False)
            leased = dc.acquire_compaction_lease(int(ctx['id']), 'w', db_path=db)
            self.assertEqual(leased['status'], dc.STATUS_COMPACTING)
            result = dc.select_carryover(int(ctx['id']), 0, db_path=db)
            self.assertEqual(result['carryover_count'], 0)
        finally:
            os.unlink(db)

    def test_auto_zero_mid_day_enable(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'already here', '2026-07-27 08:00:00')
            ctx = _ctx(db, chat_id='mid')
            out = dc.ensure_carryover_zero_if_user_messages_exist(int(ctx['id']), db_path=db)
            self.assertEqual(out['carryover_count'], 0)
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 3, db_path=db)
        finally:
            os.unlink(db)

    def test_fence_writer_cannot_commit(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = _ctx(db, chat_id='fence')
            token = dc.make_epoch_token(
                chat_id='fence',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )
            with self.assertRaises(Exception):
                dc.commit_if_epoch_current(
                    token,
                    [('COMMIT', ())],
                    db_path=db,
                )
            self.assertIsNone(
                dc.get_daily_context_by_id(int(ctx['id']), db_path=db).get('morning_greeting_message_id'),
            )
        finally:
            os.unlink(db)

    def test_backfill_does_not_bump_active_epoch(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            active = _ctx(db, chat_id='bf')
            active2 = _ctx(
                db, '2026-07-28', chat_id='bf',
                now=datetime.datetime(2026, 7, 28, 10, 0, 0),
            )
            back = _ctx(db, '2026-07-25', chat_id='bf', allow_backfill=True)
            self.assertLess(int(back['context_epoch']), int(active2['context_epoch']))
            self.assertEqual(int(back.get('is_backfill') or 0), 1)
        finally:
            os.unlink(db)

    def test_future_day_rejected(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            with self.assertRaises(ValueError):
                _ctx(db, '2099-01-01')
        finally:
            os.unlink(db)

    def test_handoff_claude_product_name_allowed(self):
        data = {
            'source_day': '2026-07-26',
            'source_epoch': 1,
            'boundary_message_id': 0,
            'topics': [],
            'confirmed_facts': ['用户提到 Claude Code 插件'],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': 'Claude 产品讨论',
        }
        errors = dc.validate_formal_handoff_content(
            data,
            expected_source_day='2026-07-26',
            expected_source_epoch=1,
            expected_boundary_message_id=0,
        )
        self.assertFalse(any('assistant voice' in e for e in errors))

    def test_handoff_speaker_label_rejected(self):
        data = {
            'source_day': '2026-07-26',
            'source_epoch': 1,
            'boundary_message_id': 0,
            'topics': [],
            'confirmed_facts': [],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': 'Claude: 你好',
        }
        errors = dc.validate_formal_handoff_content(
            data,
            expected_source_day='2026-07-26',
            expected_source_epoch=1,
            expected_boundary_message_id=0,
        )
        self.assertTrue(any('assistant voice' in e for e in errors))

    def test_non_default_chat_id_rejected(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask
        app = Flask(__name__)
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            app.register_blueprint(create_daily_context_blueprint(
                db_path=db, token_getter=lambda: 'tok',
            ))
            client = app.test_client()
            with mock.patch('chat.daily_context.enabled', return_value=True):
                r = client.get(
                    '/api/daily-context/current?chat_id=other',
                    headers={'Authorization': 'Bearer tok'},
                )
            self.assertEqual(r.status_code, 400)
        finally:
            os.unlink(db)


class RoundBasedCarryoverTests(unittest.TestCase):
    def _ctx(self, db: str, chat_id: str = 'default'):
        return dc.get_or_create_daily_context(
            chat_id=chat_id, local_day='2026-07-27', db_path=db,
        )

    def _seed_three_rounds_six_messages(self, db: str):
        _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
        _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
        _insert(db, 'hayana', 'u2', '2026-07-26 10:02:00')
        _insert(db, 'fyodor', 'a2', '2026-07-26 10:03:00')
        _insert(db, 'hayana', 'u3', '2026-07-26 10:04:00')
        _insert(db, 'fyodor', 'a3', '2026-07-26 10:05:00')

    def test_three_rounds_map_to_six_messages(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            data = dc.list_carryover_rounds(int(ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(data['available_round_count'], 3)
            self.assertEqual(len(data['rounds']), 3)
            flat = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            self.assertEqual(len(flat), 6)
            self.assertEqual([r['messages'][0]['role'] for r in data['rounds']], ['user', 'user', 'user'])
        finally:
            os.unlink(db)

    def test_round_with_multiple_assistants(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            u = _insert(db, 'hayana', 'question', '2026-07-26 10:00:00')
            a1 = _insert(db, 'fyodor', 'part1', '2026-07-26 10:01:00')
            a2 = _insert(db, 'fyodor', 'part2', '2026-07-26 10:02:00')
            ctx = self._ctx(db)
            rnd = dc.group_carryover_rounds(
                dc._collect_prev_day_eligible_messages(
                    dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    db_path=db,
                )
            )
            self.assertEqual(len(rnd), 1)
            self.assertEqual(rnd[0]['round_id'], u)
            self.assertEqual(rnd[0]['message_ids'], [u, a1, a2])
        finally:
            os.unlink(db)

    def test_last_round_user_only(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            tail = _insert(db, 'hayana', 'u-tail', '2026-07-26 10:02:00')
            ctx = self._ctx(db)
            data = dc.list_carryover_rounds(int(ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(data['rounds'][-1]['round_id'], tail)
            self.assertEqual(data['rounds'][-1]['message_ids'], [tail])
            self.assertEqual(data['rounds'][-1]['messages'][0]['role'], 'user')
        finally:
            os.unlink(db)

    def test_leading_orphan_assistant_excluded(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'fyodor', 'orphan', '2026-07-26 10:00:00')
            u = _insert(db, 'hayana', 'real', '2026-07-26 10:01:00')
            ctx = self._ctx(db)
            data = dc.list_carryover_rounds(int(ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(len(data['rounds']), 1)
            self.assertEqual(data['rounds'][0]['round_id'], u)
            cands = dc.list_carryover_candidates(int(ctx['id']), limit=10, db_path=db)
            self.assertEqual([c['content_preview'] for c in cands], ['real'])
        finally:
            os.unlink(db)

    def test_save_user_excludes_orphan_assistant(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u [[SAVE]]', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'orphan-after-save', '2026-07-26 10:01:00')
            u2 = _insert(db, 'hayana', 'ok', '2026-07-26 10:02:00')
            ctx = self._ctx(db)
            data = dc.list_carryover_rounds(int(ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(len(data['rounds']), 1)
            self.assertEqual(data['rounds'][0]['round_id'], u2)
        finally:
            os.unlink(db)

    def test_limit_applies_to_rounds_not_messages(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            for i in range(12):
                _insert(db, 'hayana', 'u%d' % i, '2026-07-26 %02d:00:00' % (10 + i))
                _insert(db, 'fyodor', 'a%d' % i, '2026-07-26 %02d:01:00' % (10 + i))
            ctx = self._ctx(db)
            data = dc.list_carryover_rounds(int(ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(data['available_round_count'], 12)
            self.assertEqual(len(data['rounds']), 10)
            self.assertEqual(sum(len(r['message_ids']) for r in data['rounds']), 20)
            result = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(result['selected_round_count'], 3)
            self.assertEqual(result['selected_message_count'], 6)
        finally:
            os.unlink(db)

    def test_select_fewer_rounds_than_available(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            result = dc.select_carryover(int(ctx['id']), 5, db_path=db)
            self.assertEqual(result['selected_round_count'], 3)
            self.assertEqual(result['selected_message_count'], 6)
        finally:
            os.unlink(db)

    def test_selected_message_ids_flatten_order_and_ordinals(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            result = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(result['selected_round_count'], 3)
            self.assertEqual(result['selected_message_count'], 6)
            selected = dc.get_selected_carryover_messages(int(ctx['id']), db_path=db)
            self.assertEqual(result['selected_message_ids'], [m['message_id'] for m in selected])
            self.assertEqual([m['content'] for m in selected], ['u1', 'a1', 'u2', 'a2', 'u3', 'a3'])
        finally:
            os.unlink(db)

    def test_carryover_count_stores_rounds_message_count_separate(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            result = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=db)
            self.assertEqual(refreshed['carryover_count'], 3)
            self.assertEqual(result['selected_round_count'], 3)
            self.assertEqual(result['selected_message_count'], 6)
        finally:
            os.unlink(db)

    def test_idempotent_retry_returns_same_counts_and_ids(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            first = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            second = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(second['requested_round_count'], first['requested_round_count'])
            self.assertEqual(second['selected_round_count'], first['selected_round_count'])
            self.assertEqual(second['selected_message_count'], first['selected_message_count'])
            self.assertEqual(second['selected_message_ids'], first['selected_message_ids'])
        finally:
            os.unlink(db)

    def test_idempotent_when_fewer_rounds_than_requested(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            _insert(db, 'hayana', 'u2', '2026-07-26 10:02:00')
            _insert(db, 'fyodor', 'a2', '2026-07-26 10:03:00')
            ctx = self._ctx(db)
            first = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            second = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(first['requested_round_count'], 3)
            self.assertEqual(first['selected_round_count'], 2)
            self.assertEqual(first['selected_message_count'], 4)
            self.assertEqual(second, first)
            refreshed = dc.get_daily_context_by_id(int(ctx['id']), db_path=db)
            self.assertEqual(int(refreshed['carryover_requested_count']), 3)
            self.assertEqual(int(refreshed['carryover_count']), 2)
        finally:
            os.unlink(db)

    def test_idempotent_when_three_rounds_request_five(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            first = dc.select_carryover(int(ctx['id']), 5, db_path=db)
            second = dc.select_carryover(int(ctx['id']), 5, db_path=db)
            self.assertEqual(first['requested_round_count'], 5)
            self.assertEqual(first['selected_round_count'], 3)
            self.assertEqual(second, first)
        finally:
            os.unlink(db)

    def test_idempotent_zero_rounds_available_request_three(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = self._ctx(db)
            first = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            second = dc.select_carryover(int(ctx['id']), 3, db_path=db)
            self.assertEqual(first['requested_round_count'], 3)
            self.assertEqual(first['selected_round_count'], 0)
            self.assertEqual(second, first)
        finally:
            os.unlink(db)

    def test_different_requested_tier_after_lock_returns_409(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 5, db_path=db)
        finally:
            os.unlink(db)

    def test_api_routes_deferred_while_old_day_lease_active(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid = _insert(db, 'hayana', 'late', start.strftime('%Y-%m-%d %H:%M:%S'))
            dc.acquire_resident_turn_lease(
                int(old_ctx['id']), 1,
                lease_owner='worker-api', request_message_id=uid,
                db_path=db, now=start,
            )

            app = Flask(__name__)
            app.register_blueprint(create_daily_context_blueprint(
                db_path=db, token_getter=lambda: 'tok',
            ))
            client = app.test_client()

            auth = {'Authorization': 'Bearer tok'}
            real_resolve = dc.resolve_current_daily_context_for_api
            with mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch(
                     'chat.daily_context.resolve_current_daily_context_for_api',
                     side_effect=lambda **kw: real_resolve(
                         chat_id=kw.get('chat_id', 'default'),
                         db_path=kw.get('db_path', db),
                         now=finish,
                     ),
                 ):
                for path in (
                    '/api/daily-context/carryover-candidates',
                    '/api/daily-context/current',
                ):
                    resp = client.get(path, headers=auth)
                    self.assertEqual(resp.status_code, 423, path)
                    body = resp.get_json()
                    self.assertEqual(body['code'], 'rollover_deferred')
                    self.assertTrue(body['retryable'])
                resp = client.post(
                    '/api/daily-context/select-carryover',
                    json={'count': 3},
                    headers=auth,
                )
                self.assertEqual(resp.status_code, 423)
                self.assertEqual(resp.get_json()['code'], 'rollover_deferred')
            self.assertIsNone(
                dc.get_context_for_local_day('default', '2026-07-27', db_path=db),
            )
        finally:
            os.unlink(db)

    def test_post_route_idempotent_contract_fewer_rounds(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            self._ctx(db, chat_id='default')
            app = Flask(__name__)
            app.register_blueprint(create_daily_context_blueprint(
                db_path=db, token_getter=lambda: 'tok',
            ))
            client = app.test_client()
            auth = {'Authorization': 'Bearer tok'}
            real_resolve = dc.resolve_current_daily_context_for_api
            with mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch(
                     'chat.daily_context.resolve_current_daily_context_for_api',
                     side_effect=lambda **kw: real_resolve(
                         chat_id=kw.get('chat_id', 'default'),
                         db_path=kw.get('db_path', db),
                         now=_FIXED_NOW,
                     ),
                 ):
                first = client.post(
                    '/api/daily-context/select-carryover',
                    json={'count': 3},
                    headers=auth,
                )
                second = client.post(
                    '/api/daily-context/select-carryover',
                    json={'count': 3},
                    headers=auth,
                )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            body1 = first.get_json()
            body2 = second.get_json()
            self.assertEqual(body1['requested_round_count'], 3)
            self.assertEqual(body1['selected_round_count'], 1)
            self.assertEqual(body1['selected_message_count'], 2)
            self.assertEqual(body2['selected_message_ids'], body1['selected_message_ids'])
        finally:
            os.unlink(db)

    def test_cross_midnight_user_and_mapped_assistant_same_round(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='xmid', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid = _insert(db, 'hayana', 'late user', start.strftime('%Y-%m-%d %H:%M:%S'))
            aid = _insert(db, 'assistant', 'late reply', finish.strftime('%Y-%m-%d %H:%M:%S'))
            dc.record_daily_message_context(
                uid, context_id=int(old_ctx['id']), context_epoch=int(old_ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            dc.record_daily_message_context(
                aid, context_id=int(old_ctx['id']), context_epoch=int(old_ctx['context_epoch']),
                resident_generation=1, role='assistant', db_path=db,
            )
            new_ctx = dc.get_or_create_daily_context(
                chat_id='xmid', local_day='2026-07-27', db_path=db, now=finish,
            )
            data = dc.list_carryover_rounds(int(new_ctx['id']), limit_rounds=10, db_path=db)
            self.assertEqual(len(data['rounds']), 1)
            self.assertEqual(data['rounds'][0]['message_ids'], [uid, aid])
            result = dc.select_carryover(int(new_ctx['id']), 3, db_path=db)
            self.assertEqual(result['selected_round_count'], 1)
            self.assertEqual(result['selected_message_count'], 2)
            self.assertEqual(result['selected_message_ids'], [uid, aid])
        finally:
            os.unlink(db)

    def test_manifest_reports_round_and_message_counts(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            ctx = self._ctx(db)
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            _insert(db, 'hayana', 'today', '2026-07-27 09:00:00')
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='default',
                    daily_context=dc.get_daily_context_by_id(int(ctx['id']), db_path=db),
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            manifest = built['manifest']
            self.assertEqual(manifest['carryover_unit'], 'round')
            self.assertEqual(manifest['carryover_count'], 3)
            self.assertEqual(manifest['carryover_round_count'], 3)
            self.assertEqual(manifest['carryover_message_count'], 6)
            user_contents = [
                m['content'] for m in built['carryover_messages'] if m['role'] == 'user'
            ]
            self.assertEqual(user_contents, ['u1', 'u2', 'u3'])
        finally:
            os.unlink(db)

    def test_get_route_round_contract(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            self._seed_three_rounds_six_messages(db)
            app = Flask(__name__)
            app.register_blueprint(create_daily_context_blueprint(
                db_path=db, token_getter=lambda: 'test-token',
            ))
            client = app.test_client()
            real_resolve = dc.resolve_current_daily_context_for_api
            with mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch(
                     'chat.daily_context.resolve_current_daily_context_for_api',
                     side_effect=lambda **kw: real_resolve(
                         chat_id=kw.get('chat_id', 'default'),
                         db_path=kw.get('db_path', db),
                         now=_FIXED_NOW,
                     ),
                 ):
                resp = client.get(
                    '/api/daily-context/carryover-candidates',
                    headers={'Authorization': 'Bearer test-token'},
                )
                self.assertEqual(resp.status_code, 200)
                body = resp.get_json()
                self.assertEqual(body['carryover_unit'], 'round')
                self.assertEqual(body['available_round_count'], 3)
                self.assertEqual(len(body['rounds']), 3)
                flat_ids = [c['message_id'] for c in body['candidates']]
                round_flat = [
                    m['message_id'] for r in body['rounds'] for m in r['messages']
                ]
                self.assertEqual(flat_ids, round_flat)
        finally:
            os.unlink(db)


class CurrentRoundTruthTests(unittest.TestCase):
    def _summary(self, db: str, *, now=_FIXED_NOW):
        return dc.current_summary(chat_id='default', db_path=db, now=now)

    def test_unselected_current_round_truth(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            data = self._summary(db)
            self.assertEqual(data['carryover_unit'], 'round')
            self.assertIsNone(data['requested_round_count'])
            self.assertEqual(data['selected_round_count'], 0)
            self.assertEqual(data['selected_message_count'], 0)
            self.assertEqual(data['selected_message_ids'], [])
            self.assertEqual(data['carryover_count'], 0)
            self.assertFalse(data['selection_finalized'])
        finally:
            os.unlink(db)

    def test_explicit_zero_locked_current_truth(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            dc.select_carryover(int(ctx['id']), 0, db_path=db)
            data = self._summary(db)
            self.assertEqual(data['requested_round_count'], 0)
            self.assertEqual(data['selected_round_count'], 0)
            self.assertEqual(data['selected_message_count'], 0)
            self.assertEqual(data['selected_message_ids'], [])
            self.assertTrue(data['selection_finalized'])
        finally:
            os.unlink(db)

    def test_request_five_only_two_rounds_current_truth(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            _insert(db, 'hayana', 'u2', '2026-07-26 10:02:00')
            _insert(db, 'fyodor', 'a2', '2026-07-26 10:03:00')
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            dc.select_carryover(int(ctx['id']), 5, db_path=db)
            data = self._summary(db)
            self.assertEqual(data['requested_round_count'], 5)
            self.assertEqual(data['selected_round_count'], 2)
            self.assertEqual(data['carryover_count'], 2)
            self.assertEqual(data['selected_message_count'], 4)
            self.assertEqual(len(data['selected_message_ids']), 4)
            self.assertTrue(data['selection_finalized'])
        finally:
            os.unlink(db)

    def test_selected_message_ids_follow_ordinal_order(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            u1 = _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            a1 = _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            u2 = _insert(db, 'hayana', 'u2', '2026-07-26 10:02:00')
            a2 = _insert(db, 'fyodor', 'a2', '2026-07-26 10:03:00')
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            data = self._summary(db)
            self.assertEqual(data['selected_message_ids'], [u1, a1, u2, a2])
        finally:
            os.unlink(db)

    def test_current_route_returns_round_truth_fields(self):
        from daily_context_routes import create_daily_context_blueprint
        from flask import Flask

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            dc.select_carryover(int(ctx['id']), 5, db_path=db)
            expected = self._summary(db)
            app = Flask(__name__)
            app.register_blueprint(create_daily_context_blueprint(
                db_path=db, token_getter=lambda: 'tok',
            ))
            client = app.test_client()
            real_resolve = dc.resolve_current_daily_context_for_api
            with mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch(
                     'chat.daily_context.resolve_current_daily_context_for_api',
                     side_effect=lambda **kw: real_resolve(
                         chat_id=kw.get('chat_id', 'default'),
                         db_path=kw.get('db_path', db),
                         now=_FIXED_NOW,
                     ),
                 ):
                resp = client.get(
                    '/api/daily-context/current',
                    headers={'Authorization': 'Bearer tok'},
                )
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertTrue(body['ok'])
            for key in (
                'carryover_unit', 'requested_round_count', 'selected_round_count',
                'selected_message_count', 'selected_message_ids', 'carryover_count',
                'selection_finalized',
            ):
                self.assertEqual(body[key], expected[key], key)
        finally:
            os.unlink(db)

    def test_after_409_get_current_restores_requested_tier(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'u1', '2026-07-26 10:00:00')
            _insert(db, 'fyodor', 'a1', '2026-07-26 10:01:00')
            _insert(db, 'hayana', 'u2', '2026-07-26 10:02:00')
            _insert(db, 'fyodor', 'a2', '2026-07-26 10:03:00')
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db,
            )
            dc.select_carryover(int(ctx['id']), 3, db_path=db)
            with self.assertRaises(dc.ConflictError):
                dc.select_carryover(int(ctx['id']), 5, db_path=db)
            data = self._summary(db)
            self.assertEqual(data['requested_round_count'], 3)
            self.assertEqual(data['selected_round_count'], 2)
            self.assertEqual(data['carryover_count'], 2)
            self.assertEqual(data['selected_message_count'], 4)
            self.assertTrue(data['selection_finalized'])
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
