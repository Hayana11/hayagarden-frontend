"""P-CONTEXT-DAILY-SOFT-WINDOW-R1 resident integration tests — no model calls."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident
import config_store
from chat import context_window as cw
from chat import daily_context as dc
from chat import daily_history as dh
from chat import daily_runtime as dr
from chat.daily_context import ConflictError, DeferredError
from chat.canonical_turn import CanonicalTurn, projection_hash
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_READY,
    get_context_claude_session,
    register_context_claude_session,
)
from chat.system_builder import build_cc_daily_static_parts, build_cc_static_parts
from continuity.sources import (
    build_source_members,
    derive_autonomous_events,
    derive_completed_turns,
)
from tools.cc_jsonl_usage import session_jsonl_path
from tools.execution_fence import evaluate_tool_call


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
            cache_info TEXT DEFAULT '',
            choices TEXT DEFAULT '',
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
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='r45a_scope_fixture'"
    ).fetchone()
    if table_exists:
        role = 'user' if author.lower() in {'hayana', 'haya', 'user'} else 'assistant'
        conn.execute(
            'INSERT INTO daily_message_contexts '
            '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
            'VALUES (?,?,?,?,?,?)',
            (mid, 1, 1, 1, role, created_at),
        )
        conn.commit()
    conn.close()
    return mid


_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)
_REAL_PREPARE_DAILY_TURN = dr.prepare_daily_turn


def _prepare_turn(db, uid, *, now=None, wall_now=None, **kwargs):
    """Test helper: pin epoch wall clock explicitly without mutating production defaults."""
    with mock.patch.object(config_store, 'get_bool', return_value=True), \
         mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
        return _REAL_PREPARE_DAILY_TURN(
            user_message_id=uid,
            db_path=db,
            now=now or _FIXED_NOW,
            wall_now=wall_now if wall_now is not None else (now or _FIXED_NOW),
            static_system=kwargs.pop('static_system', 'S'),
            **kwargs,
        )


def _test_canonical_turn(content: str = '成功回复', *, tool_calls=()) -> CanonicalTurn:
    """Small deterministic canonical prerequisite for non-parser unit tests."""
    calls = tuple(dict(call) for call in tool_calls)
    segments = [
        {'type': 'tool', 'tool_index': index}
        for index, _call in enumerate(calls)
    ]
    segments.append({'type': 'text', 'text': content})
    display_segments = json.dumps(segments, ensure_ascii=False, separators=(',', ':'))
    return CanonicalTurn(
        content=content,
        thinking='',
        display_segments=display_segments,
        tool_calls=calls,
        choices=(),
        provider_rounds=(
            {
                'index': 1,
                'event_uuid': 'test-canonical-assistant',
                'text_block_count': 1,
                'tool_use_count': len(calls),
                'stop_reason': 'end_turn',
            },
        ),
        stop_reason='end_turn',
        terminal_state='confirmed',
        transcript_identity={
            'path': '/tmp/test-canonical.jsonl',
            'start_offset': 0,
            'end_offset': 1,
            'session_id': 'test-canonical-session',
            'mapping_status': 'MAPPED',
            'message_id': None,
            'context_id': None,
            'context_epoch': None,
            'resident_generation': None,
        },
        projection_hash=projection_hash(content, display_segments),
    )


def _attach_successful_canonical(plan, *, content: str = '成功回复', tool_calls=()):
    canonical = _test_canonical_turn(content, tool_calls=tool_calls)
    plan._canonical_turn = canonical
    plan.manifest.update({
        'transcript_mapping_status': 'MAPPED',
        'transcript_mapping_error_code': None,
        'transcript_mapping_event_count': 1,
    })
    return canonical


def _mark_transcript_mapped(plan, **_kwargs):
    plan.manifest.update({
        'transcript_mapping_status': 'MAPPED',
        'transcript_mapping_error_code': None,
        'transcript_mapping_event_count': 1,
    })
    return dict(plan.manifest)


def _persist_canonical_for_plan(plan):
    canonical = dr.build_canonical_turn_for_plan(plan)
    aid = dr.persist_daily_assistant_for_plan(
        plan,
        content=canonical.content,
        thinking=canonical.thinking,
        tool_calls=json.dumps(list(canonical.tool_calls), ensure_ascii=False)
        if canonical.tool_calls else '',
        choices=json.dumps(list(canonical.choices), ensure_ascii=False)
        if canonical.choices else '',
        display_segments=canonical.display_segments,
    )
    return canonical, aid


def _gateway_canonical_builder(*, content: str = 'daily reply', tool_calls=()):
    canonical = _test_canonical_turn(content, tool_calls=tool_calls)

    def _build(plan, **_kwargs):
        plan._canonical_turn = canonical
        plan.manifest.update({
            'transcript_mapping_status': 'MAPPED',
            'transcript_mapping_error_code': None,
            'transcript_mapping_event_count': 1,
        })
        return canonical

    return _build


class _FakeResident:
    generation = 1
    tool_profile = cc_resident.TOOL_PROFILE_UH_A0

    def __init__(self):
        self._alive = False
        self._cold = True
        self.last_state_snapshot = {'mood': 'calm'}
        self.sent: list[str] = []
        self.killed = 0
        self._spawn_args: list[tuple] = []
        self.turn_leases: list[dict[str, Any]] = []

    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        self._spawn_args.append((system_text, tool_profile))
        self._alive = True
        cold = self._cold
        self._cold = False
        self.tool_profile = tool_profile
        return cold

    def _alive_fn(self):
        return self._alive

    def _kill(self, quiet=True):
        self._alive = False
        self.killed += 1

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        self.turn_leases.append(turn_lease)
        yield ('text', 'daily reply')
        yield ('done', ('daily reply', '', {'input_tokens': 3, 'output_tokens': 5}, {}))


class _ReadToolResident(_FakeResident):
    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        self.turn_leases.append(turn_lease)
        decision = evaluate_tool_call(
            'mcp__home__get_light_status', {}, turn_lease,
        )
        yield ('tool_use', {
            'id': 't1',
            'name': 'mcp__home__get_light_status',
            'args': {},
            **decision,
        })
        yield ('tool_result', {
            'tool_use_id': 't1',
            'content': '{"ok":true,"result":{"power":false}}',
            'is_error': False,
        })
        yield ('text', '根据刚才读取到的结果回答。')
        yield ('done', ('根据刚才读取到的结果回答。', '', {}, {}))


class _FailingResident(_FakeResident):
    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        raise RuntimeError('provider exploded')


class _SurpriseColdResident(_FakeResident):
    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        self._cold = True
        return super().ensure_alive(system_text, env, tool_profile=tool_profile)


def _binding_for_plan(plan: dr.DailyTurnPlan, *, cursor: int | None = None) -> dr.LocalResidentBinding:
    return dr.LocalResidentBinding(
        resident_key=plan.resident_key,
        context_id=plan.context_id,
        context_epoch=plan.context_epoch,
        resident_generation=plan.resident_generation,
        bound_cursor_message_id=cursor,
        process_generation=1,
        tool_profile=dr.DAILY_TOOL_PROFILE,
    )


class DailyRuntimeFlagOffTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_prepare_rejects_when_disabled(self):
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            with self.assertRaises(dr.DailyRuntimeError):
                dr.prepare_daily_turn(user_message_id=1)

    def test_flag_off_no_binding(self):
        with mock.patch.object(config_store, 'get_bool', return_value=False):
            self.assertIsNone(dr.get_local_binding())

    def test_legacy_static_unchanged(self):
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'):
            legacy = build_cc_static_parts()
        self.assertIn('[[SAVE', legacy['save_instr'])
        self.assertIn('你拥有真实的工具', legacy['stable_note'])


class DailyStaticProfileTests(unittest.TestCase):
    def test_daily_static_excludes_tools_and_save(self):
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'):
            parts = build_cc_daily_static_parts()
        full = parts['full_system']
        self.assertNotIn('[[SAVE', full)
        self.assertNotIn('你拥有真实的工具', full)
        self.assertNotIn('workspace', full.lower())
        self.assertIn('[choices]', full)

    def test_strip_save_markers_no_side_effects(self):
        cleaned, had = dr.strip_daily_save_markers('hi [[SAVE: secret]] there')
        self.assertTrue(had)
        self.assertEqual(cleaned, 'hi  there')


class DailyRuntimeLeaseTests(unittest.TestCase):
    def test_single_lease_under_concurrency(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='lease', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])

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
            dc.release_resident_turn_lease(cid, gen, lease_owner='o%d' % ok[0][1], db_path=db)
        finally:
            os.unlink(db)

    def test_prepare_releases_lease_on_assembly_failure(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history.build_daily_window_context', side_effect=RuntimeError('asm')):
                with self.assertRaises(RuntimeError):
                    _prepare_turn(db, uid, now=_FIXED_NOW, wall_now=_FIXED_NOW)
            ctx = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), int(ctx['resident_generation']), db_path=db,
            ))
        finally:
            os.unlink(db)

    def test_heartbeat_renews_lease(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            now_s = now.strftime('%Y-%m-%d %H:%M:%S')
            uid = _insert(db, 'hayana', 'hb', now_s)
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
                 mock.patch.object(dr, 'LEASE_HEARTBEAT_INTERVAL', 0.02):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW)
                hb = dr.LeaseHeartbeat(plan)
                hb.start()
                time.sleep(0.06)
                hb.stop()
            self.assertFalse(hb.failed)
            self.assertTrue(dc.is_resident_turn_active(
                plan.context_id, plan.resident_generation, db_path=db,
            ))
            dr._release_lease(plan)
        finally:
            os.unlink(db)


class DailyRuntimeTurnTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def _prepare(self, db, uid, *, resident=None, now=None, wall_now=None):
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('STATE', 'snapshot', {'k': 'v'})):
            return _prepare_turn(
                db, uid,
                now=now,
                wall_now=wall_now,
                resident=resident,
                static_system='STATIC',
            )

    def test_auto_display_thinking_uses_resident_model_authority(self):
        from chat.display_thinking import prepare_daily_display_thinking_plan

        for identity, expected_suffix in (
            ('explicit:claude-opus-5-5', False),
            ('explicit:claude-opus-4-6', True),
        ):
            with self.subTest(model_identity=identity):
                db = _tmp_db()
                plan = None
                try:
                    _init_chat_messages(db)
                    uid = _insert(db, 'hayana', 'hello', '2026-07-27 10:00:00')
                    plan = self._prepare(db, uid)
                    prepare_daily_display_thinking_plan(plan, 'auto')
                    resident = _FakeResident()
                    resident._model_identity = identity
                    list(dr.stream_daily_resident_turn(
                        plan, resident=resident, env={}, static_system='STATIC',
                    ))
                    marker = '正式回复不能为空。'
                    self.assertEqual(expected_suffix, marker in resident.sent[0])
                    self.assertEqual('auto', plan.provider_display_thinking_mode)
                    self.assertEqual(
                        'native' if not expected_suffix else 'auto',
                        plan.provider_display_thinking_effective_mode,
                    )
                finally:
                    if plan is not None:
                        dr._release_lease(plan)
                    os.unlink(db)

    def test_cold_turn_atomic_persist_and_cursor(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'hello', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _FakeResident()
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC',
            ))
            self.assertEqual(resident.turn_leases, [plan.turn_lease])
            self.assertFalse(any(evt == 'tool_use' for evt, _payload in events))
            self.assertTrue(any(evt == 'done' for evt, _payload in events))
            aid = dr.persist_daily_assistant_for_plan(
                plan, content='reply', thinking='', tool_calls='', cache_info='', choices='',
            )
            out = dr.complete_daily_turn(plan, assistant_message_id=aid)
            self.assertEqual(out['cursor_after'], aid)
            self.assertIsNotNone(dc.get_message_context(aid, db_path=db))
        finally:
            os.unlink(db)

    def test_uh_a0_spawn_args(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'spawn', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _FakeResident()
            list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system='STATIC'))
            self.assertEqual(resident._spawn_args[-1][1], cc_resident.TOOL_PROFILE_UH_A0)
        finally:
            os.unlink(db)

    def test_key_change_closes_old_binding(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'k', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _FakeResident()
            dr.set_local_binding(_binding_for_plan(plan))
            list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system='STATIC'))
            dr._release_lease(plan)
            old_key = plan.resident_key
            dc.respawn_daily_resident(plan.context_id, db_path=db)
            uid2 = _insert(db, 'hayana', 'k2', '2026-07-27 10:01:00')
            plan2 = self._prepare(db, uid2, resident=resident)
            self.assertNotEqual(plan2.resident_key, old_key)
            self.assertGreaterEqual(resident.killed, 1)
        finally:
            os.unlink(db)

    def test_hot_to_cold_reprepare_single_send(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'hot', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('STATE', 'snapshot', {'k': 'v'})):
                plan = self._prepare(db, uid)
                original_plan = plan
                original_id = id(plan)
                old_gen = int(plan.resident_generation)
                plan.is_cold = False
                plan.manifest['turn_kind'] = 'hot'
                resident = _SurpriseColdResident()
                dr.set_local_binding(_binding_for_plan(plan, cursor=0))
                events = list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
                # Caller keeps the same plan object; identity is adopted in place.
                self.assertIs(plan, original_plan)
                self.assertEqual(id(plan), original_id)
                self.assertGreater(int(plan.resident_generation), old_gen)
                _attach_successful_canonical(plan, content='daily reply')
                aid = dr.persist_daily_assistant_for_plan(plan, content='daily reply')
                with mock.patch.object(
                    dr,
                    'finalize_transcript_mapping_after_success',
                    side_effect=_mark_transcript_mapped,
                ):
                    out = dr.handle_provider_success(
                        plan, assistant_message_id=aid, raw_text='daily reply',
                    )
            self.assertTrue(any(e[0] == 'done' for e in events))
            self.assertEqual(len(resident.sent), 1)
            self.assertNotIn('新增正式对话', resident.sent[0])
            self.assertEqual(out.get('assistant_message_id'), aid)
            self.assertTrue(plan.lease_released)
            self.assertFalse(dc.is_resident_turn_active(
                plan.context_id, plan.resident_generation, db_path=db, now=_FIXED_NOW,
            ))
        finally:
            os.unlink(db)

    def test_default_chat_leases_are_fresh_and_write_is_in_scope(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid1 = _insert(db, 'hayana', 'first', '2026-07-27 10:00:00')
            first = self._prepare(db, uid1)
            self.assertEqual(first.turn_lease['turn_mode'], 'chat')
            self.assertEqual(first.turn_lease['issued_from'], 'default_policy')
            self.assertIn('home.light.status', first.turn_lease['allowed_capabilities'])
            self.assertIn('todo.write', first.turn_lease['allowed_capabilities'])
            dr._release_lease(first)

            uid2 = _insert(db, 'hayana', 'second', '2026-07-27 10:01:00')
            second = self._prepare(db, uid2)
            self.assertIsNot(first.turn_lease, second.turn_lease)
            self.assertNotEqual(first.turn_lease['turn_id'], second.turn_lease['turn_id'])
            decision = evaluate_tool_call(
                'mcp__home__add_todo',
                {'content': 'must not execute'},
                second.turn_lease,
            )
            self.assertEqual(
                decision['lease_decision'],
                'ALLOW',
            )
            self.assertNotIn('approval_id', decision)
            dr._release_lease(second)
        finally:
            os.unlink(db)

    def test_read_tool_use_and_result_flow_through_daily(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', '灯现在是什么状态？', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _ReadToolResident()
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC',
            ))
            self.assertEqual(resident.turn_leases, [plan.turn_lease])
            tool_use = [payload for evt, payload in events if evt == 'tool_use']
            tool_result = [payload for evt, payload in events if evt == 'tool_result']
            self.assertEqual(len(tool_use), 1)
            self.assertEqual(tool_use[0]['name'], 'mcp__home__get_light_status')
            self.assertEqual(tool_use[0]['capability_id'], 'home.light.status')
            self.assertEqual(tool_use[0]['lease_decision'], 'ALLOW')
            self.assertEqual(len(tool_result), 1)
            self.assertEqual(tool_result[0]['tool_use_id'], 't1')
            self.assertFalse(tool_result[0]['is_error'])
            self.assertTrue(any(evt == 'text' for evt, _payload in events))
            self.assertTrue(any(evt == 'done' for evt, _payload in events))
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_stale_abort_no_respawn(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'stale', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            gen_before = plan.resident_generation
            dc.respawn_daily_resident(plan.context_id, db_path=db)
            out = dr.abort_daily_turn(plan, error_code='stale', respawn=False)
            self.assertFalse(out.get('abort_epoch_current', True))
            refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=db)
            self.assertGreater(int(refreshed['resident_generation']), gen_before)
        finally:
            os.unlink(db)

    def test_persist_rejects_stale_epoch(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'p', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            dc.respawn_daily_resident(plan.context_id, db_path=db)
            with self.assertRaises(ConflictError):
                dr.persist_daily_assistant_for_plan(plan, content='x')
        finally:
            os.unlink(db)


class DailyRuntimeRolloverTests(unittest.TestCase):
    def test_0359_vs_0400_chat_day(self):
        ts1 = datetime.datetime(2026, 7, 27, 3, 59, 59)
        ts2 = datetime.datetime(2026, 7, 27, 4, 0, 0)
        self.assertEqual(dc.chat_day_for_timestamp(ts1), '2026-07-26')
        self.assertEqual(dc.chat_day_for_timestamp(ts2), '2026-07-27')

    def test_message_context_scoped_history(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='scope', local_day='2026-07-26', db_path=db,
                now=datetime.datetime(2026, 7, 26, 12, 0, 0), allow_backfill=True,
            )
            ctx2 = dc.get_or_create_daily_context(
                chat_id='scope', local_day='2026-07-27', db_path=db,
                now=datetime.datetime(2026, 7, 27, 10, 0, 0),
            )
            old_user = _insert(db, 'hayana', 'old user', '2026-07-26 23:00:00')
            old_asst = _insert(db, 'assistant', 'old reply', '2026-07-27 03:59:59')
            dc.record_daily_message_context(
                old_user, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            dc.record_daily_message_context(
                old_asst, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='assistant', db_path=db,
            )
            from chat import daily_history as dh
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='scope',
                    daily_context=ctx2,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            hist_ids = [m['message_id'] for m in built.get('current_day_history') or []]
            self.assertNotIn(old_asst, hist_ids)
        finally:
            os.unlink(db)


class DailyRuntimeWorkerOwnerTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_worker_takeover_bumps_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='w', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            key_a = dc.make_resident_key(
                chat_id='w', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            dc.upsert_resident_owner(
                cid, gen, worker_id='worker-a', resident_key=key_a, db_path=db,
            )
            key_b = dc.make_resident_key(
                chat_id='w', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            status, new_gen, new_key = dc.ensure_worker_resident_owner(
                cid, gen,
                worker_id='worker-b',
                resident_key=key_b,
                bound_cursor_message_id=None,
                process_generation=None,
                db_path=db,
            )
            self.assertEqual(status, 'takeover')
            self.assertGreater(new_gen, gen)
            owner = dc.get_resident_owner(cid, new_gen, db_path=db)
            self.assertEqual(str(owner.get('worker_id')), 'worker-b')
            self.assertEqual(new_key, str(owner.get('resident_key')))
        finally:
            os.unlink(db)

    def test_stale_worker_cannot_hot_after_takeover(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            resident = _FakeResident()
            uid = _insert(db, 'hayana', 'a', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})):
                plan_a = _prepare_turn(
                    db, uid,
                    wall_now=_FIXED_NOW,
                    lease_owner='worker-a', resident=resident,
                )
                self.assertFalse(plan_a.is_cold is False and plan_a.manifest.get('turn_kind') == 'hot')
                dr._release_lease(plan_a)
                uid2 = _insert(db, 'hayana', 'b', '2026-07-27 10:01:00')
                with mock.patch.object(dr, 'WORKER_ID', 'worker-b'):
                    plan_b = _prepare_turn(
                        db, uid2,
                        wall_now=_FIXED_NOW,
                        lease_owner='worker-b', resident=resident,
                    )
                dr._release_lease(plan_b)
                refreshed = dc.get_daily_context_by_id(plan_b.context_id, db_path=db)
                owner = dc.get_resident_owner(
                    plan_b.context_id, int(refreshed['resident_generation']), db_path=db,
                )
                self.assertEqual(str(owner.get('worker_id')), 'worker-b')
                uid3 = _insert(db, 'hayana', 'c', '2026-07-27 10:02:00')
                with mock.patch.object(dr, 'WORKER_ID', 'worker-a'):
                    plan_c = _prepare_turn(
                        db, uid3,
                        wall_now=_FIXED_NOW,
                        lease_owner='worker-a', resident=resident,
                    )
                self.assertTrue(plan_c.is_cold or plan_c.manifest.get('turn_kind') != 'hot')
                dr._release_lease(plan_c)
        finally:
            os.unlink(db)


class DailyRuntimeLeaseTtlTests(unittest.TestCase):
    def test_renew_extends_lease_past_original_ttl(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='ttl', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])
            t0 = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='owner-a', request_message_id=1,
                ttl_seconds=2, db_path=db, now=t0,
            )
            dc.upsert_resident_owner(
                cid, gen,
                worker_id='worker-a',
                resident_key=dc.make_resident_key(
                    chat_id='ttl', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
                ),
                db_path=db,
            )
            t_renew = t0 + datetime.timedelta(seconds=1)
            token = dc.make_epoch_token(
                chat_id='ttl', context_id=cid, context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            dc.renew_resident_turn_lease(
                cid, gen, lease_owner='owner-a', ttl_seconds=60, db_path=db,
                now=t_renew, epoch_token=token,
            )
            t_late = t0 + datetime.timedelta(seconds=3)
            with self.assertRaises(dc.ConflictError):
                dc.acquire_resident_turn_lease(
                    cid, gen, lease_owner='owner-b', request_message_id=2,
                    ttl_seconds=2, db_path=db, now=t_late,
                )
        finally:
            os.unlink(db)

    def test_heartbeat_failure_aborts_before_persist(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'hb-fail', '2026-07-27 10:00:00')

            class _SlowResident(_FakeResident):
                def send_turn(self, content, commit_meta=None, turn_lease=None):
                    time.sleep(0.08)
                    yield from super().send_turn(content, commit_meta=commit_meta)

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
                 mock.patch('chat.daily_context.renew_resident_turn_lease', side_effect=dc.ConflictError('stale')):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW)
                resident = _SlowResident()
                with mock.patch.object(dr, 'LEASE_HEARTBEAT_INTERVAL', 0.01):
                    with self.assertRaises((dr.LeaseConflictError, dr.LeaseHeartbeatTerminalFailure)):
                        list(dr.stream_daily_resident_turn(
                            plan, resident=resident, env={}, static_system='S',
                        ))
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(int(count), 0)
            self.assertGreaterEqual(resident.killed, 1)
        finally:
            os.unlink(db)


class DailyRuntimeMidnightPersistTests(unittest.TestCase):
    def test_assistant_belongs_to_start_epoch_after_rollover(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            ctx = dc.get_or_create_daily_context(
                chat_id='mid', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid = _insert(db, 'hayana', 'late night', '2026-07-27 03:59:59')
            dc.record_daily_message_context(
                uid, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            token = dc.make_epoch_token(
                chat_id='mid', context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']), resident_generation=1,
            )
            dc.acquire_resident_turn_lease(
                int(ctx['id']), 1, lease_owner='night-owner', request_message_id=uid,
                db_path=db, now=start,
            )
            dc.upsert_resident_owner(
                int(ctx['id']), 1,
                worker_id='night-worker',
                resident_key=dc.make_resident_key(
                    chat_id='mid', context_epoch=int(ctx['context_epoch']), resident_generation=1,
                ),
                db_path=db,
            )
            dc.renew_resident_turn_lease(
                int(ctx['id']), 1, lease_owner='night-owner', db_path=db,
                now=finish, epoch_token=token,
            )
            aid = dc.persist_daily_assistant_if_current(
                chat_id='mid',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
                lease_owner='night-owner',
                content='reply after midnight',
                db_path=db,
                now=finish,
            )
            self.assertGreater(aid, 0)
            mapping = dc.get_message_context(aid, db_path=db)
            self.assertEqual(int(mapping['context_id']), int(ctx['id']))
            ctx_new = dc.get_or_create_daily_context(
                chat_id='mid', local_day='2026-07-27', db_path=db, now=finish,
            )
            from chat import daily_history as dh
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='mid', daily_context=ctx_new, static_system='S',
                    is_cold=True, db_path=db,
                )
            hist_ids = [m['message_id'] for m in built.get('current_day_history') or []]
            self.assertNotIn(aid, hist_ids)
        finally:
            os.unlink(db)


class GatewayFlagOffGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('/opt/workspace/tools', exist_ok=True)

    def test_daily_path_not_entered_when_disabled(self):
        import gateway
        calls = {'daily_static': 0, 'legacy_static': 0, 'daily_stream': 0, 'legacy_stream': 0}

        def _daily_static():
            calls['daily_static'] += 1
            return {'persona': 'D', 'full_system': 'D'}

        def _legacy_static():
            calls['legacy_static'] += 1
            return {'persona': 'L', 'full_system': 'L', 'save_instr': '[[SAVE]]'}

        def _daily_stream(*_a, **_k):
            calls['daily_stream'] += 1
            yield 'data: {"t":"done","ok":true}\n\n'
            return

        class _LegacyResident:
            def ensure_alive(self, *_a, **_k):
                return True

            def send_turn(self, *_a, **_k):
                yield 'done', ('ok', '', 0, 0)

        conn = sqlite3.connect(':memory:')
        conn.execute(
            'CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, '
            'thinking TEXT, tool_calls TEXT, cache_info TEXT, choices TEXT)'
        )
        patches = [
            mock.patch.object(config_store, 'get_bool', return_value=False),
            mock.patch('chat.daily_context.enabled', return_value=False),
            mock.patch('chat.system_builder.build_cc_daily_static_parts', side_effect=_daily_static),
            mock.patch('chat.system_builder.build_cc_static_parts', side_effect=_legacy_static),
            mock.patch.object(gateway, '_stream_cc_daily_soft_window', side_effect=_daily_stream),
            mock.patch.object(gateway, '_CC_RESIDENT', _LegacyResident()),
            mock.patch.object(gateway, '_get_provider', return_value='claude_code'),
            mock.patch('gateway._gen_acquire_or_wait', return_value=('new', None)),
            mock.patch('gateway._gen_release'),
            mock.patch('gateway.build_messages', return_value=[]),
            mock.patch('gateway.is_pending_user_turn', return_value=False),
            mock.patch('gateway.capture_pending_wake_ids', return_value=[]),
            mock.patch('gateway._cc_resident_stream_gen', return_value=iter([('done', ('hi', '', 0, 0))])),
            mock.patch('moments_turn.prepare_turn', return_value={'content': 'hi'}),
            mock.patch('moments_turn.insert_user_message', return_value={'content': 'hi', 'user_message_id': 1}),
            mock.patch('moments_turn.activate_turn', side_effect=lambda td, **_k: td),
            mock.patch('moments_turn.release_turn'),
            mock.patch('gateway.consume_wake_ids'),
            mock.patch('chat.system_builder.consume_cc_one_shot_claims'),
            mock.patch('gateway._write_session_memo'),
            mock.patch('gateway.get_db', return_value=conn),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            save_mock = stack.enter_context(mock.patch('gateway._cc_save_markers', return_value='hi'))
            client = gateway.app.test_client()
            resp = client.post('/chat/stream', json={'content': 'hi'})
            self.assertEqual(resp.status_code, 200)
            list(resp.response)
        self.assertEqual(calls['daily_static'], 0)
        self.assertEqual(calls['daily_stream'], 0)
        self.assertGreater(calls['legacy_static'], 0)
        save_mock.assert_called()


class DailyRuntimeAtomicClaimTests(unittest.TestCase):
    def test_claim_rejects_active_lease_from_other_owner(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='atomic', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='owner-a', request_message_id=1,
                db_path=db, now=now,
            )
            with self.assertRaises(dc.ConflictError):
                dc.claim_daily_resident_turn(
                    chat_id='atomic',
                    context_id=cid,
                    expected_context_epoch=int(ctx['context_epoch']),
                    worker_id='worker-b',
                    request_message_id=2,
                    lease_owner='owner-b',
                    resident_key=dc.make_resident_key(
                        chat_id='atomic', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
                    ),
                    db_path=db,
                    now=now,
                )
        finally:
            os.unlink(db)

    def test_claim_takeover_bumps_generation_atomically(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='take', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            key = dc.make_resident_key(
                chat_id='take', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            dc.upsert_resident_owner(cid, gen, worker_id='worker-a', resident_key=key, db_path=db)
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            out = dc.claim_daily_resident_turn(
                chat_id='take',
                context_id=cid,
                expected_context_epoch=int(ctx['context_epoch']),
                worker_id='worker-b',
                request_message_id=10,
                lease_owner='owner-b',
                resident_key=key,
                db_path=db,
                now=now,
            )
            self.assertEqual(out['status'], 'takeover')
            self.assertGreater(int(out['resident_generation']), gen)
        finally:
            os.unlink(db)

    def test_claim_same_owner_different_request_conflicts(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='same-owner', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            key = dc.make_resident_key(
                chat_id='same-owner', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            dc.claim_daily_resident_turn(
                chat_id='same-owner',
                context_id=cid,
                expected_context_epoch=int(ctx['context_epoch']),
                worker_id='worker-a',
                request_message_id=100,
                lease_owner='shared-owner',
                resident_key=key,
                db_path=db,
                now=now,
            )
            with self.assertRaises(dc.ConflictError):
                dc.claim_daily_resident_turn(
                    chat_id='same-owner',
                    context_id=cid,
                    expected_context_epoch=int(ctx['context_epoch']),
                    worker_id='worker-b',
                    request_message_id=101,
                    lease_owner='shared-owner',
                    resident_key=key,
                    db_path=db,
                    now=now,
                )
        finally:
            os.unlink(db)

    def test_claim_idempotent_same_owner_returns_without_bump(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='idem', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid = int(ctx['id'])
            gen = int(ctx['resident_generation'])
            key = dc.make_resident_key(
                chat_id='idem', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            first = dc.claim_daily_resident_turn(
                chat_id='idem',
                context_id=cid,
                expected_context_epoch=int(ctx['context_epoch']),
                worker_id='worker-a',
                request_message_id=200,
                lease_owner='idem-owner',
                resident_key=key,
                db_path=db,
                now=now,
            )
            second = dc.claim_daily_resident_turn(
                chat_id='idem',
                context_id=cid,
                expected_context_epoch=int(ctx['context_epoch']),
                worker_id='worker-a',
                request_message_id=200,
                lease_owner='idem-owner',
                resident_key=key,
                db_path=db,
                now=now,
            )
            self.assertEqual(second['status'], 'idempotent')
            self.assertEqual(int(second['resident_generation']), int(first['resident_generation']))
            refreshed = dc.get_daily_context_by_id(cid, db_path=db)
            self.assertEqual(int(refreshed['resident_generation']), gen)
        finally:
            os.unlink(db)

    def test_idempotent_claim_blocks_second_prepare(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'dup', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan1 = _prepare_turn(db, uid, wall_now=_FIXED_NOW, lease_owner='dup-owner')
                with self.assertRaises(dr.DuplicateTurnInProgress) as ctx:
                    _prepare_turn(db, uid, wall_now=_FIXED_NOW, lease_owner='dup-owner')
                self.assertTrue(ctx.exception.retryable)
                resident = _FakeResident()
                list(dr.stream_daily_resident_turn(
                    plan1, resident=resident, env={}, static_system='S',
                ))
            self.assertEqual(len(resident.sent), 1)
            refreshed = dc.get_daily_context_by_id(plan1.context_id, db_path=db)
            self.assertEqual(int(refreshed['resident_generation']), 1)
            dr._release_lease(plan1)
        finally:
            os.unlink(db)


class DailyRuntimeRolloverLeaseFenceTests(unittest.TestCase):
    def test_resolver_defers_new_day_when_old_lease_active(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='rollover', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid_old = _insert(db, 'hayana', 'old turn', start.strftime('%Y-%m-%d %H:%M:%S'))
            uid_new = _insert(db, 'hayana', 'new day', finish.strftime('%Y-%m-%d %H:%M:%S'))
            gen_before = int(old_ctx['resident_generation'])
            dc.acquire_resident_turn_lease(
                int(old_ctx['id']), gen_before,
                lease_owner='worker-a', request_message_id=uid_old,
                db_path=db, now=start,
            )
            dc.upsert_resident_owner(
                int(old_ctx['id']), gen_before,
                worker_id='worker-a',
                resident_key=dc.make_resident_key(
                    chat_id='rollover',
                    context_epoch=int(old_ctx['context_epoch']),
                    resident_generation=gen_before,
                ),
                db_path=db,
            )
            resident = _FakeResident()
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})), \
                 mock.patch.object(dc, 'has_active_provider_turn_lease', return_value=False), \
                 mock.patch('chat.daily_context.retire_resident_for_rollover') as retire_mock:
                plan = _prepare_turn(
                    db, uid_new,
                    chat_id='rollover',
                    now=finish,
                    wall_now=finish,
                    resident=resident,
                )
                retire_mock.assert_not_called()
            self.assertIsNone(
                dc.get_context_for_local_day('rollover', '2026-07-27', db_path=db),
            )
            self.assertEqual(plan.context_id, int(old_ctx['id']))
            # Manual canonical path may takeover an active lease and close the resident.
            self.assertGreaterEqual(resident.killed, 1)
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_expired_old_lease_allows_new_day_context(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = start + datetime.timedelta(seconds=5)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='expired', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid_old = _insert(db, 'hayana', 'old', start.strftime('%Y-%m-%d %H:%M:%S'))
            uid_new = _insert(db, 'hayana', 'new', finish.strftime('%Y-%m-%d %H:%M:%S'))
            dc.acquire_resident_turn_lease(
                int(old_ctx['id']), 1,
                lease_owner='worker-a', request_message_id=uid_old,
                ttl_seconds=2, db_path=db, now=start,
            )
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})), \
                 mock.patch.object(dc, 'has_active_provider_turn_lease', return_value=False):
                plan = _prepare_turn(
                    db, uid_new,
                    chat_id='expired',
                    now=finish,
                    wall_now=finish,
                )
            self.assertEqual(plan.context_id, int(old_ctx['id']))
            self.assertIsNone(
                dc.get_context_for_local_day('expired', '2026-07-27', db_path=db),
            )
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_same_origin_lease_is_duplicate_not_rollover_deferred(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'same', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                _prepare_turn(db, uid, wall_now=_FIXED_NOW, lease_owner='same-owner')
                with self.assertRaises(dr.DuplicateTurnInProgress):
                    _prepare_turn(db, uid, wall_now=_FIXED_NOW, lease_owner='same-owner')
        finally:
            os.unlink(db)


class DailyRuntimeOriginDayTests(unittest.TestCase):
    def test_stale_origin_prevents_backdated_context_creation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            uid = _insert(db, 'hayana', 'late', start.strftime('%Y-%m-%d %H:%M:%S'))
            new_ctx = dc.get_or_create_daily_context(
                chat_id='origin', local_day='2026-07-27', db_path=db, now=finish,
            )
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})):
                plan = _prepare_turn(
                    db, uid,
                    chat_id='origin',
                    now=start,
                    wall_now=finish,
                )
                open_ctx = cw.get_current_context_window(db_path=db, now=finish, chat_id='origin')
                self.assertEqual(plan.context_id, int(open_ctx['id']))
                self.assertEqual(plan.context_id, int(new_ctx['id']))
            self.assertIsNone(
                dc.get_context_for_local_day('origin', '2026-07-26', db_path=db),
            )
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_stale_origin_rejects_even_when_old_context_exists(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            dc.get_or_create_daily_context(
                chat_id='exist', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            dc.get_or_create_daily_context(
                chat_id='exist', local_day='2026-07-27', db_path=db, now=finish,
            )
            uid = _insert(db, 'hayana', 'late', start.strftime('%Y-%m-%d %H:%M:%S'))
            resident = _FakeResident()
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})), \
                 mock.patch('chat.daily_context.retire_resident_for_rollover') as retire_mock:
                plan = _prepare_turn(
                    db, uid,
                    chat_id='exist',
                    now=start,
                    wall_now=finish,
                    resident=resident,
                )
                retire_mock.assert_not_called()
            open_ctx = cw.get_current_context_window(db_path=db, now=finish, chat_id='exist')
            self.assertEqual(plan.context_id, int(open_ctx['id']))
            self.assertEqual(str(open_ctx['local_day']), '2026-07-27')
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_cross_midnight_creates_active_closing_context(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 0, 1)
            uid = _insert(db, 'hayana', 'cross', start.strftime('%Y-%m-%d %H:%M:%S'))
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _prepare_turn(db, uid, now=start, wall_now=finish)
                open_ctx = cw.get_current_context_window(db_path=db, now=finish)
                self.assertEqual(plan.context_id, int(open_ctx['id']))
                resident = _FakeResident()
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='S',
                ))
                self.assertEqual(len(resident.sent), 1)
                aid = dr.persist_daily_assistant_for_plan(
                    plan, content='cross reply', thinking='', tool_calls='', cache_info='', choices='',
                )
                mapping = dc.get_message_context(aid, db_path=db)
                self.assertEqual(int(mapping['context_id']), int(open_ctx['id']))
                dr._release_lease(plan)
                uid_new = _insert(db, 'hayana', 'new day', '2026-07-27 10:00:00')
                new_day = datetime.datetime(2026, 7, 27, 10, 0, 0)
                before_count = int(sqlite3.connect(db).execute(
                    'SELECT COUNT(*) FROM daily_contexts',
                ).fetchone()[0])
                plan_new = _prepare_turn(db, uid_new, wall_now=new_day)
                after_count = int(sqlite3.connect(db).execute(
                    'SELECT COUNT(*) FROM daily_contexts',
                ).fetchone()[0])
                self.assertEqual(plan_new.context_id, int(open_ctx['id']))
                self.assertEqual(before_count, after_count)
                dr._release_lease(plan_new)
        finally:
            os.unlink(db)

    def test_now_parameter_does_not_implicitly_set_wall_clock(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            user_time = datetime.datetime(2026, 7, 27, 3, 59, 59)
            wall_time = datetime.datetime(2026, 7, 27, 4, 0, 1)
            uid = _insert(db, 'hayana', 'wall split', user_time.strftime('%Y-%m-%d %H:%M:%S'))
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = dr.prepare_daily_turn(
                    user_message_id=uid,
                    db_path=db,
                    now=user_time,
                    wall_now=wall_time,
                    static_system='S',
                )
            self.assertEqual(plan.origin_local_day, '2026-07-26')
            open_ctx = cw.get_current_context_window(db_path=db, now=wall_time)
            self.assertEqual(plan.context_id, int(open_ctx['id']))
            dr._release_lease(plan)
        finally:
            os.unlink(db)

    def test_reprepare_keeps_origin_day_across_midnight(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            origin = datetime.datetime(2026, 7, 27, 3, 59, 59)
            uid = _insert(db, 'hayana', 'late', origin.strftime('%Y-%m-%d %H:%M:%S'))
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})):
                plan = _prepare_turn(db, uid, now=origin, wall_now=origin)
                self.assertEqual(plan.origin_local_day, '2026-07-26')
                dr._release_lease(plan)
                with mock.patch.object(dr, 'prepare_daily_turn', wraps=dr.prepare_daily_turn) as prep:
                    dr.reprepare_after_hot_cold_mismatch(plan, resident=None, static_system='S')
                reprep_kwargs = prep.call_args.kwargs
                self.assertEqual(reprep_kwargs.get('origin_local_day'), '2026-07-26')
                self.assertEqual(reprep_kwargs.get('now'), origin)
        finally:
            os.unlink(db)


class DailyRuntimeMembershipHistoryTests(unittest.TestCase):
    def test_midday_enable_keeps_morning_unmapped_history(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            morning = _insert(db, 'hayana', 'morning msg', '2026-07-27 08:00:00')
            noon = _insert(db, 'hayana', 'noon msg', '2026-07-27 12:01:00')
            ctx = dc.get_or_create_daily_context(
                chat_id='mid', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            dc.record_daily_message_context(
                noon, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            from chat import daily_history as dh
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='mid',
                    daily_context=ctx,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            hist_ids = [m['message_id'] for m in built.get('current_day_history') or []]
            self.assertIn(morning, hist_ids)
            self.assertIn(noon, hist_ids)
        finally:
            os.unlink(db)

    def test_cross_midnight_assistant_in_carryover_candidates(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            start = datetime.datetime(2026, 7, 27, 3, 59, 59)
            finish = datetime.datetime(2026, 7, 27, 4, 1, 0)
            old_ctx = dc.get_or_create_daily_context(
                chat_id='co', local_day='2026-07-26', db_path=db,
                now=start, allow_backfill=True,
            )
            uid = _insert(db, 'hayana', 'late user', start.strftime('%Y-%m-%d %H:%M:%S'))
            dc.record_daily_message_context(
                uid, context_id=int(old_ctx['id']), context_epoch=int(old_ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            aid = _insert(db, 'assistant', 'late reply', finish.strftime('%Y-%m-%d %H:%M:%S'))
            dc.record_daily_message_context(
                aid, context_id=int(old_ctx['id']), context_epoch=int(old_ctx['context_epoch']),
                resident_generation=1, role='assistant', db_path=db,
            )
            new_ctx = dc.get_or_create_daily_context(
                chat_id='co', local_day='2026-07-27', db_path=db, now=finish,
            )
            self.assertGreater(int(new_ctx['boundary_message_id']), 0)
            self.assertEqual(int(new_ctx['boundary_message_id']), uid)
            self.assertGreater(aid, uid)
            from chat import daily_history as dh
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='co', daily_context=new_ctx, static_system='S',
                    is_cold=True, db_path=db,
                )
            hist_ids = [m['message_id'] for m in built.get('current_day_history') or []]
            self.assertNotIn(aid, hist_ids)
            cands = dc.list_carryover_candidates(int(new_ctx['id']), limit=10, db_path=db)
            cand_ids = [c['message_id'] for c in cands]
            self.assertIn(aid, cand_ids)
            for count in (3, 5, 10):
                db2 = _tmp_db()
                try:
                    _init_chat_messages(db2)
                    old2 = dc.get_or_create_daily_context(
                        chat_id='co%d' % count, local_day='2026-07-26', db_path=db2,
                        now=start, allow_backfill=True,
                    )
                    u2 = _insert(db2, 'hayana', 'late user', start.strftime('%Y-%m-%d %H:%M:%S'))
                    dc.record_daily_message_context(
                        u2, context_id=int(old2['id']), context_epoch=int(old2['context_epoch']),
                        resident_generation=1, role='user', db_path=db2,
                    )
                    a2 = _insert(db2, 'assistant', 'late reply', finish.strftime('%Y-%m-%d %H:%M:%S'))
                    dc.record_daily_message_context(
                        a2, context_id=int(old2['id']), context_epoch=int(old2['context_epoch']),
                        resident_generation=1, role='assistant', db_path=db2,
                    )
                    new2 = dc.get_or_create_daily_context(
                        chat_id='co%d' % count, local_day='2026-07-27', db_path=db2, now=finish,
                    )
                    dc.select_carryover(int(new2['id']), count, db_path=db2)
                    selected = [
                        m['message_id']
                        for m in dc.get_selected_carryover_messages(int(new2['id']), db_path=db2)
                    ]
                    self.assertIn(a2, selected)
                finally:
                    os.unlink(db2)
        finally:
            os.unlink(db)

    def test_queued_turn_cold_history_includes_prior_assistant(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='queue', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            uid_a = _insert(db, 'hayana', 'user a', '2026-07-27 10:00:00')
            uid_b = _insert(db, 'hayana', 'user b', '2026-07-27 10:01:00')
            aid_a = _insert(db, 'assistant', 'reply a', '2026-07-27 10:02:00')
            dc.record_daily_message_context(
                uid_a, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            dc.record_daily_message_context(
                aid_a, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='assistant', db_path=db,
            )
            dc.record_daily_message_context(
                uid_b, context_id=int(ctx['id']), context_epoch=int(ctx['context_epoch']),
                resident_generation=1, role='user', db_path=db,
            )
            from chat import daily_history as dh
            with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                built = dh.build_daily_window_context(
                    chat_id='queue',
                    daily_context=ctx,
                    current_user_message_id=uid_b,
                    static_system='S',
                    is_cold=True,
                    db_path=db,
                )
            hist_ids = [m['message_id'] for m in built.get('current_day_history') or []]
            self.assertIn(uid_a, hist_ids)
            self.assertIn(aid_a, hist_ids)
            self.assertNotIn(uid_b, hist_ids)
        finally:
            os.unlink(db)


class DailyRuntimeBindingFailClosedTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_close_with_expected_key_and_no_binding_is_noop(self):
        resident = _FakeResident()
        resident._alive = True
        self.assertFalse(dr.close_local_resident_if_bound(resident, expected_key='daily:x:1:1'))
        self.assertEqual(resident.killed, 0)

    def test_hot_requires_matching_process_generation(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'pg', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('S', 'snap', {})):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW, static_system='S')
                resident = _FakeResident()
                resident.generation = 2
                dr.set_local_binding(_binding_for_plan(plan, cursor=0))
                binding = dr.get_local_binding()
                assert binding is not None
                binding.bound_cursor_message_id = 0
                dc.upsert_resident_owner(
                    plan.context_id, plan.resident_generation,
                    worker_id=dr.WORKER_ID, resident_key=plan.resident_key,
                    bound_cursor_message_id=0, process_generation=2, db_path=db,
                )
                self.assertFalse(plan.is_cold is False and plan.manifest.get('turn_kind') == 'hot')
        finally:
            os.unlink(db)


class DailyRuntimeHeartbeatKillTests(unittest.TestCase):
    def test_heartbeat_terminal_failure_after_done_yield(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'tail-fail', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW)
                resident = _FakeResident()
                hb_inst = mock.MagicMock()
                hb_inst.failed = False
                hb_inst.stop.return_value = True
                with mock.patch.object(dr, 'LeaseHeartbeat', return_value=hb_inst):
                    with self.assertRaises(dr.LeaseHeartbeatTerminalFailure):
                        list(dr.stream_daily_resident_turn(
                            plan, resident=resident, env={}, static_system='S',
                        ))
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(int(count), 0)
            self.assertEqual(len(resident.sent), 1)
        finally:
            os.unlink(db)

    def test_renew_rejects_missing_owner_row(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            ctx = dc.get_or_create_daily_context(
                chat_id='renew', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            cid, gen = int(ctx['id']), int(ctx['resident_generation'])
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            dc.acquire_resident_turn_lease(
                cid, gen, lease_owner='owner-a', request_message_id=1,
                db_path=db, now=now,
            )
            token = dc.make_epoch_token(
                chat_id='renew', context_id=cid, context_epoch=int(ctx['context_epoch']), resident_generation=gen,
            )
            with self.assertRaises(dc.ConflictError):
                dc.renew_resident_turn_lease(
                    cid, gen, lease_owner='owner-a', db_path=db,
                    now=now, epoch_token=token,
                )
        finally:
            os.unlink(db)

    def test_blocking_resident_killed_on_renew_failure(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
            uid = _insert(db, 'hayana', 'block', now.strftime('%Y-%m-%d %H:%M:%S'))

            class _BlockingResident(_FakeResident):
                def send_turn(self, content, commit_meta=None, turn_lease=None):
                    deadline = time.time() + 0.5
                    while time.time() < deadline:
                        if not self._alive:
                            raise RuntimeError('resident killed during stream')
                        time.sleep(0.01)
                    yield ('done', ('late', '', {}, {}))

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
                 mock.patch('chat.daily_context.renew_resident_turn_lease', side_effect=dc.ConflictError('stale')):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW, static_system='S')
                resident = _BlockingResident()
                with mock.patch.object(dr, 'LEASE_HEARTBEAT_INTERVAL', 0.02):
                    with self.assertRaises((dr.LeaseConflictError, RuntimeError)):
                        list(dr.stream_daily_resident_turn(
                            plan, resident=resident, env={}, static_system='S',
                        ))
            self.assertGreaterEqual(resident.killed, 1)
        finally:
            os.unlink(db)


class _BlockingAfterTextResident(_FakeResident):
    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        yield ('text', 'partial')
        deadline = time.time() + 30
        while time.time() < deadline and self._alive:
            time.sleep(0.01)
        yield ('done', ('never', '', {}, {}))


class _BlockBeforeYieldResident(_FakeResident):
    def send_turn(self, content, commit_meta=None, turn_lease=None):
        deadline = time.time() + 30
        while time.time() < deadline and self._alive:
            time.sleep(0.01)
        yield ('text', 'never')
        yield ('done', ('never', '', {}, {}))


class GatewayClientDisconnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('/opt/workspace/tools', exist_ok=True)

    def _disconnect_stream_patches(self, db, uid, resident, *, stream_impl):
        import gateway

        gen_release_calls: list = []
        release_turn_calls: list = []
        memo_mock = mock.Mock()
        moments_mock = mock.Mock()
        scoring_mock = mock.Mock()

        def _track_gen_release(value):
            gen_release_calls.append(value)

        def _track_release_turn(*_a, **kwargs):
            release_turn_calls.append(kwargs)

        def _prepare_real(**kwargs):
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                return _REAL_PREPARE_DAILY_TURN(
                    user_message_id=kwargs.get('user_message_id', uid),
                    db_path=db,
                    resident=resident,
                    static_system=kwargs.get('static_system', 'S'),
                    wall_now=_FIXED_NOW,
                )

        patches = [
            mock.patch.object(config_store, 'get_bool', return_value=True),
            mock.patch('chat.daily_context.enabled', return_value=True),
            mock.patch.object(gateway, 'DB_PATH', db),
            mock.patch.object(gateway, '_get_provider', return_value='claude_code'),
            mock.patch('gateway._gen_acquire_or_wait', return_value=('new', None)),
            mock.patch.object(gateway, '_gen_release', side_effect=_track_gen_release),
            mock.patch('moments_turn.prepare_turn', return_value={'content': 'hi'}),
            mock.patch(
                'moments_turn.insert_user_message',
                return_value={'content': 'hi', 'user_message_id': uid},
            ),
            mock.patch('moments_turn.activate_turn', side_effect=lambda td, **_k: td),
            mock.patch('moments_turn.release_turn', side_effect=_track_release_turn),
            mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                'persona': 'P', 'full_system': 'STATIC',
            }),
            mock.patch.object(dr, 'prepare_daily_turn', side_effect=_prepare_real),
            mock.patch.object(
                dr,
                'build_canonical_turn_for_plan',
                side_effect=_gateway_canonical_builder(),
            ),
            mock.patch.object(gateway, '_write_session_memo', memo_mock),
            mock.patch('moments_persistence.after_assistant_persisted', moments_mock),
            mock.patch('chat.scoring_identity.trigger_turn_scoring', scoring_mock),
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch('gateway.get_db', return_value=sqlite3.connect(db)),
        ]
        if stream_impl is not None:
            patches.append(mock.patch.object(dr, 'stream_daily_resident_turn', side_effect=stream_impl))
        return gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock

    def test_chat_stream_disconnect_after_text_aborts_daily_turn(self):
        import gateway

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'disconnect', '2026-07-27 10:00:00')
            resident = _BlockingAfterTextResident()
            gen_release_calls: list = []
            release_turn_calls: list = []
            memo_mock = mock.Mock()
            moments_mock = mock.Mock()
            scoring_mock = mock.Mock()

            def _prepare_real(**kwargs):
                with mock.patch.object(config_store, 'get_bool', return_value=True), \
                     mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    return _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid,
                        db_path=db,
                        resident=resident,
                        static_system='S',
                        wall_now=_FIXED_NOW,
                    )

            patches = [
                mock.patch.object(config_store, 'get_bool', return_value=True),
                mock.patch('chat.daily_context.enabled', return_value=True),
                mock.patch.object(gateway, 'DB_PATH', db),
                mock.patch.object(gateway, '_get_provider', return_value='claude_code'),
                mock.patch('gateway._gen_acquire_or_wait', return_value=('new', None)),
                mock.patch.object(gateway, '_gen_release', side_effect=lambda v: gen_release_calls.append(v)),
                mock.patch('moments_turn.prepare_turn', return_value={'content': 'hi'}),
                mock.patch(
                    'moments_turn.insert_user_message',
                    return_value={'content': 'hi', 'user_message_id': uid},
                ),
                mock.patch('moments_turn.activate_turn', side_effect=lambda td, **_k: td),
                mock.patch('moments_turn.release_turn', side_effect=lambda *a, **k: release_turn_calls.append(k)),
                mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                    'persona': 'P', 'full_system': 'STATIC',
                }),
                mock.patch.object(dr, 'prepare_daily_turn', side_effect=_prepare_real),
                mock.patch.object(gateway, '_write_session_memo', memo_mock),
                mock.patch('moments_persistence.after_assistant_persisted', moments_mock),
                mock.patch('chat.scoring_identity.trigger_turn_scoring', scoring_mock),
                mock.patch.object(gateway, '_CC_RESIDENT', resident),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                body_iter = resp.response
                _ = next(body_iter)
                body_iter.close()

            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            conn = sqlite3.connect(db)
            assistant_row = conn.execute(
                "SELECT id, content, cache_info FROM chat_messages WHERE author='assistant'"
            ).fetchone()
            assistant_count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            mapping_count = conn.execute(
                "SELECT COUNT(*) FROM daily_message_contexts dmc "
                "INNER JOIN chat_messages m ON m.id=dmc.message_id "
                "WHERE m.author='assistant'"
            ).fetchone()[0]
            conn.close()
            self.assertIsNotNone(ctx)
            self.assertEqual(int(assistant_count), 1)
            self.assertEqual(assistant_row[1], 'partial')
            self.assertIn('stream_interrupted', str(assistant_row[2] or ''))
            self.assertEqual(int(mapping_count), 1)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), 1, db_path=db, now=_FIXED_NOW,
            ))
            self.assertGreaterEqual(resident.killed, 1)
            self.assertGreater(int(ctx['resident_generation']), 1)
            self.assertEqual(gen_release_calls, [None])
            self.assertTrue(release_turn_calls)
            self.assertTrue(all(c.get('persisted') for c in release_turn_calls))
            memo_mock.assert_not_called()
            moments_mock.assert_not_called()
            scoring_mock.assert_not_called()
            cursor_after = dc.get_resident_history_cursor(
                int(ctx['id']), int(ctx['resident_generation']), db_path=db,
            )
            self.assertIsNone(cursor_after)
        finally:
            os.unlink(db)

    def test_stream_generator_close_aborts_daily_turn(self):
        import gateway

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'gen-close', '2026-07-27 10:00:00')
            resident = _BlockingAfterTextResident()

            def _prepare_real(**kwargs):
                with mock.patch.object(config_store, 'get_bool', return_value=True), \
                     mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    return _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid,
                        db_path=db,
                        resident=resident,
                        static_system='S',
                        wall_now=_FIXED_NOW,
                    )

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_context.enabled', return_value=True), \
                 mock.patch.object(gateway, 'DB_PATH', db), \
                 mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                     'persona': 'P', 'full_system': 'STATIC',
                 }), \
                 mock.patch.object(dr, 'prepare_daily_turn', side_effect=_prepare_real), \
                 mock.patch.object(gateway, '_CC_RESIDENT', resident):
                gen = gateway._stream_cc_daily_soft_window({'user_message_id': uid}, 'gen-close')
                _ = next(gen)
                gen.close()

            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            self.assertIsNotNone(ctx)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), 1, db_path=db, now=_FIXED_NOW,
            ))
            self.assertGreaterEqual(resident.killed, 1)
        finally:
            os.unlink(db)

    def test_chat_stream_disconnect_before_provider_event_aborts(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'early-disc', '2026-07-27 10:00:00')
            resident = _BlockBeforeYieldResident()
            gen_release_calls: list = []

            def _prepare_real(**kwargs):
                with mock.patch.object(config_store, 'get_bool', return_value=True), \
                     mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    return _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid,
                        db_path=db,
                        resident=resident,
                        static_system='S',
                        wall_now=_FIXED_NOW,
                    )

            gateway, patches, _, release_turn_calls, memo_mock, moments_mock, scoring_mock = (
                self._disconnect_stream_patches(
                    db, uid, resident, stream_impl=None,
                )
            )
            patches = [
                p for p in patches
                if getattr(p, 'attribute', '') != 'prepare_daily_turn'
            ]
            patches.extend([
                mock.patch.object(dr, 'prepare_daily_turn', side_effect=_prepare_real),
                mock.patch.object(gateway, '_CC_RESIDENT', resident),
            ])
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                resp.response.close()

            ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            self.assertIsNotNone(ctx)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), 1, db_path=db, now=_FIXED_NOW,
            ))
            self.assertGreaterEqual(resident.killed, 1)
            memo_mock.assert_not_called()
        finally:
            os.unlink(db)

    def test_success_path_not_double_aborted_in_finally(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'ok', '2026-07-27 10:00:00')
            resident = _FakeResident()

            def _stream_ok(plan, *, resident, env, static_system):
                yield from _FakeResident().send_turn('', commit_meta=None)

            gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock = (
                self._disconnect_stream_patches(db, uid, resident, stream_impl=_stream_ok)
            )
            abort_mock = mock.Mock(wraps=dr.abort_daily_turn)
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(dr, 'abort_daily_turn', abort_mock))
                stack.enter_context(mock.patch.object(dr, 'persist_daily_assistant_for_plan', return_value=42))
                stack.enter_context(mock.patch.object(dr, 'handle_provider_success', return_value={'ok': True}))
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                list(resp.response)

            abort_mock.assert_not_called()
            self.assertEqual(gen_release_calls, [('daily reply', '')])
            self.assertNotIn(None, gen_release_calls)
            self.assertTrue(release_turn_calls)
            self.assertTrue(all(c.get('persisted') for c in release_turn_calls))
        finally:
            os.unlink(db)


def _sse_events_from_chunks(chunks) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        text = chunk.decode('utf-8') if isinstance(chunk, bytes) else str(chunk)
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('data: '):
                events.append(json.loads(line[6:].strip()))
    return events


class GatewaySuccessHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('/opt/workspace/tools', exist_ok=True)

    def _success_stream_patches(self, db, uid, resident):
        import gateway

        gen_release_calls: list = []
        release_turn_calls: list = []
        memo_mock = mock.Mock()
        moments_mock = mock.Mock()
        scoring_mock = mock.Mock()

        def _prepare_real(**kwargs):
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                return _REAL_PREPARE_DAILY_TURN(
                    user_message_id=kwargs.get('user_message_id', uid),
                    db_path=db,
                    resident=resident,
                    static_system=kwargs.get('static_system', 'S'),
                    wall_now=_FIXED_NOW,
                )

        patches = [
            mock.patch.object(config_store, 'get_bool', return_value=True),
            mock.patch('chat.daily_context.enabled', return_value=True),
            mock.patch.object(gateway, 'DB_PATH', db),
            mock.patch.object(gateway, '_get_provider', return_value='claude_code'),
            mock.patch('gateway._gen_acquire_or_wait', return_value=('new', None)),
            mock.patch.object(gateway, '_gen_release', side_effect=lambda v: gen_release_calls.append(v)),
            mock.patch('moments_turn.prepare_turn', return_value={'content': 'hi'}),
            mock.patch(
                'moments_turn.insert_user_message',
                return_value={'content': 'hi', 'user_message_id': uid},
            ),
            mock.patch('moments_turn.activate_turn', side_effect=lambda td, **_k: td),
            mock.patch('moments_turn.release_turn', side_effect=lambda *a, **k: release_turn_calls.append(k)),
            mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                'persona': 'P', 'full_system': 'STATIC',
            }),
            mock.patch.object(dr, 'prepare_daily_turn', side_effect=_prepare_real),
            mock.patch.object(
                dr,
                'build_canonical_turn_for_plan',
                side_effect=_gateway_canonical_builder(),
            ),
            mock.patch.object(
                dr,
                'finalize_transcript_mapping_after_success',
                side_effect=_mark_transcript_mapped,
            ),
            mock.patch.object(gateway, '_write_session_memo', memo_mock),
            mock.patch('moments_persistence.after_assistant_persisted', moments_mock),
            mock.patch('chat.scoring_identity.trigger_turn_scoring', scoring_mock),
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch('gateway.get_db', return_value=sqlite3.connect(db)),
        ]
        return gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock

    def test_chat_stream_success_full_handoff(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'full-ok', '2026-07-27 10:00:00')
            resident = _FakeResident()
            ctx_before = dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )
            gen_before = int(ctx_before['resident_generation'])
            cursor_before = dc.get_resident_history_cursor(
                int(ctx_before['id']), gen_before, db_path=db,
            ) or 0

            gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock = (
                self._success_stream_patches(db, uid, resident)
            )
            abort_mock = mock.Mock(wraps=dr.abort_daily_turn)
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(dr, 'abort_daily_turn', abort_mock))
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                events = _sse_events_from_chunks(list(resp.response))

            conn = sqlite3.connect(db)
            assistant_row = conn.execute(
                "SELECT id, content FROM chat_messages WHERE author='assistant'"
            ).fetchone()
            assistant_count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            mapping_count = conn.execute(
                "SELECT COUNT(*) FROM daily_message_contexts dmc "
                "INNER JOIN chat_messages m ON m.id=dmc.message_id "
                "WHERE m.author='assistant'"
            ).fetchone()[0]
            conn.close()
            ctx_after = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            cursor_after = dc.get_resident_history_cursor(
                int(ctx_after['id']), gen_before, db_path=db,
            ) or 0

            self.assertEqual(int(assistant_count), 1)
            self.assertEqual(assistant_row[1], 'daily reply')
            self.assertEqual(int(mapping_count), 1)
            self.assertGreater(cursor_after, cursor_before)
            self.assertEqual(cursor_after, int(assistant_row[0]))
            self.assertEqual(int(ctx_after['resident_generation']), gen_before)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx_after['id']), gen_before, db_path=db, now=_FIXED_NOW,
            ))
            self.assertEqual(resident.killed, 0)
            abort_mock.assert_not_called()
            self.assertEqual(gen_release_calls, [('daily reply', '')])
            self.assertNotIn(None, gen_release_calls)
            self.assertTrue(release_turn_calls)
            self.assertTrue(all(c.get('persisted') for c in release_turn_calls))
            memo_mock.assert_called_once()
            moments_mock.assert_called_once()
            scoring_mock.assert_called_once()
            done_evt = next(e for e in events if e.get('t') == 'done')
            self.assertTrue(done_evt.get('ok'))
        finally:
            os.unlink(db)

    def test_chat_stream_success_disconnect_after_usage(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'usage-disc', '2026-07-27 10:00:00')
            resident = _FakeResident()
            gen_before = int(dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )['resident_generation'])

            gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock = (
                self._success_stream_patches(db, uid, resident)
            )
            abort_mock = mock.Mock(wraps=dr.abort_daily_turn)
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(dr, 'abort_daily_turn', abort_mock))
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                body_iter = resp.response
                for chunk in body_iter:
                    for evt in _sse_events_from_chunks([chunk]):
                        if evt.get('t') == 'usage':
                            body_iter.close()
                            break
                    else:
                        continue
                    break

            ctx_after = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            conn = sqlite3.connect(db)
            assistant_count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(int(assistant_count), 1)
            self.assertEqual(int(ctx_after['resident_generation']), gen_before)
            self.assertEqual(resident.killed, 0)
            abort_mock.assert_not_called()
            self.assertEqual(gen_release_calls, [('daily reply', '')])
            self.assertTrue(all(c.get('persisted') for c in release_turn_calls))
            memo_mock.assert_called_once()
            moments_mock.assert_called_once()
            scoring_mock.assert_called_once()
        finally:
            os.unlink(db)

    def test_chat_stream_success_disconnect_after_done(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'done-disc', '2026-07-27 10:00:00')
            resident = _FakeResident()
            gen_before = int(dc.get_or_create_daily_context(
                chat_id='default', local_day='2026-07-27', db_path=db, now=_FIXED_NOW,
            )['resident_generation'])

            gateway, patches, gen_release_calls, release_turn_calls, memo_mock, moments_mock, scoring_mock = (
                self._success_stream_patches(db, uid, resident)
            )
            abort_mock = mock.Mock(wraps=dr.abort_daily_turn)
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(dr, 'abort_daily_turn', abort_mock))
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                body_iter = resp.response
                for chunk in body_iter:
                    for evt in _sse_events_from_chunks([chunk]):
                        if evt.get('t') == 'done' and evt.get('ok') is True:
                            body_iter.close()
                            break
                    else:
                        continue
                    break

            ctx_after = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
            conn = sqlite3.connect(db)
            assistant_count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(int(assistant_count), 1)
            self.assertEqual(int(ctx_after['resident_generation']), gen_before)
            self.assertEqual(resident.killed, 0)
            abort_mock.assert_not_called()
            self.assertEqual(gen_release_calls, [('daily reply', '')])
            self.assertTrue(all(c.get('persisted') for c in release_turn_calls))
            memo_mock.assert_called_once()
            moments_mock.assert_called_once()
            scoring_mock.assert_called_once()
        finally:
            os.unlink(db)


class GatewayDailyCasSseTests(unittest.TestCase):
    def test_daily_tool_visibility_emits_and_persists_result(self):
        import gateway

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', '读取一下待办', '2026-07-27 10:00:00')
            plan = mock.MagicMock()
            plan.resident_key = 'daily:default:1:1'
            plan.manifest = {}
            persisted = {}

            def _fake_stream(plan_arg, *, resident, env, static_system):
                yield ('tool_use', {
                    'id': 't-visible-1',
                    'name': 'mcp__home__get_todos',
                    'args': {},
                })
                yield ('tool_result', {
                    'tool_use_id': 't-visible-1',
                    'result': '{"ok":true}',
                    'is_error': False,
                })
                yield ('text', '工具读取完成')
                yield ('done', ('工具读取完成', '', {}, {}))

            def _persist(*_args, **kwargs):
                persisted['tool_calls'] = kwargs['tool_calls']
                return 4242

            patches = [
                mock.patch.object(config_store, 'get_bool', return_value=True),
                mock.patch('chat.daily_context.enabled', return_value=True),
                mock.patch.object(gateway, 'DB_PATH', db),
                mock.patch(
                    'chat.system_builder.build_cc_daily_static_parts',
                    return_value={'persona': 'P', 'full_system': 'STATIC'},
                ),
                mock.patch.object(dr, 'prepare_daily_turn', return_value=plan),
                mock.patch.object(
                    dr,
                    'build_canonical_turn_for_plan',
                    side_effect=_gateway_canonical_builder(
                        content='工具读取完成',
                        tool_calls=(
                            {
                                'id': 't-visible-1',
                                'name': 'mcp__home__get_todos',
                                'args': {},
                                'result': '{"ok":true}',
                                'success': True,
                            },
                        ),
                    ),
                ),
                mock.patch.object(
                    dr,
                    'stream_daily_resident_turn',
                    side_effect=_fake_stream,
                ),
                mock.patch.object(
                    dr,
                    'persist_daily_assistant_for_plan',
                    side_effect=_persist,
                ),
                mock.patch.object(
                    dr,
                    'handle_provider_success',
                    return_value={'cursor_cas_success': True},
                ),
                mock.patch.object(gateway, '_CC_RESIDENT', mock.MagicMock()),
                mock.patch.object(gateway, '_write_session_memo'),
                mock.patch('moments_persistence.after_assistant_persisted'),
                mock.patch('chat.scoring_identity.trigger_turn_scoring'),
            ]
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                chunks = list(
                    gateway._stream_cc_daily_soft_window(
                        {'user_message_id': uid},
                        {'content': '读取一下待办'},
                    )
                )

            events = _sse_events_from_chunks(chunks)
            tool_use = next(e for e in events if e.get('t') == 'tool_use')
            tool_result = next(e for e in events if e.get('t') == 'tool_result')
            self.assertEqual(tool_use['idx'], 0)
            self.assertEqual(tool_result['idx'], tool_use['idx'])
            self.assertEqual(tool_result['d']['name'], 'mcp__home__get_todos')
            self.assertEqual(tool_result['d']['args'], {})
            self.assertEqual(tool_result['d']['result'], '{"ok":true}')
            self.assertTrue(tool_result['d']['success'])

            persisted_calls = json.loads(persisted['tool_calls'])
            self.assertEqual(len(persisted_calls), 1)
            self.assertEqual(persisted_calls[0]['name'], 'mcp__home__get_todos')
            self.assertEqual(persisted_calls[0]['result'], '{"ok":true}')
            self.assertTrue(persisted_calls[0]['success'])
        finally:
            os.unlink(db)


    def test_cursor_cas_conflict_sse_fail_closed(self):
        import json
        import gateway

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'cas-sse', '2026-07-27 10:00:00')
            turn_data = {'user_message_id': uid, 'content': 'cas-sse'}
            uc = {'content': 'cas-sse'}

            plan = mock.MagicMock()
            plan.resident_key = 'daily:default:1:1'
            plan.manifest = {}

            def _fake_stream(plan_arg, *, resident, env, static_system):
                yield ('text', 'daily reply')
                yield ('done', ('daily reply', '', {'input_tokens': 1, 'output_tokens': 1}, {}))

            manifest = {'cursor_cas_success': False, 'error_code': 'cursor_cas_conflict'}
            cas_exc = dr.CursorCASConflictAfterPersist(
                'cursor stale', assistant_message_id=999, manifest=manifest,
            )

            memo_mock = mock.Mock()
            moments_mock = mock.Mock()
            scoring_mock = mock.Mock()
            patches = [
                mock.patch.object(config_store, 'get_bool', return_value=True),
                mock.patch('chat.daily_context.enabled', return_value=True),
                mock.patch.object(gateway, 'DB_PATH', db),
                mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                    'persona': 'P', 'full_system': 'STATIC',
                }),
                mock.patch.object(dr, 'prepare_daily_turn', return_value=plan),
                mock.patch.object(
                    dr,
                    'build_canonical_turn_for_plan',
                    side_effect=_gateway_canonical_builder(),
                ),
                mock.patch.object(dr, 'stream_daily_resident_turn', side_effect=_fake_stream),
                mock.patch.object(dr, 'persist_daily_assistant_for_plan', return_value=999),
                mock.patch.object(dr, 'handle_provider_success', side_effect=cas_exc),
                mock.patch.object(gateway, '_write_session_memo', memo_mock),
                mock.patch('moments_persistence.after_assistant_persisted', moments_mock),
                mock.patch('chat.scoring_identity.trigger_turn_scoring', scoring_mock),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(mock.patch.object(gateway, '_CC_RESIDENT', mock.MagicMock()))
                gen = gateway._stream_cc_daily_soft_window(turn_data, uc)
                chunks = []
                retval = None
                while True:
                    try:
                        chunks.append(next(gen))
                    except StopIteration as exc:
                        retval = exc.value
                        break

            events = []
            for chunk in chunks:
                if chunk.startswith('data: '):
                    events.append(json.loads(chunk[6:].strip()))
            terminal_events = [e for e in events if e.get('t') in ('err', 'done')]
            err_evt = next(e for e in terminal_events if e.get('t') == 'err')
            self.assertEqual(err_evt.get('code'), 'cursor_cas_conflict')
            self.assertEqual([e.get('t') for e in terminal_events], ['err'])
            memo_mock.assert_not_called()
            moments_mock.assert_not_called()
            scoring_mock.assert_not_called()
            self.assertIsNone(retval)
        finally:
            os.unlink(db)


class GatewayChatStreamGenReleaseCasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.makedirs('/opt/workspace/tools', exist_ok=True)

    def test_chat_stream_cas_conflict_gen_release_none_only(self):
        import json
        import gateway

        db = _tmp_db()
        gen_release_calls: list = []
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'stream-cas', '2026-07-27 10:00:00')
            plan = mock.MagicMock()
            plan.resident_key = 'daily:default:1:1'
            plan.manifest = {}

            def _fake_stream(plan_arg, *, resident, env, static_system):
                yield ('text', 'daily reply')
                yield ('done', ('daily reply', '', {'input_tokens': 1, 'output_tokens': 1}, {}))

            cas_exc = dr.CursorCASConflictAfterPersist(
                'cursor stale', assistant_message_id=999,
                manifest={'cursor_cas_success': False, 'error_code': 'cursor_cas_conflict'},
            )

            memo_mock = mock.Mock()
            moments_mock = mock.Mock()
            scoring_mock = mock.Mock()

            def _track_gen_release(value):
                gen_release_calls.append(value)

            patches = [
                mock.patch.object(config_store, 'get_bool', return_value=True),
                mock.patch('chat.daily_context.enabled', return_value=True),
                mock.patch.object(gateway, 'DB_PATH', db),
                mock.patch.object(gateway, '_get_provider', return_value='claude_code'),
                mock.patch('gateway._gen_acquire_or_wait', return_value=('new', None)),
                mock.patch.object(gateway, '_gen_release', side_effect=_track_gen_release),
                mock.patch('moments_turn.prepare_turn', return_value={'content': 'hi'}),
                mock.patch(
                    'moments_turn.insert_user_message',
                    return_value={'content': 'hi', 'user_message_id': uid},
                ),
                mock.patch('moments_turn.activate_turn', side_effect=lambda td, **_k: td),
                mock.patch('moments_turn.release_turn'),
                mock.patch('chat.system_builder.build_cc_daily_static_parts', return_value={
                    'persona': 'P', 'full_system': 'STATIC',
                }),
                mock.patch.object(dr, 'prepare_daily_turn', return_value=plan),
                mock.patch.object(
                    dr,
                    'build_canonical_turn_for_plan',
                    side_effect=_gateway_canonical_builder(),
                ),
                mock.patch.object(dr, 'stream_daily_resident_turn', side_effect=_fake_stream),
                mock.patch.object(dr, 'persist_daily_assistant_for_plan', return_value=999),
                mock.patch.object(dr, 'handle_provider_success', side_effect=cas_exc),
                mock.patch.object(gateway, '_write_session_memo', memo_mock),
                mock.patch('moments_persistence.after_assistant_persisted', moments_mock),
                mock.patch('chat.scoring_identity.trigger_turn_scoring', scoring_mock),
                mock.patch.object(gateway, '_CC_RESIDENT', mock.MagicMock()),
                mock.patch('gateway.get_db', return_value=sqlite3.connect(db)),
            ]
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                client = gateway.app.test_client()
                resp = client.post('/chat/stream', json={'content': 'hi'})
                self.assertEqual(resp.status_code, 200)
                body = b''.join(resp.response).decode('utf-8')

            events = []
            for line in body.split('\n'):
                if line.startswith('data: '):
                    events.append(json.loads(line[6:].strip()))
            terminal_events = [e for e in events if e.get('t') in ('err', 'done')]
            err_evt = next(e for e in terminal_events if e.get('t') == 'err')
            self.assertEqual(err_evt.get('code'), 'cursor_cas_conflict')
            self.assertEqual([e.get('t') for e in terminal_events], ['err'])
            self.assertEqual(gen_release_calls, [None])
            success_tuples = [c for c in gen_release_calls if isinstance(c, tuple)]
            self.assertEqual(success_tuples, [])
            memo_mock.assert_not_called()
            moments_mock.assert_not_called()
            scoring_mock.assert_not_called()
        finally:
            os.unlink(db)


class DailyRuntimeCursorCASTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def test_cas_conflict_raises_and_keeps_single_assistant(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'cas', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW, static_system='S')
                aid = dr.persist_daily_assistant_for_plan(plan, content='saved once')
                with mock.patch(
                    'chat.daily_context.finalize_daily_assistant_and_advance_cursor',
                    side_effect=dc.ConflictError('cursor stale'),
                ):
                    with self.assertRaises(dr.CursorCASConflictAfterPersist) as ctx:
                        dr.complete_daily_turn(plan, assistant_message_id=aid)
                self.assertEqual(ctx.exception.assistant_message_id, aid)
            conn = sqlite3.connect(db)
            count = conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(int(count), 1)
            conn = sqlite3.connect(db)
            source_kind = conn.execute(
                'SELECT source_kind FROM chat_messages WHERE author=?',
                ('assistant',),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(source_kind, dc.SOURCE_KIND_DAILY_PENDING)
        finally:
            os.unlink(db)

    def test_handle_cursor_conflict_rolls_back_pending_terminalization(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'cas through handler', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW, static_system='S')
                _attach_successful_canonical(plan, content='saved then rolled back')
                aid = dr.persist_daily_assistant_for_plan(
                    plan, content='saved then rolled back',
                )
                with mock.patch.object(
                    dr,
                    'finalize_transcript_mapping_after_success',
                    side_effect=_mark_transcript_mapped,
                ), mock.patch.object(
                    dc,
                    'finalize_daily_assistant_and_advance_cursor',
                    side_effect=dc.ConflictError('cursor stale'),
                ):
                    with self.assertRaises(dr.CursorCASConflictAfterPersist):
                        dr.handle_provider_success(
                            plan,
                            assistant_message_id=aid,
                            raw_text='saved then rolled back',
                        )

            conn = sqlite3.connect(db)
            self.assertIsNone(conn.execute(
                'SELECT id FROM chat_messages WHERE id=?', (aid,),
            ).fetchone())
            self.assertEqual(conn.execute(
                'SELECT COUNT(*) FROM daily_message_contexts WHERE message_id=?',
                (aid,),
            ).fetchone()[0], 0)
            conn.close()
            self.assertIsNone(dc.get_resident_history_cursor(
                plan.context_id, plan.resident_generation, db_path=db,
            ))
        finally:
            os.unlink(db)

    def test_generation_race_is_rejected_inside_finalize_transaction(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'generation race', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _prepare_turn(db, uid, wall_now=_FIXED_NOW, static_system='S')
                aid = dr.persist_daily_assistant_for_plan(plan, content='pending')
                conn = sqlite3.connect(db)
                conn.execute(
                    'UPDATE daily_contexts SET resident_generation=resident_generation+1 '
                    'WHERE id=?', (int(plan.context_id),),
                )
                conn.commit()
                conn.close()
                with self.assertRaises(dc.ConflictError):
                    dc.finalize_daily_assistant_and_advance_cursor(
                        plan.context_id,
                        plan.resident_generation,
                        aid,
                        expected_cursor=plan.cursor_before,
                        expected_context_epoch=plan.context_epoch,
                        db_path=db,
                    )
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute(
                    'SELECT source_kind FROM chat_messages WHERE id=?', (aid,),
                ).fetchone()[0],
                dc.SOURCE_KIND_DAILY_PENDING,
            )
            self.assertIsNone(conn.execute(
                'SELECT history_cursor_message_id FROM daily_resident_cursors '
                'WHERE context_id=? AND resident_generation=?',
                (plan.context_id, plan.resident_generation),
            ).fetchone())
            conn.close()
        finally:
            os.unlink(db)


# ---------------------------------------------------------------------------
# Runtime → Session Registry / Mapping R1 focused cases
# ---------------------------------------------------------------------------

_MAP_SESSION_HOT = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
_MAP_SESSION_COLD = '11111111-2222-3333-4444-555555555555'
_MAP_SESSION_AFTER_RESPAWN = '99999999-8888-7777-6666-555555555555'


def _jsonl_line(
    uuid: str,
    typ: str,
    *,
    session: str,
    parent: Optional[str],
    content: Any,
) -> str:
    obj = {
        'type': typ,
        'uuid': uuid,
        'parentUuid': parent,
        'cwd': '/tmp/synth',
        'sessionId': session,
        'message': {
            'role': 'user' if typ == 'user' else 'assistant',
            'content': content,
        },
    }
    return json.dumps(obj, ensure_ascii=False)


def _jsonl_result_line(uuid: str, *, session: str, stop_reason: str = 'end_turn') -> str:
    return json.dumps({
        'type': 'result',
        'uuid': uuid,
        'cwd': '/tmp/synth',
        'sessionId': session,
        'is_error': False,
        'stop_reason': stop_reason,
    }, ensure_ascii=False)


def _append_jsonl(path: Path, lines: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    with path.open('ab') as fh:
        fh.write(raw)
    return path.stat().st_size


def _write_jsonl(path: Path, lines: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    path.write_bytes(raw)
    return len(raw)


class _TranscriptHotResident(_FakeResident):
    """Hot resident with pre-existing session + JSONL history prefix."""

    def __init__(self, *, cwd: str, session_id: str, jsonl_path: Path, generation: int = 1):
        super().__init__()
        self.cwd = cwd
        self.session_id = session_id
        self.generation = generation
        self._jsonl_path = jsonl_path
        self._alive = True
        self._cold = False
        self.sent_generations: list[int] = []

    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        self._spawn_args.append((system_text, tool_profile))
        self._alive = True
        self.tool_profile = tool_profile
        return False

    def peek_respawn_reason(self, system_text, *, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        return None

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        self.sent_generations.append(int(self.generation))
        _append_jsonl(self._jsonl_path, [
            _jsonl_line('u-hot-1', 'user', session=self.session_id, parent=None, content='hot-user'),
            _jsonl_line(
                'a-hot-1', 'assistant', session=self.session_id, parent='u-hot-1',
                content=[{'type': 'text', 'text': 'hot-asst'}],
            ),
            _jsonl_result_line('r-hot-1', session=self.session_id),
        ])
        yield ('text', 'hot reply')
        yield ('done', ('hot reply', '', {'input_tokens': 3, 'output_tokens': 5}, {}))


class _TranscriptColdResident(_FakeResident):
    """Cold resident: session id empty before send; set + write JSONL before done."""

    def __init__(self, *, cwd: str, new_session_id: str, jsonl_path: Path, generation: int = 1):
        super().__init__()
        self.cwd = cwd
        self.session_id = None
        self.generation = generation
        self._new_session_id = new_session_id
        self._jsonl_path = jsonl_path

    def peek_respawn_reason(self, system_text, *, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        return None

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        yield ('text', 'cold reply')
        self.session_id = self._new_session_id
        _write_jsonl(self._jsonl_path, [
            _jsonl_line('u-cold-1', 'user', session=self.session_id, parent=None, content='cold-user'),
            _jsonl_line(
                'a-cold-1', 'assistant', session=self.session_id, parent='u-cold-1',
                content=[{'type': 'text', 'text': 'cold-asst'}],
            ),
            _jsonl_result_line('r-cold-1', session=self.session_id),
        ])
        yield ('done', ('cold reply', '', {'input_tokens': 2, 'output_tokens': 4}, {}))


class _TranscriptBacklogResident(_FakeResident):
    """One session that appends tool rounds so catch-up spans old and current turns."""

    def __init__(self, *, cwd: str, session_id: str, jsonl_path: Path):
        super().__init__()
        self.cwd = cwd
        self.session_id = None
        self.generation = 1
        self._session_id = session_id
        self._jsonl_path = jsonl_path
        self._turn_count = 0

    def peek_respawn_reason(self, system_text, *, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        return None

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        self.session_id = self._session_id
        index = self._turn_count
        lines = [
            _jsonl_line(
                f'u-back-{index}', 'user', session=self.session_id,
                parent=None, content=f'back-user-{index}',
            ),
            _jsonl_line(
                f'a-back-{index}-tool', 'assistant', session=self.session_id,
                parent=f'u-back-{index}',
                content=[{
                    'type': 'tool_use', 'id': f'tool-back-{index}',
                    'name': 'Read', 'input': {'path': 'fixture.txt'},
                }],
            ),
            _jsonl_line(
                f't-back-{index}', 'user', session=self.session_id,
                parent=f'a-back-{index}-tool',
                content=[{
                    'type': 'tool_result', 'tool_use_id': f'tool-back-{index}',
                    'content': f'tool-result-{index}',
                }],
            ),
            _jsonl_line(
                f'a-back-{index}-final', 'assistant', session=self.session_id,
                parent=f't-back-{index}',
                content=[{'type': 'text', 'text': f'back-asst-{index}'}],
            ),
            _jsonl_result_line(f'r-back-{index}', session=self.session_id),
        ]
        if self._turn_count == 0:
            _write_jsonl(self._jsonl_path, lines)
        else:
            _append_jsonl(self._jsonl_path, lines)
        self._turn_count += 1
        yield ('text', 'back reply')
        yield ('done', ('back reply', '', {'input_tokens': 2, 'output_tokens': 4}, {}))


class _RegisteredRespawnResident(_FakeResident):
    """Registry exists; peek says process_dead — must bump gen before stdin."""

    def __init__(self, *, cwd: str, old_session_id: str, new_session_id: str, jsonl_path: Path):
        super().__init__()
        self.cwd = cwd
        self.session_id = old_session_id
        self.generation = 1
        self._new_session_id = new_session_id
        self._jsonl_path = jsonl_path
        self._peek_reason = 'process_dead'
        self.ensure_alive_gens: list[int] = []
        self.sent_gens: list[int] = []

    def peek_respawn_reason(self, system_text, *, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        return self._peek_reason

    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        # Track generation at ensure_alive time (must be the bumped one).
        self.ensure_alive_gens.append(int(self.generation))
        self._spawn_args.append((system_text, tool_profile))
        self._alive = True
        self.tool_profile = tool_profile
        # Simulate spawn of a new Claude session under the new generation.
        self.session_id = None
        self.generation = int(self.generation) + 1
        self._peek_reason = None
        cold = self._cold
        self._cold = False
        return cold

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        self.sent_gens.append(int(self.generation))
        yield ('text', 'after-respawn')
        self.session_id = self._new_session_id
        _write_jsonl(self._jsonl_path, [
            _jsonl_line('u-rs-1', 'user', session=self.session_id, parent=None, content='rs-user'),
            _jsonl_line(
                'a-rs-1', 'assistant', session=self.session_id, parent='u-rs-1',
                content=[{'type': 'text', 'text': 'rs-asst'}],
            ),
            _jsonl_result_line('r-rs-1', session=self.session_id),
        ])
        yield ('done', ('after-respawn', '', {'input_tokens': 1, 'output_tokens': 2}, {}))


class _MappingBlockedResident(_TranscriptColdResident):
    """Writes a terminal turn without a user event → mapping must fail closed."""

    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.sent.append(str(content))
        yield ('text', 'blocked-map reply')
        self.session_id = self._new_session_id
        # Assistant-only event: mapping rejects (candidate_user_missing).
        _write_jsonl(self._jsonl_path, [
            _jsonl_line(
                'a-only-1', 'assistant', session=self.session_id, parent=None,
                content=[{'type': 'text', 'text': 'orphan'}],
            ),
            _jsonl_result_line('r-only-1', session=self.session_id),
        ])
        yield ('done', ('blocked-map reply', '', {'input_tokens': 1, 'output_tokens': 1}, {}))


class DailyRuntimeTranscriptMappingTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.tmp = tempfile.mkdtemp(prefix='rt-map-')
        self.home = self.tmp
        self.cwd = os.path.join(self.tmp, 'proj')
        os.makedirs(self.cwd, exist_ok=True)
        self.db = os.path.join(self.tmp, 'test.db')
        _init_chat_messages(self.db)
        self._home_patch = mock.patch.dict(os.environ, {'HOME': self.home})
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        dr.reset_bindings_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare(self, uid, *, resident=None):
        return _prepare_turn(self.db, uid, resident=resident, static_system='STATIC')

    def _jsonl_for(self, session_id: str) -> Path:
        path = session_jsonl_path(self.cwd, session_id)
        assert path is not None
        return Path(path)

    def test_hot_path_maps_only_current_turn_range(self):
        """1) Hot: start=EOF before write; map only this turn; manifest=MAPPED."""
        prefix = [
            _jsonl_line('u-pre', 'user', session=_MAP_SESSION_HOT, parent=None, content='prefix'),
            _jsonl_line(
                'a-pre', 'assistant', session=_MAP_SESSION_HOT, parent='u-pre',
                content=[{'type': 'text', 'text': 'prefix-a'}],
            ),
        ]
        jsonl_path = self._jsonl_for(_MAP_SESSION_HOT)
        start_before = _write_jsonl(jsonl_path, prefix)

        # Seed cold turn with plain fake (no JSONL) to establish cursor/owner.
        uid0 = _insert(self.db, 'hayana', 'seed', '2026-07-27 09:59:00')
        seed_resident = _FakeResident()
        seed_resident.generation = 1
        plan0 = self._prepare(uid0, resident=seed_resident)
        list(dr.stream_daily_resident_turn(
            plan0, resident=seed_resident, env={}, static_system='STATIC',
        ))
        aid0 = dr.persist_daily_assistant_for_plan(plan0, content='seed-asst')
        dr.complete_daily_turn(plan0, assistant_message_id=aid0)

        register_context_claude_session(
            context_id=plan0.context_id,
            context_epoch=plan0.context_epoch,
            resident_generation=plan0.resident_generation,
            chat_id=plan0.chat_id,
            claude_session_id=_MAP_SESSION_HOT,
            cwd=self.cwd,
            source='daily_runtime',
            scan_offset=start_before,
            process_generation=1,
            db_path=self.db,
        )

        resident = _TranscriptHotResident(
            cwd=self.cwd, session_id=_MAP_SESSION_HOT, jsonl_path=jsonl_path, generation=1,
        )
        dr.set_local_binding(_binding_for_plan(plan0, cursor=aid0))
        binding = dr.get_local_binding()
        assert binding is not None
        binding.process_generation = 1
        binding.claude_session_id = _MAP_SESSION_HOT
        dr.set_local_binding(binding)

        uid = _insert(self.db, 'hayana', 'hot-map', '2026-07-27 10:00:00')
        plan = self._prepare(uid, resident=resident)
        self.assertFalse(plan.is_cold)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        self.assertEqual(plan.transcript_start_offset, start_before)
        self.assertGreaterEqual(int(plan.transcript_end_offset or -1), start_before)
        self.assertEqual(plan.transcript_claude_session_id, _MAP_SESSION_HOT)
        self.assertIsNone(plan.transcript_observation_error_code)

        canonical, aid = _persist_canonical_for_plan(plan)
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text=canonical.content,
            usage={'input_tokens': 3, 'output_tokens': 5},
        )
        self.assertEqual(out['transcript_mapping_status'], 'MAPPED')
        self.assertIsNone(out['transcript_mapping_error_code'])
        self.assertGreaterEqual(int(out['transcript_mapping_event_count']), 2)
        self.assertEqual(out['transcript_mapping_scan_offset'], plan.transcript_end_offset)

        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), int(plan.transcript_end_offset))

        conn = sqlite3.connect(self.db)
        mapped = conn.execute(
            'SELECT event_uuid FROM chat_message_claude_events ORDER BY jsonl_byte_offset'
        ).fetchall()
        cache = conn.execute(
            'SELECT cache_info FROM chat_messages WHERE id=?', (aid,),
        ).fetchone()[0]
        conn.close()
        uuids = {r[0] for r in mapped}
        self.assertIn('u-hot-1', uuids)
        self.assertIn('a-hot-1', uuids)
        self.assertNotIn('u-pre', uuids)
        self.assertNotIn('a-pre', uuids)
        self.assertNotIn('transcript_path', str(cache or ''))
        self.assertNotIn(str(jsonl_path), str(cache or ''))

    def test_cold_path_registers_from_offset_zero(self):
        """2) Cold: session empty before send; Registry at 0; user/asst mapped."""
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'cold-map', '2026-07-27 10:00:00')
        resident = _TranscriptColdResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        self.assertTrue(plan.is_cold)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        self.assertEqual(plan.transcript_start_offset, 0)
        self.assertEqual(plan.transcript_claude_session_id, _MAP_SESSION_COLD)
        self.assertEqual(plan.transcript_path, str(jsonl_path))
        self.assertGreater(int(plan.transcript_end_offset or 0), 0)
        self.assertIsNone(plan.transcript_observation_error_code)

        canonical, aid = _persist_canonical_for_plan(plan)
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text=canonical.content,
        )
        self.assertEqual(out['transcript_mapping_status'], 'MAPPED')
        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['claude_session_id'], _MAP_SESSION_COLD)
        self.assertEqual(reg['source'], 'daily_runtime')
        self.assertEqual(int(out['transcript_mapping_scan_offset']), int(plan.transcript_end_offset))

        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            'SELECT role, message_id FROM chat_message_claude_events ORDER BY role'
        ).fetchall()
        conn.close()
        roles = {r[0]: r[1] for r in rows}
        self.assertEqual(roles.get('user'), uid)
        self.assertEqual(roles.get('assistant'), aid)

    def test_current_turn_mapping_rows_rollback_after_cursor_conflict(self):
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'rollback-current', '2026-07-27 10:00:00')
        resident = _TranscriptColdResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        canonical, aid = _persist_canonical_for_plan(plan)
        with mock.patch.object(
            dc,
            'finalize_daily_assistant_and_advance_cursor',
            side_effect=dc.ConflictError('cursor stale'),
        ):
            with self.assertRaises(dr.CursorCASConflictAfterPersist):
                dr.handle_provider_success(
                    plan, assistant_message_id=aid, raw_text=canonical.content,
                )

        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                'SELECT COUNT(*) FROM daily_message_contexts WHERE message_id=?', (aid,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE id=? AND source_kind=?",
                (aid, dc.SOURCE_KIND_DAILY_PENDING),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                'SELECT COUNT(*) FROM daily_terminal_mapping_receipts',
            ).fetchone()[0],
            0,
        )
        conn.close()
        self.assertIsNone(get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        ))
        self.assertIsNone(dc.get_resident_history_cursor(
            plan.context_id, plan.resident_generation, db_path=self.db,
        ))

    def test_backlog_mapping_rollback_preserves_preexisting_rows(self):
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        resident = _TranscriptBacklogResident(
            cwd=self.cwd, session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        uid0 = _insert(self.db, 'hayana', 'backlog-0', '2026-07-27 10:00:00')
        plan0 = self._prepare(uid0, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan0, resident=resident, env={}, static_system='STATIC',
        ))
        canonical0, aid0 = _persist_canonical_for_plan(plan0)
        dr.handle_provider_success(
            plan0, assistant_message_id=aid0, raw_text=canonical0.content,
        )

        # This formal historical round is deliberately made unmapped after
        # its own success, leaving a real backlog behind the registry.
        uid1 = _insert(self.db, 'hayana', 'backlog-1', '2026-07-27 10:01:00')
        plan1 = self._prepare(uid1, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan1, resident=resident, env={}, static_system='STATIC',
        ))
        canonical1, aid1 = _persist_canonical_for_plan(plan1)
        dr.handle_provider_success(
            plan1, assistant_message_id=aid1, raw_text=canonical1.content,
        )
        conn = sqlite3.connect(self.db)
        conn.execute(
            'DELETE FROM chat_message_claude_events '
            'WHERE event_uuid IN (?,?,?,?,?)',
            (
                'u-back-1', 'a-back-1-tool', 't-back-1', 'a-back-1-final',
                'r-back-1',
            ),
        )
        conn.execute(
            'UPDATE context_claude_sessions SET scan_offset=?, '
            'last_mapped_message_id=?, scan_status=? '
            'WHERE context_id=? AND resident_generation=?',
            (
                int(plan0.transcript_end_offset), aid0, 'READY',
                plan1.context_id, plan1.resident_generation,
            ),
        )
        conn.commit()
        conn.close()

        uid2 = _insert(self.db, 'hayana', 'backlog-2', '2026-07-27 10:02:00')
        plan2 = self._prepare(uid2, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan2, resident=resident, env={}, static_system='STATIC',
        ))
        canonical2, aid2 = _persist_canonical_for_plan(plan2)
        pre_mapping_registry = dict(get_context_claude_session(
            plan2.context_id, plan2.resident_generation, db_path=self.db,
        ) or {})
        self.assertEqual(
            int(pre_mapping_registry['scan_offset']), int(plan0.transcript_end_offset),
        )

        with mock.patch.object(
            dc,
            'finalize_daily_assistant_and_advance_cursor',
            side_effect=dc.ConflictError('cursor stale'),
        ):
            with self.assertRaises(dr.CursorCASConflictAfterPersist):
                dr.handle_provider_success(
                    plan2, assistant_message_id=aid2, raw_text=canonical2.content,
                )

        conn = sqlite3.connect(self.db)
        mapped = {
            row[0] for row in conn.execute(
                'SELECT event_uuid FROM chat_message_claude_events ORDER BY event_uuid',
            ).fetchall()
        }
        self.assertEqual(mapped, {
            'u-back-0', 'a-back-0-tool', 't-back-0', 'a-back-0-final',
        })
        self.assertNotIn('u-back-1', mapped)
        self.assertNotIn('a-back-1-tool', mapped)
        self.assertNotIn('t-back-1', mapped)
        self.assertNotIn('a-back-1-final', mapped)
        self.assertNotIn('u-back-2', mapped)
        self.assertNotIn('a-back-2-tool', mapped)
        self.assertNotIn('t-back-2', mapped)
        self.assertNotIn('a-back-2-final', mapped)
        self.assertEqual(
            plan2.manifest['terminal_rollback']['deleted_event_count'], 8,
        )
        self.assertEqual(
            conn.execute(
                'SELECT scan_offset FROM context_claude_sessions '
                'WHERE context_id=? AND resident_generation=?',
                (plan2.context_id, plan2.resident_generation),
            ).fetchone()[0],
            int(plan0.transcript_end_offset),
        )
        self.assertIsNone(conn.execute(
            'SELECT id FROM chat_messages WHERE id=?', (aid2,),
        ).fetchone())
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM daily_terminal_mapping_receipts').fetchone()[0],
            0,
        )
        conn.close()
        self.assertEqual(
            dict(get_context_claude_session(
                plan2.context_id, plan2.resident_generation, db_path=self.db,
            ) or {}),
            pre_mapping_registry,
        )
        self.assertEqual(
            dc.get_resident_history_cursor(
                plan2.context_id, plan2.resident_generation, db_path=self.db,
            ),
            aid1,
        )

    def test_pending_terminal_receipt_recovers_after_crash_before_finalize(self):
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'crash recovery', '2026-07-27 10:00:00')
        resident = _TranscriptColdResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        _canonical, aid = _persist_canonical_for_plan(plan)
        out = dr.finalize_transcript_mapping_after_success(
            plan, assistant_message_id=aid,
        )
        self.assertEqual(out['transcript_mapping_status'], 'MAPPED')
        self.assertIsNotNone(getattr(plan, '_terminal_mapping_receipt_id', None))

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE daily_resident_turn_leases SET expires_at='2000-01-01 00:00:00' "
            'WHERE context_id=? AND resident_generation=?',
            (plan.context_id, plan.resident_generation),
        )
        conn.commit()
        conn.close()
        self.assertEqual(dc.recover_pending_terminalizations(db_path=self.db), 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        self.assertIsNone(conn.execute(
            'SELECT id FROM chat_messages WHERE id=?', (aid,),
        ).fetchone())
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM daily_terminal_mapping_receipts').fetchone()[0],
            0,
        )
        conn.close()
        self.assertIsNone(get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        ))

    def test_live_terminal_receipt_is_fenced_by_active_lease(self):
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'live receipt', '2026-07-27 10:00:00')
        resident = _TranscriptColdResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        _canonical, aid = _persist_canonical_for_plan(plan)
        out = dr.finalize_transcript_mapping_after_success(
            plan, assistant_message_id=aid,
        )
        self.assertEqual(out['transcript_mapping_status'], 'MAPPED')
        self.assertEqual(dc.recover_pending_terminalizations(db_path=self.db), 0)

        conn = sqlite3.connect(self.db)
        self.assertIsNotNone(conn.execute(
            'SELECT receipt_id FROM daily_terminal_mapping_receipts '
            'WHERE assistant_message_id=?', (aid,),
        ).fetchone())
        self.assertIsNotNone(conn.execute(
            "SELECT id FROM chat_messages WHERE id=? AND source_kind='daily_pending'",
            (aid,),
        ).fetchone())
        self.assertGreater(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        conn.close()

        result = dc.finalize_daily_assistant_and_advance_cursor(
            plan.context_id,
            plan.resident_generation,
            aid,
            expected_cursor=plan.cursor_before,
            expected_context_epoch=plan.context_epoch,
            terminal_receipt_id=getattr(plan, '_terminal_mapping_receipt_id'),
            db_path=self.db,
        )
        self.assertTrue(result['advanced'])
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute(
                "SELECT source_kind FROM chat_messages WHERE id=?", (aid,),
            ).fetchone()[0],
            dc.SOURCE_KIND_CHAT,
        )
        self.assertIsNone(conn.execute(
            'SELECT receipt_id FROM daily_terminal_mapping_receipts '
            'WHERE assistant_message_id=?', (aid,),
        ).fetchone())
        self.assertEqual(
            conn.execute(
                'SELECT history_cursor_message_id FROM daily_resident_cursors '
                'WHERE context_id=? AND resident_generation=?',
                (plan.context_id, plan.resident_generation),
            ).fetchone()[0],
            aid,
        )
        self.assertGreater(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        conn.close()

    def test_registered_generation_respawn_before_stdin(self):
        """3) Registered gen + peek process_dead → bump gen; caller plan adopted in place."""
        jsonl_path = self._jsonl_for(_MAP_SESSION_AFTER_RESPAWN)
        uid = _insert(self.db, 'hayana', 'respawn-map', '2026-07-27 10:00:00')
        plan = self._prepare(uid)
        original_plan = plan
        original_id = id(plan)
        old_gen = int(plan.resident_generation)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=old_gen,
            chat_id=plan.chat_id,
            claude_session_id=_MAP_SESSION_HOT,
            cwd=self.cwd,
            source='daily_runtime',
            scan_offset=0,
            process_generation=1,
            db_path=self.db,
        )
        resident = _RegisteredRespawnResident(
            cwd=self.cwd,
            old_session_id=_MAP_SESSION_HOT,
            new_session_id=_MAP_SESSION_AFTER_RESPAWN,
            jsonl_path=jsonl_path,
        )

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC',
            ))

        self.assertTrue(any(e[0] == 'done' for e in events))
        self.assertIs(plan, original_plan)
        self.assertEqual(id(plan), original_id)
        self.assertGreater(int(plan.resident_generation), old_gen)

        # ensure_alive / stdin only after reprepare (never on registered old gen).
        self.assertEqual(len(resident.ensure_alive_gens), 1)
        self.assertEqual(len(resident.sent), 1)
        self.assertEqual(resident.session_id, _MAP_SESSION_AFTER_RESPAWN)

        old_reg = get_context_claude_session(plan.context_id, old_gen, db_path=self.db)
        self.assertEqual(old_reg['claude_session_id'], _MAP_SESSION_HOT)

        # Gateway contract: persist + Mapping on the same caller-held plan object.
        canonical, aid = _persist_canonical_for_plan(plan)
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text=canonical.content,
        )
        self.assertEqual(out['transcript_mapping_status'], 'MAPPED')
        self.assertTrue(plan.lease_released)
        self.assertFalse(dc.is_resident_turn_active(
            plan.context_id, plan.resident_generation, db_path=self.db, now=_FIXED_NOW,
        ))
        new_reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNotNone(new_reg)
        self.assertEqual(new_reg['claude_session_id'], _MAP_SESSION_AFTER_RESPAWN)
        self.assertEqual(
            get_context_claude_session(plan.context_id, old_gen, db_path=self.db)['claude_session_id'],
            _MAP_SESSION_HOT,
        )
        self.assertNotEqual(old_reg['claude_session_id'], new_reg['claude_session_id'])

    def test_second_registry_mismatch_releases_lease_no_stdin(self):
        """Second door-lock mismatch → error, no stdin, no leftover active lease."""
        uid = _insert(self.db, 'hayana', 'second-mismatch', '2026-07-27 10:00:00')
        plan = self._prepare(uid)
        resident = _FakeResident()
        resident.cwd = self.cwd
        resident.session_id = _MAP_SESSION_HOT
        resident.generation = 1

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch.object(
                 dr, 'peek_registered_respawn_decision',
                 return_value={
                     'requires_respawn': True,
                     'reason': 'process_dead',
                     'registry': {'source': 'daily_runtime'},
                     'effective_system': 'STATIC',
                     'capacity_swap': False,
                 },
             ):
            with self.assertRaises(dr.DailyRuntimeError) as ctx:
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))

        self.assertEqual(ctx.exception.error_code, 'registered_session_generation_mismatch')
        self.assertEqual(len(resident.sent), 0)
        self.assertTrue(plan.lease_released)
        self.assertFalse(dc.is_resident_turn_active(
            plan.context_id, plan.resident_generation, db_path=self.db, now=_FIXED_NOW,
        ))
        # Also no active lease on the original generation row if gen bumped once.
        ctx_row = dc.get_daily_context_by_id(plan.context_id, db_path=self.db)
        self.assertFalse(dc.is_resident_turn_active(
            int(ctx_row['id']), int(ctx_row['resident_generation']),
            db_path=self.db, now=_FIXED_NOW,
        ))

    def test_mapping_blocked_rejects_successful_terminalization(self):
        """4) A canonical turn with blocked mapping cannot become successful."""
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'block-map', '2026-07-27 10:00:00')
        resident = _MappingBlockedResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        self.assertIsNone(plan.transcript_observation_error_code)
        canonical, aid = _persist_canonical_for_plan(plan)
        with mock.patch.object(dr, 'note_same_context_last_good') as note_last_good:
            with self.assertRaises(dr.DailyRuntimeError) as ctx:
                dr.handle_provider_success(
                    plan, assistant_message_id=aid, raw_text=canonical.content,
                )
        note_last_good.assert_not_called()
        self.assertEqual(ctx.exception.error_code, 'candidate_user_missing')
        self.assertIn('candidate_user_missing', str(ctx.exception))
        self.assertEqual(plan.manifest['transcript_mapping_status'], 'BLOCKED')
        self.assertIsNotNone(plan.manifest['transcript_mapping_error_code'])
        self.assertEqual(int(plan.manifest['transcript_mapping_event_count']), 0)
        self.assertIsNone(plan.manifest.get('cursor_cas_success'))
        self.assertTrue(plan.lease_released)
        self.assertFalse(dc.is_resident_turn_active(
            plan.context_id, plan.resident_generation, db_path=self.db, now=_FIXED_NOW,
        ))

        conn = sqlite3.connect(self.db)
        asst = conn.execute(
            "SELECT id, content FROM chat_messages WHERE author='assistant'"
        ).fetchone()
        formal_asst_count = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant' AND source_kind='chat'"
        ).fetchone()[0]
        context_mapping_count = conn.execute(
            'SELECT COUNT(*) FROM daily_message_contexts WHERE role=?',
            ('assistant',),
        ).fetchone()[0]
        map_count = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events'
        ).fetchone()[0]
        conn.close()
        self.assertIsNone(asst)
        self.assertEqual(int(formal_asst_count), 0)
        self.assertEqual(int(context_mapping_count), 0)
        self.assertEqual(int(map_count), 0)

        self.assertIsNone(dc.get_resident_history_cursor(
            plan.context_id,
            plan.resident_generation,
            db_path=self.db,
        ))

        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNone(reg)

    def test_mapping_terminal_diagnostic_precedes_rollback(self):
        jsonl_path = self._jsonl_for(_MAP_SESSION_COLD)
        uid = _insert(self.db, 'hayana', 'diagnostic-order', '2026-07-27 10:00:00')
        resident = _MappingBlockedResident(
            cwd=self.cwd, new_session_id=_MAP_SESSION_COLD, jsonl_path=jsonl_path,
        )
        plan = self._prepare(uid, resident=resident)
        list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC',
        ))
        canonical, aid = _persist_canonical_for_plan(plan)
        events = []
        real_rollback = dr._rollback_failed_terminalization

        def record_log(*args, **kwargs):
            rendered = ' '.join(str(value) for value in args)
            if 'TRANSCRIPT_MAPPING_TERMINAL_BLOCKED' in rendered:
                events.append(('log', rendered))

        def record_rollback(*args, **kwargs):
            events.append(('rollback', ''))
            return real_rollback(*args, **kwargs)

        with mock.patch.object(dr.logger, 'warning', side_effect=record_log), \
             mock.patch.object(
                 dr, '_rollback_failed_terminalization', side_effect=record_rollback,
             ):
            with self.assertRaises(dr.DailyRuntimeError) as ctx:
                dr.handle_provider_success(
                    plan, assistant_message_id=aid, raw_text=canonical.content,
                )

        self.assertEqual(ctx.exception.error_code, 'candidate_user_missing')
        self.assertEqual(events[0][0], 'log')
        self.assertIn('candidate_user_missing', events[0][1])
        self.assertEqual(events[1][0], 'rollback')

    def test_observation_mapping_diagnostic_preserves_exact_code(self):
        uid = _insert(self.db, 'hayana', 'observation-diagnostic', '2026-07-27 10:00:00')
        plan = self._prepare(uid)
        plan.transcript_observation_error_code = 'transcript_process_generation_changed'

        with mock.patch.object(dr.logger, 'warning') as warning:
            out = dr.finalize_transcript_mapping_after_success(
                plan, assistant_message_id=999,
            )

        self.assertEqual(out['transcript_mapping_status'], 'BLOCKED')
        self.assertEqual(
            out['transcript_mapping_error_code'],
            'transcript_process_generation_changed',
        )
        rendered_calls = [
            ' '.join(str(value) for value in call.args)
            for call in warning.call_args_list
        ]
        self.assertTrue(any(
            'TRANSCRIPT_MAPPING_OBSERVATION_BLOCKED' in rendered
            and 'transcript_process_generation_changed' in rendered
            for rendered in rendered_calls
        ))


class ResidentPeekRespawnReasonTests(unittest.TestCase):
    def test_peek_matches_decide_without_side_effects(self):
        rs = cc_resident.ResidentSession('/tmp/x', '', '/tmp/mcp.json')
        rs._proc = mock.Mock()
        rs._proc.poll.return_value = None
        rs._system_text = 'SYS'
        rs._last_used = time.time()
        rs._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
        rs._last_round_context = 0
        rs._resident_turn_count = 0
        rs._turns_since_respawn = 0
        gen_before = rs.generation
        sid_before = rs.session_id
        reason = rs.peek_respawn_reason('SYS', tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY)
        self.assertIsNone(reason)
        self.assertEqual(
            rs.peek_respawn_reason('OTHER', tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY),
            'system_changed',
        )
        self.assertEqual(rs.generation, gen_before)
        self.assertEqual(rs.session_id, sid_before)
        self.assertEqual(rs.cwd, '/tmp/x')


class SendTurnStdinFlushAckTests(unittest.TestCase):
    """Narrow: on_stdin_flushed sits after write+flush, before commit/stdout."""

    def _session_with_fake_proc(self, *, write_exc=None, flush_exc=None, lines=None):
        order: list[str] = []
        lines = list(lines if lines is not None else [])

        class _Stdin:
            def write(self, data):
                order.append('write')
                if write_exc is not None:
                    raise write_exc

            def flush(self):
                order.append('flush')
                if flush_exc is not None:
                    raise flush_exc

            def close(self):
                order.append('stdin_close')

        class _Stdout:
            def readline(self):
                order.append('stdout_read')
                if not lines:
                    return ''
                return lines.pop(0)

        rs = cc_resident.ResidentSession('/tmp/x', '', '/tmp/mcp.json')
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = _Stdin()
        proc.stdout = _Stdout()
        proc.terminate = mock.Mock()
        proc.kill = mock.Mock()
        proc.wait = mock.Mock(return_value=0)
        rs._proc = proc
        rs._session_id = 'sess-ack'
        rs._system_text = 'SYS'
        rs._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
        return rs, order, proc

    def test_callback_order_after_flush_before_stdout(self):
        rs, order, proc = self._session_with_fake_proc(lines=[])
        with mock.patch.object(
            rs, '_commit_sent_context', side_effect=lambda _m: order.append('commit'),
        ), mock.patch.object(
            rs, '_attach_jsonl_usage_with_retry', side_effect=lambda u, c: u,
        ):
            def _cb():
                order.append('callback')

            with self.assertRaises(cc_resident.ResidentError):
                list(rs.send_turn('hi', on_stdin_flushed=_cb))
        self.assertEqual(
            order[:5],
            ['write', 'flush', 'callback', 'commit', 'stdout_read'],
        )

    def test_default_none_callback_unchanged(self):
        rs, order, proc = self._session_with_fake_proc(lines=[])
        with mock.patch.object(
            rs, '_commit_sent_context', side_effect=lambda _m: order.append('commit'),
        ), mock.patch.object(
            rs, '_attach_jsonl_usage_with_retry', side_effect=lambda u, c: u,
        ):
            with self.assertRaises(cc_resident.ResidentError):
                list(rs.send_turn('hi'))
        self.assertEqual(order[:4], ['write', 'flush', 'commit', 'stdout_read'])
        self.assertNotIn('callback', order)

    def test_write_failure_skips_callback(self):
        rs, order, proc = self._session_with_fake_proc(
            write_exc=BrokenPipeError('pipe'),
        )
        called = {'n': 0}

        def _cb():
            called['n'] += 1

        with self.assertRaises(cc_resident.ResidentError):
            list(rs.send_turn('hi', on_stdin_flushed=_cb))
        self.assertEqual(called['n'], 0)
        self.assertEqual(order[0], 'write')
        self.assertNotIn('flush', order)
        self.assertNotIn('callback', order)
        self.assertIsNone(rs._proc)

    def test_callback_failure_kills_and_reraises(self):
        rs, order, proc = self._session_with_fake_proc(lines=[])

        def _cb():
            order.append('callback')
            raise RuntimeError('ack boom')

        with mock.patch.object(
            rs, '_commit_sent_context', side_effect=lambda _m: order.append('commit'),
        ):
            with self.assertRaises(RuntimeError):
                list(rs.send_turn('hi', on_stdin_flushed=_cb))
        self.assertEqual(order[:3], ['write', 'flush', 'callback'])
        self.assertNotIn('commit', order)
        self.assertNotIn('stdout_read', order)
        self.assertIsNone(rs._proc)


class DailyRuntimeTaskFeedbackTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    def _prepare(self, db, *, feedback=([], [])):
        _init_chat_messages(db)
        uid = _insert(db, 'hayana', 'feedback turn', '2026-07-27 10:00:00')
        fake_store = types.SimpleNamespace(
            peek_feedback=mock.Mock(return_value=feedback),
            consume_feedback=mock.Mock(return_value=len(feedback[1])),
        )
        with mock.patch.dict(sys.modules, {'command_store': fake_store}):
            plan = _prepare_turn(db, uid, static_system='STATIC')
        _attach_successful_canonical(plan)
        return plan, fake_store

    def test_peek_only_and_daily_injection_for_hot_cold_respawn(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(
                db,
                feedback=(['「阅读」用时 2分3秒（比预设快 7 秒）'], [11]),
            )
            store.peek_feedback.assert_called_once_with()
            store.consume_feedback.assert_not_called()
            for is_cold, is_respawn in ((False, False), (True, False), (False, True)):
                content = dr.format_resident_turn_content(
                    assembly=plan.assembly,
                    user_content=plan.user_content,
                    is_cold=is_cold,
                    is_respawn=is_respawn,
                )
                self.assertIn('## 任务完成反馈', content)
                self.assertIn('「阅读」用时 2分3秒（比预设快 7 秒）', content)
        finally:
            os.unlink(db)

    def test_empty_feedback_has_no_empty_section(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(db, feedback=([], []))
            content = dr.format_resident_turn_content(
                assembly=plan.assembly,
                user_content=plan.user_content,
                is_cold=False,
                is_respawn=False,
            )
            self.assertNotIn('## 任务完成反馈', content)
            store.consume_feedback.assert_not_called()
        finally:
            os.unlink(db)

    def test_snapshot_is_stable_across_reprepare(self):
        db = _tmp_db()
        try:
            first = (['「第一批」用时 1秒'], [21])
            second = (['「后来完成」用时 2秒'], [22])
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'reprepare', '2026-07-27 10:00:00')
            fake_store = types.SimpleNamespace(
                peek_feedback=mock.Mock(side_effect=[first, second]),
                consume_feedback=mock.Mock(return_value=1),
            )
            with mock.patch.dict(sys.modules, {'command_store': fake_store}), \
                 mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('STATE', 'snapshot', {})):
                plan = _prepare_turn(db, uid, static_system='STATIC')
                replacement = dr.reprepare_after_hot_cold_mismatch(
                    plan, resident=None, static_system='STATIC',
                )
            self.assertEqual(replacement.feedback_lines, ('「第一批」用时 1秒',))
            self.assertEqual(replacement.feedback_ids, (21,))
            fake_store.peek_feedback.assert_called_once_with()
        finally:
            os.unlink(db)

    def test_fence_b_rebuild_preserves_frozen_feedback_snapshot(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'Fence B feedback', '2026-07-27 10:00:00')
            original = (['「原反馈」用时 3秒'], [91])
            new_snapshot = (['「重建期间新反馈」用时 4秒'], [92])
            fake_store = types.SimpleNamespace(
                peek_feedback=mock.Mock(return_value=original),
                consume_feedback=mock.Mock(return_value=1),
            )
            with mock.patch.dict(sys.modules, {'command_store': fake_store}), \
                 mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                plan = _REAL_PREPARE_DAILY_TURN(
                    user_message_id=uid,
                    db_path=db,
                    now=_FIXED_NOW,
                    wall_now=_FIXED_NOW,
                    static_system='STATIC',
                )
            _attach_successful_canonical(plan)
            frozen_ids = plan.feedback_ids
            self.assertEqual(fake_store.peek_feedback.call_count, 1)

            def _build_rebuilt_assembly(**kwargs):
                # A new command completes while Fence B is rebuilding.  It is
                # deliberately made visible to any illegal second peek.
                fake_store.peek_feedback.return_value = new_snapshot
                return {'current_day_history': [], 'manifest': {}}

            with mock.patch.dict(sys.modules, {'command_store': fake_store}), \
                 mock.patch.object(
                     dr.dh,
                     'build_daily_window_context',
                     side_effect=_build_rebuilt_assembly,
                 ), \
                 mock.patch(
                     'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
                     return_value=50,
                 ), \
                 mock.patch(
                     'chat.cold_bootstrap_budget.estimate_whole_prompt',
                     side_effect=[100, 10],
                 ), \
                 mock.patch(
                     'chat.cold_bootstrap_budget.estimate_text_tokens',
                     return_value=0,
                 ), \
                 mock.patch(
                     'chat.cold_bootstrap_budget.effective_history_budget',
                     return_value=1,
                 ), \
                 mock.patch(
                     'chat.context_lean.cc_history_token_budget',
                     return_value=10,
                 ):
                rebuilt_content = dr._apply_daily_cold_prompt_fence(
                    plan,
                    resident=None,
                    static_system='STATIC',
                    content='over-budget prompt',
                    is_cold=True,
                    is_respawn=False,
                )

                self.assertIn('## 任务完成反馈', rebuilt_content)
                self.assertIn('「原反馈」用时 3秒', rebuilt_content)
                self.assertNotIn('「重建期间新反馈」用时 4秒', rebuilt_content)
                self.assertEqual(plan.feedback_ids, frozen_ids)
                self.assertEqual(plan.feedback_ids, (91,))
                self.assertEqual(
                    plan.assembly.get('task_feedback'),
                    dr._format_task_feedback(plan.feedback_lines),
                )

                # The rebuild itself must not create another peek.
                fake_store.peek_feedback.assert_called_once_with()

                with mock.patch.object(dr, 'complete_daily_turn', return_value={}), \
                     mock.patch.object(
                         dr,
                         'finalize_transcript_mapping_after_success',
                         side_effect=_mark_transcript_mapped,
                     ):
                    out = dr.handle_provider_success(
                        plan, assistant_message_id=191, raw_text='成功回复',
                    )
                fake_store.consume_feedback.assert_called_once_with([91])
                self.assertEqual(out['feedback_consume_status'], 'CONSUMED')

            # A failure after the same rebuild boundary still preserves the
            # frozen snapshot and cannot consume it.
            db2 = _tmp_db()
            try:
                _init_chat_messages(db2)
                uid2 = _insert(db2, 'hayana', 'Fence B failed feedback', '2026-07-27 10:00:00')
                failing_store = types.SimpleNamespace(
                    peek_feedback=mock.Mock(return_value=original),
                    consume_feedback=mock.Mock(return_value=1),
                )
                with mock.patch.dict(sys.modules, {'command_store': failing_store}), \
                     mock.patch.object(config_store, 'get_bool', return_value=True), \
                     mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
                    failed_plan = _REAL_PREPARE_DAILY_TURN(
                        user_message_id=uid2,
                        db_path=db2,
                        now=_FIXED_NOW,
                        wall_now=_FIXED_NOW,
                        static_system='STATIC',
                    )
                _attach_successful_canonical(failed_plan)
                with mock.patch.dict(sys.modules, {'command_store': failing_store}), \
                     mock.patch.object(
                         dr.dh,
                         'build_daily_window_context',
                         return_value={'current_day_history': [], 'manifest': {}},
                     ), \
                     mock.patch(
                         'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
                         return_value=50,
                     ), \
                     mock.patch(
                         'chat.cold_bootstrap_budget.estimate_whole_prompt',
                         side_effect=[100, 10],
                     ), \
                     mock.patch(
                         'chat.cold_bootstrap_budget.estimate_text_tokens',
                         return_value=0,
                     ), \
                     mock.patch(
                         'chat.cold_bootstrap_budget.effective_history_budget',
                         return_value=1,
                     ), \
                     mock.patch(
                         'chat.context_lean.cc_history_token_budget',
                         return_value=10,
                     ):
                    dr._apply_daily_cold_prompt_fence(
                        failed_plan,
                        resident=None,
                        static_system='STATIC',
                        content='over-budget prompt',
                        is_cold=True,
                        is_respawn=False,
                    )
                    self.assertEqual(failed_plan.feedback_ids, (91,))
                    with mock.patch.object(
                        dr,
                        'complete_daily_turn',
                        side_effect=RuntimeError('commit failed'),
                    ), \
                     mock.patch.object(
                         dr,
                         'finalize_transcript_mapping_after_success',
                         side_effect=_mark_transcript_mapped,
                     ):
                        with self.assertRaises(RuntimeError):
                            dr.handle_provider_success(
                                failed_plan,
                                assistant_message_id=192,
                                raw_text='回复',
                            )
                failing_store.consume_feedback.assert_not_called()
            finally:
                os.unlink(db2)
        finally:
            os.unlink(db)

    def test_success_consumes_snapshot_once_after_full_success(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(
                db,
                feedback=(['「任务」用时 3秒'], [31]),
            )
            with mock.patch.dict(sys.modules, {'command_store': store}), \
                 mock.patch.object(dr, 'complete_daily_turn', return_value={}) as complete, \
                 mock.patch.object(
                     dr,
                     'finalize_transcript_mapping_after_success',
                     side_effect=_mark_transcript_mapped,
                 ):
                out = dr.handle_provider_success(
                    plan, assistant_message_id=99, raw_text='成功回复',
                )
            complete.assert_called_once()
            store.consume_feedback.assert_called_once_with([31])
            self.assertEqual(out['feedback_consume_status'], 'CONSUMED')
            self.assertEqual(out['feedback_consumed_count'], 1)
        finally:
            os.unlink(db)

    def test_success_never_repeeks_and_does_not_overconsume_new_feedback(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(
                db,
                feedback=(['「旧任务」用时 3秒'], [41]),
            )
            with mock.patch.dict(sys.modules, {'command_store': store}), \
                 mock.patch.object(dr, 'complete_daily_turn', return_value={}), \
                 mock.patch.object(
                     dr,
                     'finalize_transcript_mapping_after_success',
                     side_effect=_mark_transcript_mapped,
                 ):
                dr.handle_provider_success(
                    plan, assistant_message_id=100, raw_text='成功回复',
                )
            store.peek_feedback.assert_called_once_with()
            store.consume_feedback.assert_called_once_with([41])
        finally:
            os.unlink(db)

    def test_provider_empty_and_cursor_failures_do_not_consume(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(db, feedback=(['「任务」用时 3秒'], [51]))
            with mock.patch.dict(sys.modules, {'command_store': store}), \
                 mock.patch.object(dr, 'abort_daily_turn', return_value={}):
                with self.assertRaises(dr.DailyRuntimeError):
                    dr.handle_provider_success(
                        plan, assistant_message_id=101, raw_text='',
                    )
            self.assertFalse(store.consume_feedback.called)

            exc = dr.CursorCASConflictAfterPersist(
                'cursor conflict', assistant_message_id=101, manifest={},
            )
            with mock.patch.dict(sys.modules, {'command_store': store}), \
                 mock.patch.object(dr, 'complete_daily_turn', side_effect=exc), \
                 mock.patch.object(
                     dr,
                     'finalize_transcript_mapping_after_success',
                     side_effect=_mark_transcript_mapped,
                 ), \
                 mock.patch.object(
                     dr,
                     '_rollback_failed_terminalization',
                     return_value={},
                 ) as rollback:
                with self.assertRaises(dr.CursorCASConflictAfterPersist):
                    dr.handle_provider_success(
                        plan, assistant_message_id=101, raw_text='回复',
                    )
            rollback.assert_called_once_with(plan, assistant_message_id=101)
            self.assertFalse(store.consume_feedback.called)
        finally:
            os.unlink(db)

    def test_provider_failure_and_persist_failure_do_not_consume(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(db, feedback=(['「任务」用时 3秒'], [61]))
            with mock.patch.object(dr, 'abort_daily_turn', return_value={}):
                dr.handle_provider_failure(plan, error_code='provider_failure')
            self.assertFalse(store.consume_feedback.called)

            with mock.patch.object(dr, 'persist_daily_assistant_for_plan', side_effect=RuntimeError('persist failed')):
                with self.assertRaises(RuntimeError):
                    dr.persist_daily_assistant_for_plan(plan, content='回复')
            self.assertFalse(store.consume_feedback.called)
        finally:
            os.unlink(db)

    def test_interrupt_and_partial_rescue_do_not_consume(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(db, feedback=(['「任务」用时 3秒'], [81]))
            with mock.patch.object(dr, 'persist_daily_assistant_for_plan', return_value=103):
                dr.persist_partial_daily_stream_rescue(plan, content='部分回复')
            self.assertFalse(store.consume_feedback.called)
        finally:
            os.unlink(db)

    def test_consume_failure_is_fail_open_and_manifested(self):
        db = _tmp_db()
        try:
            plan, store = self._prepare(
                db,
                feedback=(['「任务」用时 3秒'], [71]),
            )
            store.consume_feedback.side_effect = RuntimeError('bookkeeping failed')
            with mock.patch.dict(sys.modules, {'command_store': store}), \
                 mock.patch.object(dr, 'complete_daily_turn', return_value={}), \
                 mock.patch.object(
                     dr,
                     'finalize_transcript_mapping_after_success',
                     side_effect=_mark_transcript_mapped,
                 ):
                out = dr.handle_provider_success(
                    plan, assistant_message_id=102, raw_text='成功回复',
                )
            self.assertEqual(out['feedback_consume_status'], 'FAILED')
            self.assertEqual(out['feedback_consume_error'], 'RuntimeError')
        finally:
            os.unlink(db)



class DailyAttachmentReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.static_dir = Path(self.tmp.name)
        (self.static_dir / 'uploads' / 'files').mkdir(parents=True)
        self.image_bytes = b'fake-image'

    def tearDown(self):
        self.tmp.cleanup()

    def _image(self, name):
        return {'type': 'image', 'url': '/static/uploads/' + name, 'name': name}

    def _file(self, name, body):
        stored = 'abcdef12_' + name
        (self.static_dir / 'uploads' / 'files' / stored).write_bytes(body)
        return {
            'type': 'file',
            'url': '/static/uploads/files/' + stored,
            'name': name,
        }

    def test_empty_metadata_variants_and_legacy_fallback(self):
        from chat.attachment_contract import (
            AttachmentValidationError,
            provider_current_turn_attachments,
        )

        self.assertEqual(provider_current_turn_attachments(None), [])
        self.assertEqual(provider_current_turn_attachments(''), [])
        self.assertEqual(provider_current_turn_attachments('  \n\t'), [])
        self.assertEqual(provider_current_turn_attachments('[]'), [])
        image = provider_current_turn_attachments(
            '', legacy_image_url='/static/uploads/legacy.png',
        )
        self.assertEqual(image[0]['type'], 'image')
        text_file = self._file('legacy.txt', b'legacy')
        file_items = provider_current_turn_attachments(
            '', legacy_file_url=text_file['url'], legacy_file_name=text_file['name'],
        )
        self.assertEqual(file_items, [text_file])
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments('{')
        with self.assertRaises(AttachmentValidationError):
            provider_current_turn_attachments('{}')
        canonical = self._image('same.png')
        self.assertEqual(
            provider_current_turn_attachments(
                [canonical], legacy_image_url=canonical['url'],
            ),
            [canonical],
        )

    def test_daily_prior_attachments_are_explicitly_bounded_on_cold_respawn(self):
        from chat.daily_runtime import (
            DAILY_HISTORY_ATTACHMENT_POLICY,
            format_resident_turn_content,
        )

        prior = [
            self._image('one.png'),
            self._image('two.png'),
            self._file('round-a.txt', b'private body'),
        ]
        history = [
            {
                'role': 'user',
                'content': 'Round A text',
                'image_url': '',
                'file_url': '',
                'file_name': '',
                'attachments': prior,
            },
            {'role': 'assistant', 'content': 'Round A reply', 'attachments': []},
        ]
        for is_cold, is_respawn in ((True, False), (False, True)):
            content = format_resident_turn_content(
                assembly={'state': '', 'current_day_history': history},
                user_content='Round B text',
                user_image_url='',
                user_attachments=[],
                is_cold=is_cold,
                is_respawn=is_respawn,
            )
            self.assertEqual(
                DAILY_HISTORY_ATTACHMENT_POLICY,
                'explicit_metadata_degrade_v1',
            )
            self.assertIn('Round A text', content)
            self.assertIn('one.png', content)
            self.assertIn('two.png', content)
            self.assertIn('round-a.txt', content)
            self.assertIn('显式降级为元数据标记', content)
            self.assertIn('本轮不重读历史图片或文件正文', content)
            self.assertNotIn('private body', content)

    def test_selected_forge_carryover_keeps_ordered_attachment_content(self):
        from chat.context_window_forge import _user_content_for_row

        row = {
            'id': 101,
            'content': 'Round A text',
            'image_url': '',
            'file_url': '',
            'file_name': '',
            'attachments': [
                self._image('one.png'),
                self._file('carryover.txt', b'carryover body'),
                self._image('two.png'),
            ],
        }
        with mock.patch(
            'chat.cc_vision_bridge.resolve_image_bytes',
            return_value=(self.image_bytes, 'image/png'),
        ):
            content = _user_content_for_row(
                row, attachment_static_dir=str(self.static_dir),
            )
        self.assertEqual(
            [block['type'] for block in content],
            ['text', 'image', 'text', 'image'],
        )
        self.assertIn('carryover body', content[2]['text'])
        self.assertNotIn('base64', content[0]['text'].lower())


    def test_daily_selected_carryover_keeps_explicit_attachment_marker(self):
        from chat.daily_runtime import format_resident_turn_content

        attachments = [
            self._image('carry-one.png'),
            self._image('carry-two.png'),
            self._file('carry.txt', b'carry body'),
        ]
        content = format_resident_turn_content(
            assembly={
                'state': '',
                'carryover_messages': [{
                    'role': 'user',
                    'content': 'Old round',
                    'image_url': '',
                    'file_url': '',
                    'file_name': '',
                    'attachments': attachments,
                }],
                'current_day_history': [],
            },
            user_content='New round',
            user_image_url='',
            user_attachments=[],
            is_cold=True,
            is_respawn=False,
        )
        self.assertIn('Old round', content)
        self.assertIn('carry-one.png', content)
        self.assertIn('carry-two.png', content)
        self.assertIn('carry.txt', content)
        self.assertIn('显式降级为元数据标记', content)
        self.assertNotIn('carry body', content)



class ContinuityShadowObservationTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()

    class _Resident(_FakeResident):
        def __init__(self):
            super().__init__()
            self.sent_objects = []
            self.sent_kwargs = []

        def send_turn(self, content, commit_meta=None, turn_lease=None):
            self.sent_objects.append(content)
            self.sent_kwargs.append({
                'commit_meta': commit_meta,
                'turn_lease': turn_lease,
            })
            yield ('text', 'daily reply')
            yield ('done', ('daily reply', '', {}, {}))

    @staticmethod
    def _result(*, surface='empty', status='ready', error_code=None, representations=()):
        policy = types.SimpleNamespace(recent_raw_target=24)
        plan = types.SimpleNamespace(
            plan_id='shadow-plan',
            plan_hash='shadow-hash',
            valid=(status == 'ready'),
            budget_status='fit' if status == 'ready' else 'blocked',
            token_budget=100,
            reserve_budget=8,
            recent_raw_target=24,
            budget_policy=policy,
            selected_token_estimate=12,
            fixed_section_token_estimate=5,
            total_token_estimate=25,
            remaining_budget=75,
            representations=tuple(representations),
            gaps=(),
            exclusions=(),
        )
        return types.SimpleNamespace(
            status=status,
            error_code=error_code,
            plan=plan if status == 'ready' else None,
            chunk_surface=surface,
            source_member_count=1,
            chunk_binding_count=1 if surface == 'ready' else 0,
        )

    @staticmethod
    def _observations(log):
        out = []
        for call in log.call_args_list:
            if call.args and call.args[0] == 'continuity_shadow_observation %s':
                out.append(json.loads(call.args[1]))
        return out

    def _plan(self, db, *, content='hello'):
        _init_chat_messages(db)
        uid = _insert(db, 'hayana', content, '2026-07-27 10:00:00')
        plan = _prepare_turn(db, uid, static_system='STATIC')
        plan.assembly.setdefault('manifest', {})['cold_history_budget'] = 24
        return plan

    def _stream(self, plan, *, env=None):
        resident = self._Resident()
        events = list(dr.stream_daily_resident_turn(
            plan,
            resident=resident,
            env=env or {},
            static_system='STATIC',
        ))
        return resident, events

    def test_unconfigured_cold_shadow_is_blocked_and_send_once(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(dr.logger, 'info') as log, \
                 mock.patch('chat.daily_continuity_shadow.build_daily_continuity_shadow_plan') as adapter:
                resident, events = self._stream(plan)
            adapter.assert_not_called()
            self.assertEqual(len(resident.sent_objects), 1)
            self.assertTrue(any(evt == 'done' for evt, _payload in events))
            self.assertEqual(self._observations(log)[-1]['error_code'], 'shadow_store_unconfigured')
        finally:
            os.unlink(db)

    def test_cold_ready_uses_explicit_path_policy_and_fixed_sections(self):
        db = _tmp_db()
        try:
            plan = self._plan(db, content='中文 production body')
            result = self._result()
            shadow_path = os.path.join(tempfile.gettempdir(), 'continuity-shadow-r4c.db')
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': shadow_path,
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     return_value=result,
                 ) as adapter, \
                 mock.patch.object(dr.logger, 'info') as log, \
                 mock.patch('chat.cold_bootstrap_budget.resident_rebuild_prompt_target', return_value=100), \
                 mock.patch('chat.cold_bootstrap_budget.cold_safety_margin', return_value=8):
                resident, _events = self._stream(plan)
            adapter.assert_called_once()
            kwargs = adapter.call_args.kwargs
            self.assertEqual(kwargs['source_db_path'], db)
            self.assertEqual(kwargs['shadow_store_path'], shadow_path)
            self.assertEqual(kwargs['budget_policy'].token_budget, 108)
            self.assertEqual(kwargs['budget_policy'].reserve_budget, 8)
            self.assertEqual(kwargs['budget_policy'].recent_raw_target, 24)
            self.assertEqual(
                [section.kind for section in kwargs['accepted_fixed_sections']],
                ['invariant_system'],
            )
            self.assertEqual(len(resident.sent_objects), 1)
            observation = self._observations(log)[-1]
            self.assertTrue(observation['shadow_plan_available'])
            self.assertFalse(observation['installed_context_proven'])
        finally:
            os.unlink(db)

    def test_respawn_ready_chunk_and_final_send_once(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            plan.is_respawn = True
            plan.is_cold = False
            plan.manifest['turn_kind'] = 'respawn'
            result = self._result(
                surface='ready',
                representations=(types.SimpleNamespace(kind='chunk'),),
            )
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     return_value=result,
                 ) as adapter:
                resident, _events = self._stream(plan)
            adapter.assert_called_once()
            self.assertEqual(len(resident.sent_objects), 1)
        finally:
            os.unlink(db)

    def test_hot_and_capacity_swap_block_without_adapter(self):
        for turn_kind in ('hot', 'capacity_swap'):
            db = _tmp_db()
            try:
                plan = self._plan(db)
                plan.is_cold = False
                plan.is_respawn = False
                plan.manifest['turn_kind'] = turn_kind
                with mock.patch.dict(os.environ, {
                    'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
                }, clear=False), \
                     mock.patch(
                         'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     ) as adapter, \
                     mock.patch.object(dr.logger, 'info') as log:
                    dr._observe_continuity_shadow(
                        plan=plan,
                        resident=self._Resident(),
                        static_system='STATIC',
                        content='hot body',
                    )
                adapter.assert_not_called()
                expected_error = (
                    'hot_budget_policy_unmapped'
                    if turn_kind == 'hot'
                    else 'installed_context_policy_unmapped'
                )
                self.assertEqual(
                    self._observations(log)[-1]['error_code'],
                    expected_error,
                )
            finally:
                os.unlink(db)

    def test_unavailable_and_exception_are_fail_open(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            unavailable = self._result(
                status='blocked',
                error_code='chunk_surface_unavailable',
                surface='unavailable',
            )
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     return_value=unavailable,
                 ) as adapter, \
                 mock.patch.object(dr.logger, 'info') as log:
                resident, _events = self._stream(plan)
            adapter.assert_called_once()
            self.assertFalse(
                self._observations(log)[-1]['installed_context_proven'],
            )
            self.assertEqual(len(resident.sent_objects), 1)
        finally:
            os.unlink(db)

        db = _tmp_db()
        try:
            plan = self._plan(db, content='second')
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     side_effect=RuntimeError('observer fault'),
                 ) as adapter, \
                 mock.patch.object(dr.logger, 'exception') as log_exception, \
                 mock.patch.object(dr.logger, 'info') as log_info:
                resident, _events = self._stream(plan)
            adapter.assert_called_once()
            log_exception.assert_called_once()
            self.assertFalse(
                self._observations(log_info)[-1]['installed_context_proven'],
            )
            self.assertEqual(len(resident.sent_objects), 1)
        finally:
            os.unlink(db)

    def test_fingerprint_and_projection_are_deterministic_without_body_logging(self):
        self.assertEqual(
            dr._continuity_shadow_fingerprint('中文abc'),
            dr._continuity_shadow_fingerprint('中文abc'),
        )
        first = dr._continuity_shadow_fingerprint(
            [{'type': 'text', 'text': '中文'}, {'type': 'image', 'name': 'a'}],
        )
        second = dr._continuity_shadow_fingerprint(
            [{'text': '中文', 'type': 'text'}, {'name': 'a', 'type': 'image'}],
        )
        self.assertEqual(first, second)
        self.assertEqual(first[2], 'multimodal')
        db = _tmp_db()
        try:
            plan = self._plan(db)
            plan.assembly.update({
                'state': 'STATE BODY',
                'day_handoff_content': {
                    'source_day': '2026-09-09',
                    'source_sha256': 'handoff-hash',
                    'source_last_message_id': 7,
                    'open_loops': ['loop one'],
                    'topics': ['must not affect projection'],
                    'last_topic': 'also ignored',
                },
            })
            resident = self._Resident()
            resident._system_text = 'EFFECTIVE SYSTEM'
            before = dict(plan.manifest)
            sections = dr._build_continuity_shadow_fixed_sections(
                plan=plan,
                resident=resident,
                static_system='FALLBACK SYSTEM',
            )
            self.assertEqual(
                [section.kind for section in sections],
                ['invariant_system', 'accepted_state', 'accepted_open_loops'],
            )
            self.assertEqual(
                sections[0].content_hash,
                hashlib.sha256(b'EFFECTIVE SYSTEM').hexdigest(),
            )
            self.assertEqual(plan.manifest, before)
            log = mock.Mock()
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                     return_value=self._result(),
                 ):
                with mock.patch.object(dr.logger, 'info') as info:
                    dr._observe_continuity_shadow(
                        plan=plan,
                        resident=resident,
                        static_system='FALLBACK SYSTEM',
                        content='PRIVATE BODY MUST NOT APPEAR',
                    )
            serialized = ' '.join(
                str(arg)
                for call in info.call_args_list
                for arg in call.args
            )
            self.assertNotIn('PRIVATE BODY MUST NOT APPEAR', serialized)
        finally:
            os.unlink(db)


    def test_fixed_sections_use_full_semantic_state_snapshot_not_hot_delta(self):
        from chat.persona_state_semantic import (
            format_persona_semantic_snapshot,
            translate_raw_state_to_persona_semantic,
        )

        plan = types.SimpleNamespace(
            assembly={
                'state': '【此刻有一点变化】\\n旧 transport delta',
                'state_snapshot': {
                    'emotion': 'mood=平静',
                    'drive': 'fatigue=0.10 stress=0.10',
                },
                'day_handoff_content': None,
            },
            manifest={},
        )
        sections = dr._build_continuity_shadow_fixed_sections(
            plan=plan,
            resident=types.SimpleNamespace(_system_text='STATIC'),
            static_system='FALLBACK',
        )
        accepted = next(section for section in sections if section.kind == 'accepted_state')
        expected = format_persona_semantic_snapshot(
            translate_raw_state_to_persona_semantic(plan.assembly['state_snapshot']),
        )
        self.assertEqual(accepted.content_hash, hashlib.sha256(expected.encode()).hexdigest())
        self.assertNotEqual(accepted.content_hash, hashlib.sha256(
            plan.assembly['state'].encode(),
        ).hexdigest())

    def test_production_fixed_sections_do_not_fallback_to_hot_delta(self):
        plan = types.SimpleNamespace(
            assembly={
                'state': 'HOT DELTA ONLY',
                'day_handoff_content': None,
            },
            manifest={},
        )
        sections = dr._build_continuity_shadow_fixed_sections(
            plan=plan,
            resident=types.SimpleNamespace(_system_text='STATIC'),
            static_system='FALLBACK',
            require_full_state_snapshot=True,
        )
        self.assertNotIn('accepted_state', [section.kind for section in sections])


    def test_legacy_fixed_sections_fallback_when_state_snapshot_is_empty(self):
        plan = types.SimpleNamespace(
            assembly={
                'state_snapshot': {},
                'state': 'LEGACY STATE',
                'day_handoff_content': None,
            },
            manifest={},
        )
        sections = dr._build_continuity_shadow_fixed_sections(
            plan=plan,
            resident=types.SimpleNamespace(_system_text='STATIC'),
            static_system='FALLBACK',
            require_full_state_snapshot=False,
        )
        self.assertIn('accepted_state', [section.kind for section in sections])

    def test_hot_transport_keeps_delta_or_empty_without_replaying_snapshot(self):
        from chat.persona_state_semantic import (
            format_persona_semantic_snapshot,
            translate_raw_state_to_persona_semantic,
        )

        snapshot = {'emotion': 'mood=平静', 'drive': 'stress=0.10'}
        full_snapshot = format_persona_semantic_snapshot(
            translate_raw_state_to_persona_semantic(snapshot),
        )
        empty_payload = dr.format_resident_turn_content(
            assembly={'state': '', 'state_snapshot': snapshot},
            user_content='hello',
            is_cold=False,
            is_respawn=False,
        )
        delta_payload = dr.format_resident_turn_content(
            assembly={'state': 'DELTA ONLY', 'state_snapshot': snapshot},
            user_content='hello',
            is_cold=False,
            is_respawn=False,
        )
        self.assertEqual(empty_payload, 'hello')
        self.assertNotIn(full_snapshot, empty_payload)
        self.assertIn('DELTA ONLY', delta_payload)
        self.assertNotIn(full_snapshot, delta_payload)

    def test_missing_cold_manifest_fails_closed(self):
        db = _tmp_db()
        try:
            plan = self._plan(db)
            plan.assembly['manifest'].pop('cold_history_budget', None)
            with mock.patch.dict(os.environ, {
                'HAYA_CONTINUITY_SHADOW_STORE_PATH': 'shadow.db',
            }, clear=False), \
                 mock.patch(
                     'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
                 ) as adapter, \
                 mock.patch.object(dr.logger, 'info') as log:
                dr._observe_continuity_shadow(
                    plan=plan,
                    resident=self._Resident(),
                    static_system='STATIC',
                    content='body',
                )
            adapter.assert_not_called()
            self.assertEqual(
                self._observations(log)[-1]['error_code'],
                'budget_policy_unmapped',
            )
        finally:
            os.unlink(db)


class ContextPlanBudgetAuthorityTests(unittest.TestCase):
    def test_policy_maps_canonical_authority_without_semantic_version_change(self):
        with mock.patch(
            'chat.cold_bootstrap_budget.cold_prompt_target',
            return_value=90000,
        ) as target, mock.patch(
            'chat.cold_bootstrap_budget.cold_safety_margin',
            return_value=8000,
        ) as reserve, mock.patch(
            'chat.context_lean.cc_history_token_budget',
            return_value=24000,
        ) as recent_raw:
            policy, version = dr._context_plan_policy()

        self.assertEqual(policy.token_budget, 98000)
        self.assertEqual(policy.reserve_budget, 8000)
        self.assertEqual(policy.usable_budget, 90000)
        self.assertEqual(policy.recent_raw_target, 24000)
        self.assertEqual(version, 'continuity_context_budget_v1')
        target.assert_called_once_with()
        reserve.assert_called_once_with()
        recent_raw.assert_called_once_with()

    def test_policy_tracks_dynamic_canonical_authority(self):
        cases = (
            (90000, 8000, 24000, 98000),
            (70000, 6000, 18000, 76000),
        )
        for target_value, reserve_value, recent_value, token_budget in cases:
            with self.subTest(
                target=target_value,
                reserve=reserve_value,
                recent_raw=recent_value,
            ), mock.patch(
                'chat.cold_bootstrap_budget.cold_prompt_target',
                return_value=target_value,
            ), mock.patch(
                'chat.cold_bootstrap_budget.cold_safety_margin',
                return_value=reserve_value,
            ), mock.patch(
                'chat.context_lean.cc_history_token_budget',
                return_value=recent_value,
            ):
                policy, _version = dr._context_plan_policy()

            self.assertEqual(
                (
                    policy.token_budget,
                    policy.reserve_budget,
                    policy.usable_budget,
                    policy.recent_raw_target,
                ),
                (token_budget, reserve_value, target_value, recent_value),
            )

    def test_legacy_context_plan_budget_keys_are_not_read_or_authoritative(self):
        legacy_values = {
            'CONTEXT_PLAN_TOKEN_BUDGET': '123',
            'CONTEXT_PLAN_RESERVE_BUDGET': '45',
            'CONTEXT_PLAN_RECENT_RAW_TARGET': '67',
        }

        def _legacy_get(key, default=''):
            return legacy_values.get(key, default)

        with mock.patch.object(
            config_store,
            'get',
            side_effect=_legacy_get,
        ) as legacy_get, mock.patch(
            'chat.cold_bootstrap_budget.cold_prompt_target',
            return_value=70000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_safety_margin',
            return_value=6000,
        ), mock.patch(
            'chat.context_lean.cc_history_token_budget',
            return_value=18000,
        ):
            policy, _version = dr._context_plan_policy()

        self.assertEqual(
            (
                policy.token_budget,
                policy.reserve_budget,
                policy.usable_budget,
                policy.recent_raw_target,
            ),
            (76000, 6000, 70000, 18000),
        )
        self.assertFalse(
            any(
                call.args and call.args[0] in legacy_values
                for call in legacy_get.call_args_list
            )
        )

    def test_canonical_authority_failure_is_fail_visible(self):
        with mock.patch(
            'chat.cold_bootstrap_budget.cold_prompt_target',
            side_effect=RuntimeError('canonical target unavailable'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'canonical target unavailable'):
                dr._context_plan_policy()

    def test_mode_aware_policy_keeps_cold_hot_and_capacity_authorities_separate(self):
        with mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard',
            return_value=70000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_prompt_target',
            return_value=150000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.capacity_swap_prompt_target',
            return_value=90000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_safety_margin',
            return_value=8000,
        ), mock.patch(
            'chat.context_lean.cc_history_token_budget',
            return_value=24000,
        ):
            cold, _ = dr._context_plan_policy(mode='cold')
            respawn, _ = dr._context_plan_policy(mode='respawn')
            hot, _ = dr._context_plan_policy(mode='hot')
            capacity, _ = dr._context_plan_policy(mode='capacity')

        self.assertEqual(cold.token_budget, 98000)
        self.assertEqual(respawn.token_budget, 98000)
        self.assertEqual(cold.usable_budget, 90000)
        self.assertEqual(respawn.usable_budget, 90000)
        self.assertEqual(hot.usable_budget, 150000)
        self.assertEqual(capacity.usable_budget, 90000)


    def test_cold_rebuild_guard_does_not_change_rebuild_packing_policy(self):
        for guard_value in (60000, 70000, 80000):
            with self.subTest(guard=guard_value), mock.patch(
                'chat.cold_bootstrap_budget.cold_rebuild_guard',
                return_value=guard_value,
            ), mock.patch(
                'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
                return_value=90000,
            ), mock.patch(
                'chat.cold_bootstrap_budget.cold_safety_margin',
                return_value=8000,
            ), mock.patch(
                'chat.context_lean.cc_history_token_budget',
                return_value=24000,
            ):
                cold, _ = dr._context_plan_policy(mode='cold')
                respawn, _ = dr._context_plan_policy(mode='respawn')

            self.assertEqual(cold.usable_budget, 90000)
            self.assertEqual(respawn.usable_budget, 90000)

    def test_pre_462_rebuild_packing_budget_is_deterministically_equivalent(self):
        fixed_sections = {
            'static_system': 12000,
            'dynamic_state': 4000,
            'current_user': 3000,
            'anchor_reserve': 5000,
        }
        fixed_total = sum(fixed_sections.values())
        with mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_safety_margin',
            return_value=8000,
        ), mock.patch(
            'chat.context_lean.cc_history_token_budget',
            return_value=24000,
        ):
            corrected, _ = dr._context_plan_policy(mode='cold')

        # PRE_462 used cold_prompt_target() == 90k and token_budget=target+reserve.
        pre_462_usable = 90000
        corrected_retained = corrected.usable_budget - fixed_total
        pre_462_retained = pre_462_usable - fixed_total
        self.assertEqual(corrected_retained, pre_462_retained)
        self.assertEqual(corrected_retained, 66000)
        self.assertEqual(corrected_retained - pre_462_retained, 0)

    def test_hot_threshold_defaults_remain_unchanged(self):
        from chat.cold_bootstrap_budget import cold_hard_limit, cold_soft_limit

        with mock.patch(
            'config_store.get_int',
            side_effect=lambda _key, default=0: default,
        ):
            self.assertEqual(cold_soft_limit(), 150000)
            self.assertEqual(cold_hard_limit(), 180000)
            self.assertEqual(cc_resident._cfg_int('CC_MAX_RESIDENT_TURNS', 45), 45)

    def test_cold_planner_accepts_75k_plan_despite_legacy_guard(self):
        from continuity.context_plan import (
            ContextChunkBinding,
            ContextSection,
            build_context_plan,
        )
        from continuity.contracts import (
            ContinuityChunk,
            SourceMember,
            SourceSnapshot,
            candidate_source_revision,
        )
        from continuity.coverage import source_hash
        from continuity.sealing import CandidateBlock

        members = tuple(
            SourceMember(
                seq=seq,
                source_kind='completed_turn',
                source_ref='turn:%s' % seq,
                source_revision='rev:%s' % seq,
                role='user',
                content_hash='hash:%s' % seq,
                logical_size=40000,
                created_at='2026-07-27 09:0%s:00' % seq,
            )
            for seq in (1, 2)
        )
        snapshot_hash = source_hash(members)
        snapshot = SourceSnapshot(
            snapshot_id='snapshot:one',
            identity_id='default',
            chat_id='default',
            branch_id='active-transcript',
            local_day='2026-07-27',
            source_watermark=2,
            policy_version='r1',
            source_hash=snapshot_hash,
            status='ready',
            created_at='2026-07-27 10:00:00',
            members=members,
        )
        candidate = CandidateBlock(
            candidate_id='candidate:one',
            snapshot_id=snapshot.snapshot_id,
            policy_version='r1',
            block_seq=0,
            local_day='2026-07-27',
            branch_id='active-transcript',
            source_start_seq=1,
            source_end_seq=2,
            source_seqs=(1, 2),
            source_refs=('turn:1', 'turn:2'),
            source_revisions=('rev:1', 'rev:2'),
            logical_size=80000,
            completed_turn_count=2,
            oversize=False,
            close_reason='boundary',
            source_revision=candidate_source_revision(members),
        )
        body = 'compact canonical chunk'
        chunk = ContinuityChunk(
            chunk_id='chunk:one',
            generation_job_id='job:one',
            candidate_id=candidate.candidate_id,
            snapshot_id=snapshot.snapshot_id,
            artifact_revision='artifact:one',
            body=body,
            body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(),
            source_token_estimate=80000,
            output_token_estimate=45000,
            generator_policy_version='r1',
            prompt_policy_version='r1',
            provider='test',
            model_identity='test',
            actual_executor='test',
            generation_id='generation:one',
            status='ready',
            created_at='2026-07-27 10:01:00',
        )
        binding = ContextChunkBinding(chunk=chunk, candidate=candidate, snapshot=snapshot)
        with mock.patch(
            'chat.cold_bootstrap_budget.cold_rebuild_guard',
            return_value=70000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.resident_rebuild_prompt_target',
            return_value=90000,
        ), mock.patch(
            'chat.cold_bootstrap_budget.cold_safety_margin',
            return_value=8000,
        ), mock.patch(
            'chat.context_lean.cc_history_token_budget',
            return_value=0,
        ):
            policy, _version = dr._context_plan_policy(mode='cold')
        plan = build_context_plan(
            members,
            raw_members=members,
            chunks=(binding,),
            budget_policy=policy,
            fixed_sections=(
                # The compact fixture contributes 8,006 tokens; fixed sections total 66,994.
                ContextSection(
                    kind='invariant_system',
                    source_ref='system:one',
                    content_hash='system-hash',
                    estimated_tokens=61994,
                ),
                ContextSection(
                    kind='current_request',
                    source_ref='message:current',
                    content_hash='current-hash',
                    estimated_tokens=5000,
                ),
            ),
            budget_policy_version='continuity_context_budget_v1',
        )

        self.assertEqual(sum(member.logical_size for member in members), 80000)
        self.assertTrue(plan.valid)
        self.assertEqual(plan.budget_status, 'fit')
        self.assertEqual(plan.total_token_estimate, 75000)
        self.assertEqual([item.kind for item in plan.representations], ['chunk'])
        self.assertLessEqual(plan.total_token_estimate, 90000)
        self.assertLess(plan.total_token_estimate, 90000)

        fixed_overflow = build_context_plan(
            members,
            raw_members=members,
            chunks=(binding,),
            budget_policy=policy,
            fixed_sections=(
                ContextSection(
                    kind='invariant_system',
                    source_ref='system:oversized',
                    content_hash='system-oversized-hash',
                    estimated_tokens=90001,
                ),
                ContextSection(
                    kind='current_request',
                    source_ref='message:current',
                    content_hash='current-hash',
                    estimated_tokens=1,
                ),
            ),
            budget_policy_version='continuity_context_budget_v1',
        )
        self.assertFalse(fixed_overflow.valid)
        self.assertEqual(fixed_overflow.budget_status, 'blocked')


class ContextPlanConsumerTests(unittest.TestCase):
    @staticmethod
    def _context_plan_runtime_db():
        db = _tmp_db()
        _init_chat_messages(db)
        conn = sqlite3.connect(db)
        for column, definition in (
            ('branches', 'TEXT DEFAULT ""'),
            ('branch_idx', 'INTEGER DEFAULT 0'),
            ('attachments', 'TEXT DEFAULT ""'),
            ('file_url', 'TEXT DEFAULT ""'),
            ('file_name', 'TEXT DEFAULT ""'),
        ):
            conn.execute(
                'ALTER TABLE chat_messages ADD COLUMN %s %s' % (
                    column, definition,
                )
            )
        conn.execute(
            'CREATE TABLE IF NOT EXISTS daily_message_contexts ('
            'message_id INTEGER PRIMARY KEY, context_id INTEGER, context_epoch INTEGER, '
            'resident_generation INTEGER, role TEXT, created_at TEXT)'
        )
        conn.execute(
            'CREATE TABLE r45a_scope_fixture (id INTEGER PRIMARY KEY)'
        )
        conn.execute(
            'CREATE TABLE IF NOT EXISTS wake_log ('
            'id INTEGER PRIMARY KEY AUTOINCREMENT, thoughts TEXT, action TEXT, '
            'content TEXT, consumed INTEGER DEFAULT 0, woke_at TEXT, cache_info TEXT, '
            'wake_run_id TEXT, chat_id TEXT, context_id INTEGER, context_epoch INTEGER, '
            'resident_generation INTEGER)'
        )
        conn.commit()
        conn.close()
        from continuity.store import ensure_schema as ensure_continuity_schema
        conn = sqlite3.connect(db)
        ensure_continuity_schema(conn)
        conn.close()
        return db

    def _fake_context_plan(self, member=None, *, representation_kind='raw'):
        if member is None:
            member = types.SimpleNamespace(
                seq=0,
                source_ref='turn:1:2',
                source_revision='rev-turn',
                source_kind='completed_turn',
                content_hash='hash-turn',
                span_start=None,
                span_end=None,
                branch_id='active-transcript',
            )
        representation = types.SimpleNamespace(
            representation_id='raw:one',
            kind=representation_kind,
            source_members=(member,),
            estimated_tokens=4,
        )
        current_request = types.SimpleNamespace(
            kind='current_request',
            source_ref='message:3',
            content_hash='hash-current',
            estimated_tokens=1,
        )
        return types.SimpleNamespace(
            plan_id='plan:one',
            plan_hash='plan-hash-one',
            source_hash='source-hash-one',
            source_members=(member,),
            representations=(representation,),
            ordered_sections=(current_request,),
            budget_policy=types.SimpleNamespace(recent_raw_target=1),
            budget_policy_version='continuity_context_budget_v1',
            measurement_semantics='heuristic_cjk1_ascii4_v1',
            budget_status='fit',
            valid=True,
            token_budget=100,
            reserve_budget=2,
            selected_token_estimate=4,
            fixed_section_token_estimate=1,
            total_token_estimate=7,
            remaining_budget=93,
            gaps=(),
            exclusions=(),
        )

    def _fake_hot_context_plan(self):
        context_plan = self._fake_context_plan()
        context_plan.ordered_sections = (
            types.SimpleNamespace(
                kind='invariant_system',
                source_ref='system:static',
                content_hash='hash-static',
                estimated_tokens=2,
            ),
            types.SimpleNamespace(
                kind='current_request',
                source_ref='message:3',
                content_hash='hash-current',
                estimated_tokens=1,
            ),
        )
        return context_plan

    def _fake_hot_reconcile_plan(self, *, receipt_members=None):
        from chat.context_receipt import ContextReceipt

        context_plan = self._fake_hot_context_plan()
        plan = types.SimpleNamespace(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            resident_key='default:e3:g1',
            chat_id='default',
            worker_id=dr.WORKER_ID,
            manifest={'provider': 'claude_code', 'model': 'model-1'},
            hot_desired_plan=context_plan,
        )
        desired_members = dr._context_receipt_members(plan)
        members = tuple(receipt_members or desired_members)
        receipt = ContextReceipt.build(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            resident_key='default:e3:g1',
            provider='claude_code',
            model_identity='model-1',
            session_id='session-1',
            process_generation=1,
            plan_id='plan:previous',
            plan_hash='previous-plan-hash',
            budget_policy_version='continuity_context_budget_v1',
            measurement_semantics='heuristic_cjk1_ascii4_v1',
            installed_source_watermark=2,
            members=members,
        )
        plan.hot_receipt_frozen = {
            'receipt': receipt,
            'members': members,
            'receipt_missing': False,
            'receipt_members_complete': True,
            'membership_valid': True,
            'expected_receipt_revision': 0,
        }
        return plan, context_plan, desired_members


    @staticmethod
    def _historical_source(ref, revision=None):
        revision = revision or ('revision-' + ref)
        return types.SimpleNamespace(
            source_ref=ref,
            source_revision=revision,
            source_kind='completed_turn',
            content_hash='content-' + ref,
            span_start=None,
            span_end=None,
            branch_id='active-transcript',
        )

    @classmethod
    def _historical_receipt_member(
        cls,
        order,
        ref,
        *,
        representation_kind='raw',
        representation_id='raw:old',
        revision=None,
        content_hash=None,
    ):
        from chat.context_receipt import ContextReceiptMember

        return ContextReceiptMember(
            installed_order=order,
            representation_id=representation_id,
            representation_kind=representation_kind,
            source_ref=ref,
            source_revision=revision or ('revision-' + ref),
            source_kind='completed_turn',
            content_hash=content_hash or ('content-' + ref),
            branch_id='active-transcript',
        )

    def _assert_hot_historical_respawn(self, installed, desired, tail):
        compatible, reason = dr._hot_historical_compatibility(
            receipt_members=tuple(installed),
            desired_members=tuple(desired),
            native_tail_member=tail,
        )
        self.assertFalse(compatible, reason)
        self.assertEqual(reason, 'historical_representation_identity_changed'
                         if any(
                             left.representation_kind != right.representation_kind
                             or (
                                 left.representation_kind == 'chunk'
                                 and left.representation_id != right.representation_id
                             )
                             for left, right in zip(installed, desired)
                         ) else 'historical_source_members_changed')

    def _state_context_plan(self, snapshot):
        context_plan = self._fake_hot_context_plan()
        state_plan = types.SimpleNamespace(
            assembly={
                'state': '',
                'state_snapshot': dict(snapshot),
                'day_handoff_content': None,
            },
            manifest={},
        )
        fixed_sections = dr._build_continuity_shadow_fixed_sections(
            plan=state_plan,
            resident=types.SimpleNamespace(_system_text='STATIC'),
            static_system='STATIC',
        )
        current_request = next(
            section for section in context_plan.ordered_sections
            if section.kind == 'current_request'
        )
        context_plan.ordered_sections = tuple(fixed_sections) + (current_request,)
        return context_plan

    def _state_hot_reconcile_plan(self, installed_snapshot, desired_snapshot):
        from chat.context_receipt import ContextReceipt

        desired_context_plan = self._state_context_plan(desired_snapshot)
        installed_context_plan = self._state_context_plan(installed_snapshot)
        plan = types.SimpleNamespace(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            resident_key='default:e3:g1',
            chat_id='default',
            worker_id=dr.WORKER_ID,
            user_message_id=3,
            manifest={'provider': 'claude_code', 'model': 'model-1'},
            assembly={
                'state': '',
                'state_snapshot': dict(desired_snapshot),
            },
            hot_desired_plan=desired_context_plan,
        )
        desired_members = dr._context_receipt_members(plan)
        installed_plan = types.SimpleNamespace(
            continuity_plan=None,
            hot_desired_plan=installed_context_plan,
        )
        installed_members = dr._context_receipt_members(installed_plan)
        receipt = ContextReceipt.build(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            resident_key='default:e3:g1',
            provider='claude_code',
            model_identity='model-1',
            session_id='session-1',
            process_generation=1,
            plan_id='plan:previous',
            plan_hash='previous-plan-hash',
            budget_policy_version='continuity_context_budget_v1',
            measurement_semantics='heuristic_cjk1_ascii4_v1',
            installed_source_watermark=2,
            members=installed_members,
        )
        plan.hot_receipt_frozen = {
            'receipt': receipt,
            'members': installed_members,
            'receipt_missing': False,
            'receipt_members_complete': True,
            'membership_valid': True,
            'expected_receipt_revision': 0,
        }
        return plan, desired_context_plan, desired_members

    def test_hot_full_accepted_state_stays_no_op_when_transport_is_unchanged(self):
        state_a = {'emotion': 'mood=平静', 'drive': 'stress=0.10'}
        plan, context_plan, _desired_members = self._state_hot_reconcile_plan(
            state_a,
            state_a,
        )
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={
                'status': 'pass',
                'source_member': context_plan.source_members[0],
            },
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'NO_OP')

    def test_hot_full_accepted_state_change_respawns(self):
        state_a = {'emotion': 'mood=平静', 'drive': 'stress=0.10'}
        state_b = {'emotion': 'mood=焦虑', 'drive': 'stress=0.80'}
        plan, _context_plan, _desired_members = self._state_hot_reconcile_plan(
            state_a,
            state_b,
        )
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'RESPAWN')
        self.assertEqual(plan.hot_decision_reason, 'fixed_context_plan_mismatch')

    def test_context_receipt_chunk_member_uses_canonical_representation_id(self):
        context_plan = self._fake_context_plan(representation_kind='chunk')
        context_plan.representations[0].representation_id = 'chunk:canonical'
        plan = types.SimpleNamespace(
            continuity_plan=None,
            hot_desired_plan=context_plan,
        )
        members = dr._context_receipt_members(plan)
        historical = tuple(
            member for member in members if member.source_kind == 'completed_turn'
        )
        self.assertEqual(
            [member.representation_id for member in historical],
            ['chunk:canonical'],
        )
        self.assertTrue(all(':proof:' not in member.representation_id for member in historical))

    def test_raw_native_tail_allows_natural_representation_regroup(self):
        installed = [
            self._historical_receipt_member(0, 'turn:a', representation_id='raw:old'),
            self._historical_receipt_member(1, 'turn:b', representation_id='raw:old'),
        ]
        desired = [
            self._historical_receipt_member(0, 'turn:a', representation_id='raw:new'),
            self._historical_receipt_member(1, 'turn:b', representation_id='raw:new'),
            self._historical_receipt_member(2, 'turn:t', representation_id='raw:new'),
        ]
        compatible, reason = dr._hot_historical_compatibility(
            receipt_members=tuple(installed),
            desired_members=tuple(desired),
            native_tail_member=self._historical_source('turn:t'),
        )
        self.assertTrue(compatible, reason)

    def test_raw_compatibility_rejects_unproven_extra_member(self):
        installed = [
            self._historical_receipt_member(0, 'turn:a'),
            self._historical_receipt_member(1, 'turn:b'),
        ]
        desired = [
            self._historical_receipt_member(0, 'turn:a', representation_id='raw:new'),
            self._historical_receipt_member(1, 'turn:b', representation_id='raw:new'),
            self._historical_receipt_member(2, 'turn:x', representation_id='raw:new'),
            self._historical_receipt_member(3, 'turn:t', representation_id='raw:new'),
        ]
        self._assert_hot_historical_respawn(
            installed, desired, self._historical_source('turn:t'),
        )

    def test_raw_to_chunk_respawns(self):
        installed = [
            self._historical_receipt_member(0, 'turn:a'),
            self._historical_receipt_member(1, 'turn:b'),
        ]
        desired = [
            self._historical_receipt_member(
                0, 'turn:a', representation_kind='chunk',
                representation_id='chunk:one',
            ),
            self._historical_receipt_member(
                1, 'turn:b', representation_kind='chunk',
                representation_id='chunk:one',
            ),
            self._historical_receipt_member(
                2, 'turn:t', representation_id='raw:new',
            ),
        ]
        self._assert_hot_historical_respawn(
            installed, desired, self._historical_source('turn:t'),
        )

    def test_chunk_to_raw_respawns(self):
        installed = [
            self._historical_receipt_member(
                0, 'turn:a', representation_kind='chunk',
                representation_id='chunk:one',
            ),
            self._historical_receipt_member(
                1, 'turn:b', representation_kind='chunk',
                representation_id='chunk:one',
            ),
        ]
        desired = [
            self._historical_receipt_member(0, 'turn:a', representation_id='raw:new'),
            self._historical_receipt_member(1, 'turn:b', representation_id='raw:new'),
            self._historical_receipt_member(2, 'turn:t', representation_id='raw:new'),
        ]
        self._assert_hot_historical_respawn(
            installed, desired, self._historical_source('turn:t'),
        )

    def test_chunk_canonical_representation_change_respawns(self):
        installed = [
            self._historical_receipt_member(
                0, 'turn:a', representation_kind='chunk',
                representation_id='chunk:one',
            ),
            self._historical_receipt_member(
                1, 'turn:b', representation_kind='chunk',
                representation_id='chunk:one',
            ),
        ]
        desired = [
            self._historical_receipt_member(
                0, 'turn:a', representation_kind='chunk',
                representation_id='chunk:two',
            ),
            self._historical_receipt_member(
                1, 'turn:b', representation_kind='chunk',
                representation_id='chunk:two',
            ),
            self._historical_receipt_member(2, 'turn:t', representation_id='raw:new'),
        ]
        self._assert_hot_historical_respawn(
            installed, desired, self._historical_source('turn:t'),
        )

    def test_historical_raw_source_change_respawns(self):
        installed = [
            self._historical_receipt_member(0, 'turn:a'),
            self._historical_receipt_member(1, 'turn:b'),
        ]
        desired = [
            self._historical_receipt_member(0, 'turn:a', representation_id='raw:new'),
            self._historical_receipt_member(
                1, 'turn:b', representation_id='raw:new', revision='revision-b-new',
            ),
            self._historical_receipt_member(2, 'turn:t', representation_id='raw:new'),
        ]
        self._assert_hot_historical_respawn(
            installed, desired, self._historical_source('turn:t'),
        )

    def test_hot_receipt_members_include_fixed_sections_without_body(self):
        from chat.context_receipt import ContextReceiptMember

        context_plan = self._fake_hot_context_plan()
        context_plan.ordered_sections = (
            types.SimpleNamespace(
                kind='invariant_system',
                source_ref='system:static',
                content_hash='hash-static',
                estimated_tokens=2,
            ),
            types.SimpleNamespace(
                kind='accepted_state',
                source_ref='state:accepted',
                content_hash='hash-state',
                estimated_tokens=2,
            ),
            types.SimpleNamespace(
                kind='accepted_open_loops',
                source_ref='loops:accepted',
                content_hash='hash-loops',
                estimated_tokens=1,
            ),
            types.SimpleNamespace(
                kind='current_request',
                source_ref='message:3',
                content_hash='hash-current',
                estimated_tokens=1,
            ),
        )
        plan = types.SimpleNamespace(
            continuity_plan=None,
            hot_desired_plan=context_plan,
        )

        members = dr._context_receipt_members(plan)
        fixed = tuple(
            member for member in members
            if member.source_kind in dr._HOT_FIXED_SECTION_KINDS
        )
        self.assertEqual(
            [member.source_kind for member in fixed],
            ['invariant_system', 'accepted_state', 'accepted_open_loops'],
        )
        self.assertTrue(all(isinstance(member, ContextReceiptMember) for member in fixed))
        self.assertEqual(
            [member.representation_id for member in fixed],
            ['fixed:invariant_system', 'fixed:accepted_state', 'fixed:accepted_open_loops'],
        )
        self.assertEqual(
            [member.source_revision for member in fixed],
            ['hash-static', 'hash-state', 'hash-loops'],
        )
        self.assertEqual([member.span_start for member in fixed], [None, None, None])
        self.assertEqual([member.span_end for member in fixed], [None, None, None])

    def test_legacy_receipt_fixed_proof_reconciles_to_respawn(self):
        plan, _context_plan, desired_members = self._fake_hot_reconcile_plan()
        legacy_members = tuple(
            member for member in desired_members
            if member.source_kind not in dr._HOT_FIXED_SECTION_KINDS
        )
        plan.hot_receipt_frozen['members'] = legacy_members
        plan.hot_receipt_frozen['receipt_members_complete'] = True
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'RESPAWN')
        self.assertEqual(
            plan.hot_decision_reason,
            'legacy_receipt_fixed_proof_incomplete',
        )

    def test_compatible_hot_receipt_reconciles_to_no_op(self):
        plan, context_plan, _desired_members = self._fake_hot_reconcile_plan()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={
                'status': 'pass',
                'source_member': context_plan.source_members[0],
            },
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'NO_OP')
        self.assertEqual(plan.hot_decision_reason, '')
        self.assertEqual(plan.manifest['context_plan_native_tail_proof'], 'PASS')

    def test_missing_native_tail_reconciles_to_respawn(self):
        plan, _context_plan, _desired_members = self._fake_hot_reconcile_plan()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={'status': 'missing', 'reason': 'native_tail_mapping_missing'},
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'RESPAWN')
        self.assertEqual(plan.hot_decision_reason, 'native_tail_mapping_missing')

    def test_ambiguous_native_tail_blocks_without_fallback(self):
        plan, _context_plan, _desired_members = self._fake_hot_reconcile_plan()
        with mock.patch.object(
            dr,
            '_hot_live_identity_decision',
            return_value=('PASS', '', {}),
        ), mock.patch.object(
            dr,
            '_hot_native_tail_proof',
            return_value={'status': 'ambiguous', 'reason': 'multiple_canonical_tail_turns'},
        ):
            decision = dr._reconcile_hot_context_plan(plan, resident=object())
        self.assertEqual(decision, 'BLOCKED')
        self.assertEqual(plan.hot_decision_reason, 'multiple_canonical_tail_turns')

    def test_hot_receipt_freezes_durable_revision_with_one_read(self):
        from chat import context_receipt as receipt_store

        db = self._context_plan_runtime_db()
        try:
            plan, _context_plan, desired_members = self._fake_hot_reconcile_plan()
            plan.db_path = db
            resident = types.SimpleNamespace(session_id='session-1', generation=1)
            receipt = receipt_store.ContextReceipt.build(
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                resident_key='default:e3:g1',
                provider='claude_code',
                model_identity='model-1',
                session_id='session-1',
                process_generation=1,
                plan_id='plan:previous',
                plan_hash='previous-plan-hash',
                budget_policy_version='continuity_context_budget_v1',
                measurement_semantics='heuristic_cjk1_ascii4_v1',
                installed_source_watermark=2,
                members=desired_members,
            )
            conn = sqlite3.connect(db)
            receipt_store.ensure_context_receipt_schema(conn)
            receipt_store.create_receipt(conn, receipt, desired_members)
            conn.close()
            with mock.patch.object(
                receipt_store,
                'get_receipt',
                wraps=receipt_store.get_receipt,
            ) as get_receipt:
                frozen = dr._freeze_hot_receipt(plan, resident=resident)
            self.assertEqual(frozen['expected_receipt_revision'], 0)
            self.assertEqual(get_receipt.call_count, 1)
        finally:
            os.unlink(db)

    def test_hot_receipt_advance_uses_frozen_revision(self):
        from chat import context_receipt as receipt_store

        db = self._context_plan_runtime_db()
        try:
            plan, context_plan, desired_members = self._fake_hot_reconcile_plan()
            plan.db_path = db
            plan.transcript_claude_session_id = 'session-1'
            plan.transcript_process_generation = 1
            plan.transcript_start_offset = 1
            plan.transcript_end_offset = 10
            plan._same_context_last_good_proven = True
            plan.manifest.update({
                'transcript_mapping_status': 'MAPPED',
                'assistant_message_id': 3,
                'cursor_after': 3,
                'cursor_cas_success': True,
                'context_receipt_last_good_proven': True,
            })
            prior = receipt_store.ContextReceipt.build(
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                resident_key='default:e3:g1',
                provider='claude_code',
                model_identity='model-1',
                session_id='session-1',
                process_generation=1,
                plan_id='plan:previous',
                plan_hash='previous-plan-hash',
                budget_policy_version='continuity_context_budget_v1',
                measurement_semantics='heuristic_cjk1_ascii4_v1',
                installed_source_watermark=2,
                members=desired_members,
            )
            conn = sqlite3.connect(db)
            receipt_store.ensure_context_receipt_schema(conn)
            receipt_store.create_receipt(conn, prior, desired_members)
            conn.close()
            plan.hot_receipt_frozen = {
                'receipt': prior,
                'expected_receipt_revision': 0,
            }
            with mock.patch.object(
                dr.dc,
                'get_resident_history_cursor',
                return_value=3,
            ), mock.patch.object(
                dr,
                'get_same_context_last_good',
                return_value={
                    'context_id': 7,
                    'context_epoch': 3,
                    'resident_generation': 1,
                    'claude_session_id': 'session-1',
                    'transcript_end_offset': 10,
                },
            ), mock.patch.object(
                receipt_store,
                'hot_advance_receipt',
                wraps=receipt_store.hot_advance_receipt,
            ) as advance:
                plan.hot_decision = 'NO_OP'
                self.assertTrue(
                    dr._commit_production_context_receipt(
                        plan,
                        assistant_message_id=3,
                    )
                )
            self.assertEqual(
                advance.call_args.kwargs['expected_receipt_revision'],
                0,
            )
            self.assertEqual(plan.manifest['context_receipt_status'], 'COMMITTED')
            self.assertEqual(plan.manifest['context_receipt_revision'], 1)
            self.assertEqual(context_plan.plan_hash, 'plan-hash-one')
        finally:
            os.unlink(db)

    def test_hot_no_op_keeps_incremental_payload_and_user_once(self):
        plan = types.SimpleNamespace(
            hot_decision='NO_OP',
            assembly={'current_day_history': []},
            user_content='hello',
            provider_display_thinking_suffix='',
            manifest={},
        )
        dr._validate_hot_no_op_payload(plan, 'existing incremental context\nhello')
        self.assertEqual(plan.manifest['context_plan_current_request_count'], 1)
        plan.assembly['context_plan_representation_blocks'] = [
            {'kind': 'raw', 'content': 'forbidden replay'},
        ]
        with self.assertRaises(dr.DailyRuntimeError) as caught:
            dr._validate_hot_no_op_payload(plan, 'hello')
        self.assertEqual(caught.exception.error_code, 'context_plan_hot_history_replay')

    def test_hot_shadow_does_not_build_second_planner(self):
        plan = types.SimpleNamespace(
            continuity_plan=None,
            hot_desired_plan=object(),
            manifest={},
        )
        with mock.patch.object(dr, '_build_production_context_plan') as build_plan:
            dr._observe_continuity_shadow(
                plan=plan,
                resident=object(),
                static_system='',
                content='hello',
            )
        build_plan.assert_not_called()
        self.assertEqual(plan.manifest['context_plan_hot_shadow'], 'disabled')

    def test_gate_reads_existing_key_and_defaults_closed(self):
        with mock.patch.object(
            config_store,
            'get',
            side_effect=lambda key, default='': (
                '1' if key == 'CONTEXT_PLAN_CONSUMER_ENABLED' else default
            ),
        ):
            self.assertTrue(dr._context_plan_consumer_enabled())
        with mock.patch.object(
            config_store,
            'get',
            return_value='0',
        ):
            self.assertFalse(dr._context_plan_consumer_enabled())

    def test_gate_imports_config_store_when_not_preloaded(self):
        saved = sys.modules.pop('config_store', None)
        try:
            fresh_config_store = importlib.import_module('config_store')
            with mock.patch.object(
                fresh_config_store,
                'get',
                return_value='1',
            ):
                self.assertTrue(dr._context_plan_consumer_enabled())
        finally:
            sys.modules.pop('config_store', None)
            if saved is not None:
                sys.modules['config_store'] = saved

    def test_gate_on_cold_and_respawn_disables_legacy_replay(self):
        for is_cold, is_respawn, turn_kind in (
            (True, False, 'cold'),
            (False, True, 'respawn'),
        ):
            assembly = {'manifest': {}, 'current_day_history': []}
            fake_plan = self._fake_context_plan()
            with mock.patch.object(
                dr,
                '_context_plan_consumer_enabled',
                return_value=True,
            ), mock.patch.object(
                dh,
                'build_daily_window_context',
                return_value=assembly,
            ) as build_context, mock.patch.object(
                dr,
                '_build_production_context_plan',
                return_value=(fake_plan, {}),
            ), mock.patch.object(
                dr,
                '_project_context_plan_history',
                return_value=assembly,
            ):
                dr._assemble_plan(
                    req_id='request-1',
                    owner='owner-1',
                    chat_id='default',
                    local_day='2026-07-27',
                    refreshed={
                        'id': 7,
                        'context_epoch': 3,
                        'resident_generation': 1,
                    },
                    user_message_id=3,
                    user_content='current',
                    is_cold=is_cold,
                    is_respawn=is_respawn,
                    turn_kind=turn_kind,
                    cursor_before=None,
                    resident=None,
                    static_system='STATIC',
                    static_system_sha256='',
                    persona_sha256='',
                    provider='claude_code',
                    model='model-1',
                    db_path='production.db',
                    lease_acquired=True,
                    turn_lease={},
                )
            kwargs = build_context.call_args.kwargs
            self.assertFalse(kwargs['inject_handoff'])
            self.assertFalse(kwargs['inject_carryover'])
            self.assertEqual(kwargs['history_override'], [])

    def test_gate_on_cold_oversized_source_uses_canonical_plan_and_sends_once(self):
        db = self._context_plan_runtime_db()
        try:
            _insert(db, 'hayana', 'previous user', '2026-07-27 09:00:00')
            _insert(db, 'assistant', 'previous assistant', '2026-07-27 09:01:00')
            uid = _insert(db, 'hayana', 'current request', '2026-07-27 09:02:00')
            source_members = (
                types.SimpleNamespace(logical_size=40000, source_ref='turn:1'),
                types.SimpleNamespace(logical_size=40000, source_ref='turn:2'),
            )
            context_plan = types.SimpleNamespace(
                plan_id='plan:oversized-source',
                plan_hash='plan-hash-oversized-source',
                source_hash='source-hash-oversized-source',
                source_members=source_members,
                representations=(types.SimpleNamespace(
                    kind='chunk',
                    chunk_id='chunk:one',
                    representation_id='chunk:one',
                    source_members=source_members,
                    estimated_tokens=75000,
                ),),
                ordered_sections=(
                    types.SimpleNamespace(kind='invariant_system', estimated_tokens=10000),
                    types.SimpleNamespace(kind='current_request', estimated_tokens=5000),
                ),
                budget_policy=types.SimpleNamespace(
                    usable_budget=90000,
                    reserve_budget=8000,
                ),
                budget_policy_version='continuity_context_budget_v1',
                measurement_semantics='heuristic_cjk1_ascii4_v1',
                budget_status='fit',
                valid=True,
                total_token_estimate=75000,
            )
            assembly = {
                'manifest': {},
                'state': '',
                'day_handoff_content': None,
                'current_day_history': [],
                'context_plan_representation_blocks': [{
                    'kind': 'chunk',
                    'representation_id': 'chunk:one',
                    'body': 'compact canonical history',
                }],
                'layers': [],
            }
            original_get = config_store.get
            captured = {}

            def _get(key, default=None):
                if key == 'CONTEXT_PLAN_CONSUMER_ENABLED':
                    return '1'
                return original_get(key, default)

            def _build(plan, *, resident, static_system, mode='hot'):
                captured['mode'] = mode
                return context_plan, {'chunk:one': 'compact canonical history'}

            resident = _FakeResident()
            with mock.patch.object(config_store, 'get', side_effect=_get), \
                 mock.patch.object(dr, '_build_production_context_plan', side_effect=_build), \
                 mock.patch.object(dr, '_project_context_plan_history', return_value=assembly), \
                 mock.patch.object(dr, '_observe_continuity_shadow'):
                plan = _prepare_turn(
                    db,
                    uid,
                    resident=resident,
                    static_system='STATIC',
                )
                events = list(dr.stream_daily_resident_turn(
                    plan,
                    resident=resident,
                    env={},
                    static_system='STATIC',
                ))

            self.assertEqual(captured['mode'], 'cold')
            self.assertTrue(any(evt == 'done' for evt, _payload in events))
            self.assertEqual(len(resident.sent), 1)
            self.assertEqual(resident.sent[0].count('current request'), 1)
            self.assertNotEqual(
                plan.manifest.get('error_code'),
                'context_plan_legacy_selector_forbidden',
            )
            self.assertFalse(plan.manifest.get('cold_budget_overflow', False))
        finally:
            os.unlink(db)

    def test_gate_on_cold_fixed_sections_over_guard_fail_closed_before_send(self):
        db = self._context_plan_runtime_db()
        try:
            uid = _insert(db, 'hayana', 'current request', '2026-07-27 09:02:00')
            resident = _FakeResident()
            assembly = {'manifest': {}, 'current_day_history': []}
            oversized = dr.DailyRuntimeError(
                'canonical ContextPlan is invalid',
                error_code='context_plan_invalid',
            )
            original_get = config_store.get

            def _get(key, default=None):
                if key == 'CONTEXT_PLAN_CONSUMER_ENABLED':
                    return '1'
                return original_get(key, default)

            def _reject_fixed_overflow(plan, *, resident, static_system, mode='hot'):
                self.assertEqual(mode, 'cold')
                self.assertGreater(len(static_system), 70000)
                raise oversized

            with mock.patch.object(config_store, 'get', side_effect=_get), \
                 mock.patch.object(
                     dh,
                     'build_daily_window_context',
                     return_value=assembly,
                 ), mock.patch.object(
                     dr,
                     '_build_production_context_plan',
                     side_effect=_reject_fixed_overflow,
                 ):
                with self.assertRaises(dr.DailyRuntimeError) as raised:
                    _prepare_turn(
                        db,
                        uid,
                        resident=resident,
                        static_system='S' * 70001,
                    )

            self.assertEqual(raised.exception.error_code, 'context_plan_invalid')
            self.assertEqual(resident.sent, [])
        finally:
            os.unlink(db)

    def test_gate_off_cold_and_respawn_keep_legacy_assembly_contract(self):
        for is_cold, is_respawn, turn_kind in (
            (True, False, 'cold'),
            (False, True, 'respawn'),
        ):
            assembly = {'manifest': {}, 'current_day_history': []}
            with mock.patch.object(
                dr,
                '_context_plan_consumer_enabled',
                return_value=False,
            ), mock.patch.object(
                dh,
                'build_daily_window_context',
                return_value=assembly,
            ) as build_context, mock.patch.object(
                dr,
                '_build_production_context_plan',
            ) as build_plan:
                plan = dr._assemble_plan(
                    req_id='request-1',
                    owner='owner-1',
                    chat_id='default',
                    local_day='2026-07-27',
                    refreshed={
                        'id': 7,
                        'context_epoch': 3,
                        'resident_generation': 1,
                    },
                    user_message_id=3,
                    user_content='current',
                    is_cold=is_cold,
                    is_respawn=is_respawn,
                    turn_kind=turn_kind,
                    cursor_before=None,
                    resident=None,
                    static_system='STATIC',
                    static_system_sha256='',
                    persona_sha256='',
                    provider='claude_code',
                    model='model-1',
                    db_path='production.db',
                    lease_acquired=True,
                    turn_lease={},
                )
            kwargs = build_context.call_args.kwargs
            self.assertTrue(kwargs['inject_handoff'])
            self.assertTrue(kwargs['inject_carryover'])
            self.assertNotIn('history_override', kwargs)
            self.assertIsNone(plan.continuity_plan)
            build_plan.assert_not_called()

    def test_gate_on_hot_and_capacity_keep_original_authority(self):
        for turn_kind in ('hot', 'capacity_swap'):
            assembly = {'manifest': {}, 'current_day_history': []}
            with mock.patch.object(
                dr,
                '_context_plan_consumer_enabled',
                return_value=True,
            ), mock.patch.object(
                dh,
                'build_daily_window_context',
                return_value=assembly,
            ) as build_context, mock.patch.object(
                dr,
                '_build_production_context_plan',
            ) as build_plan:
                plan = dr._assemble_plan(
                    req_id='request-1',
                    owner='owner-1',
                    chat_id='default',
                    local_day='2026-07-27',
                    refreshed={
                        'id': 7,
                        'context_epoch': 3,
                        'resident_generation': 1,
                    },
                    user_message_id=3,
                    user_content='current',
                    is_cold=False,
                    is_respawn=False,
                    turn_kind=turn_kind,
                    cursor_before=2,
                    resident=None,
                    static_system='STATIC',
                    static_system_sha256='',
                    persona_sha256='',
                    provider='claude_code',
                    model='model-1',
                    db_path='production.db',
                    lease_acquired=True,
                    turn_lease={},
                )
            kwargs = build_context.call_args.kwargs
            self.assertFalse(kwargs['inject_handoff'])
            self.assertFalse(kwargs['inject_carryover'])
            self.assertNotIn('history_override', kwargs)
            self.assertIsNone(plan.continuity_plan)
            build_plan.assert_not_called()

    def test_gate_on_ready_chunk_projection_has_no_raw_duplicate(self):
        plan = types.SimpleNamespace(
            db_path='production.db',
            user_message_id=3,
            assembly={
                'layers': [],
                'current_day_history': [],
                'carryover_messages': [],
                'day_handoff': '',
                'day_handoff_content': {
                    'open_loops': ['must not replay full handoff'],
                },
            },
            user_content='current request',
        )
        representation = types.SimpleNamespace(
            representation_id='chunk:ready',
            kind='chunk',
            chunk_id='chunk:ready',
            source_members=(types.SimpleNamespace(source_ref='turn:1:2'),),
        )
        context_plan = types.SimpleNamespace(
            representations=(representation,),
            ordered_sections=(types.SimpleNamespace(
                kind='older_continuity',
                source_ref='chunk:ready',
                content_hash='chunk-hash',
                estimated_tokens=3,
            ), types.SimpleNamespace(
                kind='current_request',
                source_ref='message:3',
                content_hash='current-hash',
                estimated_tokens=1,
            )),
        )
        assembly = dr._project_context_plan_history(
            plan,
            context_plan=context_plan,
            chunk_bodies={'chunk:ready': 'sealed chunk body'},
        )
        self.assertEqual(
            [item['message_id'] for item in assembly['current_day_history']],
            [0],
        )
        self.assertEqual(
            [item['body'] for item in assembly['context_plan_representation_blocks']],
            ['sealed chunk body'],
        )
        content = dr.format_resident_turn_content(
            assembly=assembly,
            user_content=plan.user_content,
            is_cold=True,
            is_respawn=False,
        )
        self.assertEqual(content.count('sealed chunk body'), 1)
        self.assertNotIn('must not replay full handoff', content)
        self.assertEqual(content.count('current request'), 1)

    def test_gate_on_unavailable_corrupt_and_invalid_budget_block_before_send(self):
        cases = (
            ('context_plan_store_unavailable',
             types.SimpleNamespace(status='unavailable', artifacts=())),
            ('context_plan_store_corrupt',
             types.SimpleNamespace(status='corrupt', artifacts=())),
        )
        for error_code, surface in cases:
            resident = _FakeResident()
            assembly = {'manifest': {}, 'current_day_history': []}
            with mock.patch.object(
                dr,
                '_context_plan_consumer_enabled',
                return_value=True,
            ), mock.patch.object(
                dh,
                'build_daily_window_context',
                return_value=assembly,
            ), mock.patch.object(
                dr,
                '_context_plan_policy',
                return_value=(types.SimpleNamespace(), 'policy-v1'),
            ), mock.patch(
                'continuity.store.read_ready_surface',
                return_value=surface,
            ), mock.patch.object(
                dr,
                '_observe_continuity_shadow',
            ) as observe_shadow:
                with self.assertRaises(dr.DailyRuntimeError) as raised:
                    dr._assemble_plan(
                        req_id='request-1',
                        owner='owner-1',
                        chat_id='default',
                        local_day='2026-07-27',
                        refreshed={
                            'id': 7,
                            'context_epoch': 3,
                            'resident_generation': 1,
                        },
                        user_message_id=3,
                        user_content='current',
                        is_cold=True,
                        is_respawn=False,
                        turn_kind='cold',
                        cursor_before=None,
                        resident=resident,
                        static_system='STATIC',
                        static_system_sha256='',
                        persona_sha256='',
                        provider='claude_code',
                        model='model-1',
                        db_path='production.db',
                        lease_acquired=True,
                        turn_lease={},
                    )
            self.assertEqual(raised.exception.error_code, error_code)
            self.assertEqual(resident.sent, [])
            observe_shadow.assert_not_called()

        resident = _FakeResident()
        with mock.patch.object(
            dr,
            '_context_plan_consumer_enabled',
            return_value=True,
        ), mock.patch.object(
            dh,
            'build_daily_window_context',
            return_value={'manifest': {}, 'current_day_history': []},
        ), mock.patch.object(
            dr,
            '_context_plan_policy',
            side_effect=ValueError('invalid budget'),
        ), mock.patch.object(dr, '_observe_continuity_shadow') as observe_shadow:
            with self.assertRaises(ValueError):
                dr._assemble_plan(
                    req_id='request-1',
                    owner='owner-1',
                    chat_id='default',
                    local_day='2026-07-27',
                    refreshed={
                        'id': 7,
                        'context_epoch': 3,
                        'resident_generation': 1,
                    },
                    user_message_id=3,
                    user_content='current',
                    is_cold=True,
                    is_respawn=False,
                    turn_kind='cold',
                    cursor_before=None,
                    resident=resident,
                    static_system='STATIC',
                    static_system_sha256='',
                    persona_sha256='',
                    provider='claude_code',
                    model='model-1',
                    db_path='production.db',
                    lease_acquired=True,
                    turn_lease={},
                )
        self.assertEqual(resident.sent, [])
        observe_shadow.assert_not_called()

    def test_production_plan_binds_only_strict_validated_ready_artifacts(self):
        artifact = types.SimpleNamespace(
            chunk_id='chunk:validated',
            artifact_revision='artifact-revision',
            body_hash='body-hash',
            body='validated body',
        )
        representation = types.SimpleNamespace(
            kind='chunk',
            chunk_id='chunk:validated',
            provenance=(
                ('artifact_revision', 'artifact-revision'),
                ('body_hash', 'body-hash'),
            ),
        )
        context_plan = types.SimpleNamespace(
            plan_id='plan:' + ('a' * 32),
            plan_hash='a' * 64,
            source_hash='b' * 64,
            valid=True,
            representations=(representation,),
        )
        result = types.SimpleNamespace(plan=context_plan)
        plan = types.SimpleNamespace(
            db_path='production.db',
            user_message_id=3,
            assembly={'state': '', 'day_handoff_content': None},
        )
        with mock.patch.object(
            dr,
            '_context_plan_policy',
            return_value=(types.SimpleNamespace(), 'policy-v1'),
        ), mock.patch(
            'continuity.store.read_ready_surface',
            return_value=types.SimpleNamespace(
                status='ready',
                artifacts=(artifact,),
            ),
        ), mock.patch.object(
            dr,
            '_build_continuity_shadow_fixed_sections',
            return_value=(),
        ), mock.patch(
            'chat.daily_continuity_shadow.build_daily_continuity_shadow_plan',
            return_value=result,
        ) as adapter:
            _plan, bodies = dr._build_production_context_plan(
                plan,
                resident=types.SimpleNamespace(),
                static_system='STATIC',
            )
        self.assertEqual(bodies, {'chunk:validated': 'validated body'})
        self.assertEqual(
            adapter.call_args.kwargs['validated_ready_artifacts'],
            ({
                'chunk_id': 'chunk:validated',
                'artifact_revision': 'artifact-revision',
                'body_hash': 'body-hash',
            },),
        )

    def test_gate_on_cold_and_respawn_empty_send_current_request_once(self):
        original_get = config_store.get
        for cold_reprepare in (False, True):
            db = self._context_plan_runtime_db()
            try:
                _insert(db, 'hayana', 'previous user', '2026-07-27 09:00:00')
                _insert(db, 'assistant', 'previous assistant', '2026-07-27 09:01:00')
                uid = _insert(db, 'hayana', 'current request', '2026-07-27 09:02:00')

                def _get(key, default=None):
                    if key == 'CONTEXT_PLAN_CONSUMER_ENABLED':
                        return '1'
                    if key == 'CONTEXT_PLAN_TOKEN_BUDGET':
                        return '1000'
                    if key == 'CONTEXT_PLAN_RESERVE_BUDGET':
                        return '0'
                    if key == 'CONTEXT_PLAN_RECENT_RAW_TARGET':
                        return '100'
                    return original_get(key, default)

                resident = _FakeResident()
                with mock.patch.object(config_store, 'get', side_effect=_get):
                    plan = _prepare_turn(
                        db,
                        uid,
                        resident=resident,
                        static_system='STATIC',
                        _cold_reprepare=cold_reprepare,
                    )
                    events = list(dr.stream_daily_resident_turn(
                        plan,
                        resident=resident,
                        env={},
                        static_system='STATIC',
                    ))
                self.assertTrue(any(evt == 'done' for evt, _payload in events))
                self.assertEqual(len(resident.sent), 1)
                self.assertEqual(resident.sent[0].count('current request'), 1)
                self.assertEqual(plan.manifest['context_plan_consumer'], 'canonical')
                self.assertEqual(plan.manifest['context_plan_fixed_section_parity'], 'PASS')
            finally:
                os.unlink(db)

    def test_gate_off_cold_and_respawn_real_send_once(self):
        original_get = config_store.get
        for cold_reprepare in (False, True):
            db = self._context_plan_runtime_db()
            try:
                _insert(db, 'hayana', 'previous user', '2026-07-27 09:00:00')
                _insert(db, 'assistant', 'previous assistant', '2026-07-27 09:01:00')
                uid = _insert(db, 'hayana', 'current request', '2026-07-27 09:02:00')

                def _get(key, default=None):
                    if key == 'CONTEXT_PLAN_CONSUMER_ENABLED':
                        return '0'
                    return original_get(key, default)

                resident = _FakeResident()
                with mock.patch.object(config_store, 'get', side_effect=_get):
                    plan = _prepare_turn(
                        db,
                        uid,
                        resident=resident,
                        static_system='STATIC',
                        _cold_reprepare=cold_reprepare,
                    )
                    events = list(dr.stream_daily_resident_turn(
                        plan,
                        resident=resident,
                        env={},
                        static_system='STATIC',
                    ))
                self.assertTrue(any(evt == 'done' for evt, _payload in events))
                self.assertEqual(len(resident.sent), 1)
                self.assertIn('previous assistant', resident.sent[0])
                self.assertEqual(resident.sent[0].count('current request'), 1)
                self.assertIsNone(plan.continuity_plan)
            finally:
                os.unlink(db)

    def test_projection_uses_selected_raw_and_excludes_current_request(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'old user', '2026-07-27 09:00:00')
            _insert(db, 'assistant', 'old reply', '2026-07-27 09:01:00')
            _insert(db, 'hayana', 'current', '2026-07-27 09:02:00')
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM chat_messages ORDER BY id').fetchall()
            conn.close()
            members = build_source_members(
                derive_completed_turns(rows[:2]),
                derive_autonomous_events(rows[:2]),
            )
            plan = types.SimpleNamespace(
                db_path=db,
                user_message_id=3,
                assembly={'layers': [], 'current_day_history': []},
            )
            assembly = dr._project_context_plan_history(
                plan,
                context_plan=self._fake_context_plan(members[0]),
                chunk_bodies={},
            )
            self.assertEqual(
                [item['message_id'] for item in assembly['current_day_history']],
                [1, 2],
            )
            self.assertNotIn(3, {
                int(item['message_id'])
                for item in assembly['current_day_history']
            })
        finally:
            os.unlink(db)

    def test_durable_receipt_carries_plan_hash(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            plan = types.SimpleNamespace(
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                resident_key='default:e3:g1',
                db_path=db,
                user_message_id=3,
                transcript_claude_session_id='session-1',
                transcript_process_generation=1,
                manifest={
                    'transcript_mapping_status': 'MAPPED',
                    'provider': 'claude_code',
                    'model': 'model-1',
                    'assistant_message_id': 4,
                    'cursor_after': 4,
                    'cursor_cas_success': True,
                    'context_receipt_last_good_proven': True,
                },
                continuity_plan=self._fake_context_plan(),
            )
            with mock.patch.object(
                dc,
                'get_resident_history_cursor',
                return_value=4,
            ), mock.patch.object(
                dr,
                'get_same_context_last_good',
                return_value={
                    'context_id': 7,
                    'context_epoch': 3,
                    'resident_generation': 1,
                    'claude_session_id': 'session-1',
                    'transcript_end_offset': 100,
                },
            ):
                plan.transcript_start_offset = 10
                plan.transcript_end_offset = 100
                plan._same_context_last_good_proven = True
                self.assertTrue(
                    dr._commit_production_context_receipt(
                        plan,
                        assistant_message_id=4,
                    )
                )
            from chat.context_receipt import get_receipt
            conn = sqlite3.connect(db)
            receipt = get_receipt(
                conn,
                context_id=7,
                context_epoch=3,
                resident_generation=1,
            )
            conn.close()
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt.plan_hash, 'plan-hash-one')
            self.assertEqual(receipt.installed_source_watermark, 4)
        finally:
            os.unlink(db)

    def test_gate_on_raw_projection_preserves_canonical_tool_outcomes(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            _insert(db, 'hayana', 'formal user', '2026-07-27 09:00:00')
            assistant_id = _insert(
                db, 'assistant', 'assistant body', '2026-07-27 09:01:00',
            )
            _insert(db, 'hayana', 'current request', '2026-07-27 09:02:00')
            conn = sqlite3.connect(db)
            conn.execute(
                'UPDATE chat_messages SET tool_calls=? WHERE id=?',
                (json.dumps([{
                    'name': 'mcp__home__get_todos',
                    'args': {'limit': 3},
                    'result': {'ok': True, 'items': ['one']},
                    'success': True,
                    'artifact': None,
                    'diff': None,
                }], ensure_ascii=False), assistant_id),
            )
            conn.commit()
            conn.row_factory = sqlite3.Row
            rows = conn.execute('SELECT * FROM chat_messages ORDER BY id').fetchall()
            conn.close()
            members = build_source_members(
                derive_completed_turns(rows[:2]),
                derive_autonomous_events(rows[:2]),
            )
            plan = types.SimpleNamespace(
                db_path=db,
                user_message_id=3,
                user_content='current request',
                assembly={'layers': [], 'current_day_history': []},
            )
            assembly = dr._project_context_plan_history(
                plan,
                context_plan=self._fake_context_plan(members[0]),
                chunk_bodies={},
            )
            content = dr.format_resident_turn_content(
                assembly=assembly,
                user_content=plan.user_content,
                is_cold=True,
                is_respawn=False,
            )
            self.assertIn('USER:', content)
            self.assertIn('formal user', content)
            self.assertIn('ASSISTANT:', content)
            self.assertIn('assistant body', content)
            self.assertIn('TOOL OUTCOME:', content)
            self.assertIn('mcp__home__get_todos', content)
            self.assertIn('"ok":true', content)
            self.assertEqual(content.count('current request'), 1)
        finally:
            os.unlink(db)

    def _receipt_plan(self, db, *, proof=True):
        _init_chat_messages(db)
        manifest = {
            'transcript_mapping_status': 'MAPPED',
            'provider': 'claude_code',
            'model': 'model-1',
            'assistant_message_id': 4,
            'cursor_after': 4,
            'cursor_cas_success': True,
            'context_receipt_last_good_proven': bool(proof),
        }
        plan = types.SimpleNamespace(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            resident_key='default:e3:g1',
            db_path=db,
            user_message_id=3,
            transcript_claude_session_id='session-1',
            transcript_process_generation=1,
            transcript_start_offset=10,
            transcript_end_offset=100,
            manifest=manifest,
            continuity_plan=self._fake_context_plan(),
        )
        plan._same_context_last_good_proven = bool(proof)
        return plan

    def test_receipt_requires_current_last_good_proof_without_resend(self):
        db = _tmp_db()
        try:
            plan = self._receipt_plan(db, proof=False)
            with mock.patch.object(dc, 'get_resident_history_cursor', return_value=4), \
                 mock.patch.object(dr, 'get_same_context_last_good', return_value=None):
                self.assertFalse(
                    dr._commit_production_context_receipt(
                        plan,
                        assistant_message_id=4,
                    )
                )
            self.assertEqual(plan.manifest['context_receipt_status'], 'REPAIR_REQUIRED')
            self.assertEqual(
                plan.manifest['context_receipt_error_code'],
                'context_receipt_last_good_unproven',
            )
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0],
                0,
            )
            conn.close()
        finally:
            os.unlink(db)

    def test_receipt_rejects_missing_cursor_after_without_resend(self):
        db = _tmp_db()
        try:
            plan = self._receipt_plan(db)
            plan.manifest['cursor_after'] = None
            plan.manifest['cursor_cas_success'] = False
            self.assertFalse(
                dr._commit_production_context_receipt(
                    plan,
                    assistant_message_id=4,
                )
            )
            self.assertEqual(plan.manifest['context_receipt_status'], 'REPAIR_REQUIRED')
            self.assertEqual(
                plan.manifest['context_receipt_error_code'],
                'context_receipt_cursor_unavailable',
            )
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM context_receipts').fetchone()[0],
                0,
            )
            conn.close()
        finally:
            os.unlink(db)

    def test_receipt_commit_failure_does_not_trigger_resend(self):
        db = _tmp_db()
        try:
            plan = self._receipt_plan(db)
            with mock.patch.object(dc, 'get_resident_history_cursor', return_value=4), \
                 mock.patch.object(
                     dr,
                     'get_same_context_last_good',
                     return_value={
                         'context_id': 7,
                         'context_epoch': 3,
                         'resident_generation': 1,
                         'claude_session_id': 'session-1',
                         'transcript_end_offset': 100,
                     },
                 ), mock.patch.object(
                     dc,
                     '_connect',
                     side_effect=sqlite3.OperationalError('commit unavailable'),
                 ):
                self.assertFalse(
                    dr._commit_production_context_receipt(
                        plan,
                        assistant_message_id=4,
                    )
                )
            self.assertEqual(plan.manifest['context_receipt_status'], 'REPAIR_REQUIRED')
            self.assertTrue(plan.manifest['context_receipt_repair_required'])
            self.assertEqual(
                plan.manifest['context_receipt_error_code'],
                'context_receipt_unavailable',
            )
        finally:
            os.unlink(db)


class CapacityContextPlanSplitCarrierTests(unittest.TestCase):
    def test_capacity_first_stdin_contains_chunks_and_fixed_content_only(self):
        assembly = {
            'capacity_context_bootstrap': True,
            'context_plan_representation_blocks': [{
                'kind': 'chunk',
                'representation_id': 'chunk:ready-1',
                'body': 'CHUNK_CANONICAL',
            }],
            'context_plan_accepted_open_loops': ['OPEN_LOOP'],
            'state': 'ACCEPTED_STATE_SNAPSHOT',
            'current_day_history': [{
                'role': 'user',
                'content': 'RAW_REPLAY_FORBIDDEN',
            }],
            'day_handoff': 'LEGACY_HANDOFF_FORBIDDEN',
            'carryover_messages': [{
                'role': 'assistant',
                'content': 'LEGACY_CARRYOVER_FORBIDDEN',
            }],
        }
        content = dr.format_resident_turn_content(
            assembly=assembly,
            user_content='CURRENT_REQUEST',
            is_cold=False,
            is_respawn=False,
        )
        self.assertEqual(content.count('CHUNK_CANONICAL'), 1)
        self.assertEqual(content.count('OPEN_LOOP'), 1)
        self.assertEqual(content.count('ACCEPTED_STATE_SNAPSHOT'), 1)
        self.assertEqual(content.count('CURRENT_REQUEST'), 1)
        self.assertNotIn('RAW_REPLAY_FORBIDDEN', content)
        self.assertNotIn('LEGACY_HANDOFF_FORBIDDEN', content)
        self.assertNotIn('LEGACY_CARRYOVER_FORBIDDEN', content)

    def _capacity_install_fixture(self, current_text):
        from continuity.sources import evidence_ref

        db = _tmp_db()
        _init_chat_messages(db)
        current_id = _insert(
            db,
            'user',
            current_text,
            '2026-07-27 09:00:00',
        )
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute(
            'SELECT * FROM chat_messages WHERE id=?',
            (current_id,),
        ).fetchone())
        conn.close()
        current_evidence = evidence_ref(row, prefix='message')
        target_system = (
            'STATIC' + dr.CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1
        )
        chunk_body = (
            'chunk carrier: %s / %s' % (current_text, current_text)
        )
        assembly = {
            'capacity_context_bootstrap': True,
            'context_plan_representation_blocks': [{
                'kind': 'chunk',
                'representation_id': 'chunk:one',
                'body': chunk_body,
            }],
            'context_plan_accepted_open_loops': [
                current_text,
                current_text,
            ],
            'day_handoff_content': {
                'open_loops': [current_text, current_text],
            },
            'state': '',
            'current_day_history': [],
            'day_handoff': '',
            'carryover_messages': [],
        }
        scaffold = types.SimpleNamespace(
            assembly=assembly,
            manifest={},
            db_path=db,
        )
        fixed_sections = dr._build_continuity_shadow_fixed_sections(
            plan=scaffold,
            resident=None,
            static_system=target_system,
            require_full_state_snapshot=True,
        )
        current_section = types.SimpleNamespace(
            kind='current_request',
            source_ref='message:%d' % current_id,
            content_hash=str(current_evidence.content_hash),
            estimated_tokens=int(current_evidence.logical_size),
        )
        chunk_representation = types.SimpleNamespace(
            representation_id='chunk:one',
            kind='chunk',
            chunk_id='chunk:one',
            source_members=(),
            estimated_tokens=4,
        )
        context_plan = types.SimpleNamespace(
            representations=(chunk_representation,),
            ordered_sections=tuple(fixed_sections) + (current_section,),
        )
        plan = types.SimpleNamespace(
            capacity_context_plan=context_plan,
            continuity_plan=None,
            hot_desired_plan=None,
            user_message_id=current_id,
            user_content=current_text,
            db_path=db,
            assembly=assembly,
            manifest={
                'capacity_context_current_user_in_candidate': False,
                'capacity_context_raw_carrier_parity': 'PASS',
                'capacity_context_raw_source_refs': (),
                'capacity_context_install_parity': 'PENDING',
            },
            capacity_context_chunk_bodies={
                'chunk:one': chunk_body,
            },
        )
        resident = types.SimpleNamespace(_system_text=target_system)
        content = dr.format_resident_turn_content(
            assembly=assembly,
            user_content=current_text,
            is_cold=False,
            is_respawn=False,
        )
        return db, plan, resident, target_system, content

    def test_capacity_presend_observation_failure_rolls_back_before_send(self):
        """The outer guard covers production install observation failures."""
        dr.reset_bindings_for_tests()
        plan = types.SimpleNamespace(
            request_id='capacity-presend',
            chat_id='default',
            context_id=7,
            context_epoch=3,
            resident_generation=2,
            resident_key='default:e3:g2',
            user_message_id=1,
            epoch_token={},
            lease_owner='test-owner',
            is_cold=False,
            is_respawn=False,
            cursor_before=0,
            assembly={},
            manifest={
                'provider': 'claude_code',
                'model': 'model-1',
            },
            user_content='好',
            user_image_url='',
            user_attachments=(),
            provider_display_thinking_suffix='',
            db_path='test.db',
            worker_id='test-worker',
            tool_profile='test-profile',
            turn_lease={},
            capacity_context_plan=object(),
            continuity_plan=None,
            hot_desired_plan=None,
            _capacity_swap_install_state={'old_proc': object()},
            _capacity_swap_deferred_old_proc=object(),
            _current_user_stdin_flushed=False,
        )
        resident = mock.Mock()
        resident.generation = 2
        resident._system_text = 'TARGET SYSTEM'
        resident.ensure_alive.return_value = False
        heartbeat = mock.Mock()
        heartbeat.failed = False
        heartbeat.stop.return_value = False
        parity_failure = dr.DailyRuntimeError(
            'forced install parity failure',
            error_code='context_plan_current_request_parity_failed',
        )
        try:
            with mock.patch.object(
                dr,
                'verify_epoch_token',
            ), mock.patch.object(
                dr,
                'LeaseHeartbeat',
                return_value=heartbeat,
            ), mock.patch.object(
                dr,
                'peek_registered_respawn_decision',
                return_value={'requires_respawn': False},
            ), mock.patch.object(
                dr,
                'format_resident_turn_content',
                return_value='TARGET CONTENT',
            ), mock.patch.object(
                dr.dc,
                'get_resident_history_cursor',
                return_value=0,
            ), mock.patch.object(
                dr.dc,
                'upsert_resident_owner',
            ), mock.patch.object(
                dr,
                '_capture_transcript_start',
            ), mock.patch.object(
                dr,
                '_validate_hot_no_op_payload',
            ), mock.patch.object(
                dr,
                '_observe_continuity_shadow',
                side_effect=parity_failure,
            ), mock.patch.object(
                dr,
                'rollback_capacity_swap_install',
            ) as rollback, mock.patch.object(
                dr,
                'try_restore_same_context_last_good',
            ) as fallback:
                with self.assertRaises(dr.DailyRuntimeError) as raised:
                    list(dr.ensure_resident_and_stream(
                        plan,
                        resident=resident,
                        env={},
                        static_system='TARGET SYSTEM',
                    ))
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_current_request_parity_failed',
            )
            rollback.assert_called_once()
            fallback.assert_not_called()
            resident.send_turn.assert_not_called()
            self.assertTrue(plan.manifest['capacity_swap_pre_flush_blocked'])
            self.assertIsNone(plan._capacity_swap_install_state)
        finally:
            dr.reset_bindings_for_tests()

    def test_capacity_presend_rollback_1_restores_before_send(self):
        """CAPACITY-PRESEND-ROLLBACK-1: parity failure cannot send."""
        plan = types.SimpleNamespace(
            capacity_context_plan=object(),
            manifest={},
            _capacity_swap_install_state={'old_proc': object()},
            _capacity_swap_deferred_old_proc=object(),
            _current_user_stdin_flushed=False,
        )
        resident = mock.Mock()
        failure = dr.DailyRuntimeError(
            'parity failed',
            error_code='context_plan_current_request_parity_failed',
        )
        with mock.patch.object(
            dr,
            'rollback_capacity_swap_install',
        ) as rollback, mock.patch.object(
            dr,
            'try_restore_same_context_last_good',
        ) as fallback:
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._raise_capacity_pre_send_failure(
                    plan,
                    resident=resident,
                    exc=failure,
                )
        self.assertEqual(
            raised.exception.error_code,
            'context_plan_current_request_parity_failed',
        )
        self.assertTrue(plan.manifest['capacity_swap_pre_flush_blocked'])
        self.assertIsNone(plan._capacity_swap_install_state)
        rollback.assert_called_once()
        fallback.assert_not_called()
        resident.send_turn.assert_not_called()

    def test_capacity_presend_rollback_2_rollback_failure_no_send(self):
        """CAPACITY-PRESEND-ROLLBACK-2: failed restore is terminal."""
        plan = types.SimpleNamespace(
            capacity_context_plan=object(),
            manifest={},
            _capacity_swap_install_state={'old_proc': object()},
            _capacity_swap_deferred_old_proc=object(),
            _current_user_stdin_flushed=False,
        )
        resident = mock.Mock()
        failure = dr.DailyRuntimeError(
            'parity failed',
            error_code='context_plan_current_request_parity_failed',
        )
        with mock.patch.object(
            dr,
            'rollback_capacity_swap_install',
            side_effect=RuntimeError('restore failed'),
        ):
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._raise_capacity_pre_send_failure(
                    plan,
                    resident=resident,
                    exc=failure,
                )
        self.assertEqual(
            raised.exception.error_code,
            'context_plan_capacity_rollback_unproven',
        )
        self.assertTrue(
            plan.manifest['capacity_swap_pre_flush_rollback_unproven']
        )
        resident.send_turn.assert_not_called()

    def test_capacity_actual_renderer_receipt_binds_each_carrier(self):
        """ACTUAL-RENDER-RECEIPT: receipt records the provider payload render."""
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('好')
        )
        try:
            receipt = plan.assembly['_context_install_render_receipt']
            payload_hash, payload_tokens, payload_kind = (
                dr._continuity_shadow_fingerprint(content)
            )
            self.assertEqual(
                receipt['representation_ids'],
                ['chunk:one'],
            )
            self.assertEqual(
                receipt['representation_body_hashes'],
                [dr._sha256_text(plan.capacity_context_chunk_bodies['chunk:one'])],
            )
            self.assertEqual(
                receipt['fixed_section_kinds'],
                ['accepted_open_loops'],
            )
            self.assertEqual(
                receipt['fixed_section_body_hashes'],
                [dr._sha256_text(
                    dr._format_context_plan_open_loops(
                        plan.assembly['context_plan_accepted_open_loops'],
                    ),
                )],
            )
            self.assertEqual(receipt['current_request_slots'], 1)
            self.assertEqual(receipt['current_request_carrier'], 'tail')
            self.assertEqual(receipt['final_payload_hash'], payload_hash)
            self.assertEqual(
                receipt['final_payload_token_estimate'],
                payload_tokens,
            )
            self.assertEqual(receipt['final_payload_kind'], payload_kind)
            dr._validate_production_context_install(
                plan=plan,
                resident=resident,
                static_system=target_system,
                content=content,
            )
            self.assertEqual(
                plan.manifest['capacity_context_install_parity'],
                'PASS',
            )
        finally:
            os.unlink(db)

    def test_capacity_render_proof_1_chunk_omission_fails_closed(self):
        """RENDER-PROOF-1: omitted chunk is rejected before provider send."""
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('CURRENT_CHUNK')
        )
        try:
            chunk_body = plan.capacity_context_chunk_bodies['chunk:one']
            self.assertEqual(content.count(chunk_body), 1)
            omitted = content.replace(chunk_body, '', 1)
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=omitted,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_render_receipt_mismatch',
            )
            self.assertNotEqual(
                plan.manifest['capacity_context_install_parity'],
                'PASS',
            )
        finally:
            os.unlink(db)

    def test_capacity_render_proof_2_current_omission_or_duplication_fails_closed(self):
        """RENDER-PROOF-2: CURRENT is absent or installed twice."""
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('CURRENT_SLOT')
        )
        try:
            current_text = plan.user_content
            self.assertTrue(content.endswith(current_text))
            actual_payloads = (
                content[:-len(current_text)],
                content + current_text,
            )
            for actual in actual_payloads:
                with self.subTest(payload=actual):
                    plan.manifest['capacity_context_install_parity'] = 'PENDING'
                    with self.assertRaises(dr.DailyRuntimeError) as raised:
                        dr._validate_production_context_install(
                            plan=plan,
                            resident=resident,
                            static_system=target_system,
                            content=actual,
                        )
                    self.assertEqual(
                        raised.exception.error_code,
                        'context_plan_render_receipt_mismatch',
                    )
                    self.assertNotEqual(
                        plan.manifest['capacity_context_install_parity'],
                        'PASS',
                    )
        finally:
            os.unlink(db)

    def test_capacity_render_proof_3_post_receipt_payload_mutation_fails_closed(self):
        """RENDER-PROOF-3: a frozen receipt rejects later payload mutation."""
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('CURRENT_REQUEST')
        )
        try:
            mutated = content + '\nPOST_RENDER_MUTATION'
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=mutated,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_render_receipt_mismatch',
            )
            self.assertNotEqual(
                plan.manifest['capacity_context_install_parity'],
                'PASS',
            )
        finally:
            os.unlink(db)

    def test_capacity_current_request_structural_proof_allows_literal_collisions(self):
        """Short and repeated prose do not look like duplicate carriers."""
        for current_text in ('好', '重复请求文本'):
            with self.subTest(current_text=current_text):
                db, plan, resident, target_system, content = (
                    self._capacity_install_fixture(current_text)
                )
                try:
                    self.assertGreaterEqual(content.count(current_text), 5)
                    self.assertEqual(
                        plan.assembly['_context_install_render_receipt'][
                            'current_request_slots'
                        ],
                        1,
                    )
                    self.assertEqual(
                        plan.manifest['capacity_context_install_parity'],
                        'PENDING',
                    )
                    dr._validate_production_context_install(
                        plan=plan,
                        resident=resident,
                        static_system=target_system,
                        content=content,
                    )
                    self.assertEqual(
                        plan.manifest['capacity_context_install_parity'],
                        'PASS',
                    )
                finally:
                    os.unlink(db)

    def test_capacity_current_source_cannot_be_a_second_forged_carrier(self):
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('重复请求文本')
        )
        try:
            plan.manifest['capacity_context_current_user_in_candidate'] = True
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_capacity_current_user_in_candidate',
            )
            self.assertNotEqual(
                plan.manifest['capacity_context_install_parity'],
                'PASS',
            )
        finally:
            os.unlink(db)

    def test_capacity_install_parity_failure_never_reaches_pass(self):
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('好')
        )
        try:
            self.assertEqual(
                plan.manifest['capacity_context_install_parity'],
                'PENDING',
            )
            plan.assembly['_context_install_render_receipt'][
                'current_request_slots'
            ] = 2
            with self.assertRaises(dr.DailyRuntimeError):
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertNotEqual(
                plan.manifest['capacity_context_install_parity'],
                'PASS',
            )
        finally:
            os.unlink(db)

    def test_capacity_source_receipt_supersession_uses_frozen_revision_once(self):
        from chat import context_receipt as receipt_store

        db = _tmp_db()
        try:
            _init_chat_messages(db)
            conn = sqlite3.connect(db)
            receipt_store.ensure_context_receipt_schema(conn)
            source = receipt_store.ContextReceipt.build(
                context_id=7,
                context_epoch=3,
                resident_generation=1,
                resident_key='default:e3:g1',
                provider='claude_code',
                model_identity='model-1',
                session_id='source-session',
                process_generation=1,
                plan_id='plan:source',
                plan_hash='source-hash',
                budget_policy_version='continuity_context_budget_v1',
                measurement_semantics='heuristic_cjk1_ascii4_v1',
                installed_source_watermark=2,
                members=(),
            )
            receipt_store.create_receipt(conn, source, ())
            conn.close()

            plan = types.SimpleNamespace(
                context_id=7,
                context_epoch=3,
                resident_generation=2,
                db_path=db,
                manifest={},
                capacity_source_receipt_frozen={
                    'context_id': 7,
                    'context_epoch': 3,
                    'resident_generation': 1,
                    'receipt': source,
                    'expected_receipt_revision': 0,
                },
            )
            self.assertTrue(
                dr._commit_capacity_source_receipt_supersession(
                    plan,
                    target_receipt_committed=True,
                )
            )
            self.assertEqual(
                plan.manifest['capacity_source_receipt_supersession'],
                'COMMITTED',
            )
            conn = sqlite3.connect(db)
            row = conn.execute(
                'SELECT receipt_revision, result, superseded_by_generation '
                'FROM context_receipts '
                'WHERE context_id=7 AND context_epoch=3 AND resident_generation=1',
            ).fetchone()
            conn.close()
            self.assertEqual(row, (1, 'superseded', 2))
            self.assertTrue(
                dr._commit_capacity_source_receipt_supersession(
                    plan,
                    target_receipt_committed=True,
                )
            )
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
