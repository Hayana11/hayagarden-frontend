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


class _SessionAppearsResident(_FakeResident):
    def send_turn(self, content, commit_meta=None, turn_lease=None):
        self.session_id = 'session-abc'
        yield from super().send_turn(
            content,
            commit_meta=commit_meta,
            turn_lease=turn_lease,
        )


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

    def test_cold_first_turn_success_backfills_binding_session_identity(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'hello', '2026-07-27 10:00:00')
            with mock.patch.object(
                dr,
                '_context_plan_consumer_enabled',
                return_value=False,
            ):
                plan = self._prepare(db, uid)
            resident = _SessionAppearsResident()
            with mock.patch.object(
                dr,
                'snapshot_session_jsonl',
                return_value={'path': '/tmp/session-abc.jsonl', 'offset': 1},
            ):
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
            initial_binding = dr.get_local_binding()
            self.assertIsNotNone(initial_binding)
            self.assertIsNone(initial_binding.claude_session_id)
            self.assertEqual(plan.transcript_claude_session_id, 'session-abc')
            self.assertEqual(plan.transcript_process_generation, 1)

            plan.manifest['transcript_mapping_status'] = 'MAPPED'
            aid = dr.persist_daily_assistant_for_plan(
                plan, content='reply', thinking='', tool_calls='',
                cache_info='', choices='',
            )
            out = dr.complete_daily_turn(plan, assistant_message_id=aid)

            binding = dr.get_local_binding()
            self.assertEqual(out['cursor_after'], aid)
            self.assertIsNotNone(binding)
            self.assertEqual(binding.claude_session_id, 'session-abc')
            self.assertEqual(binding.bound_cursor_message_id, aid)
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
            self.assertIn('health.read', first.turn_lease['allowed_capabilities'])
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

    @staticmethod
    def _session_backfill_plan(session_id):
        return types.SimpleNamespace(
            resident_key='default:e1:g1',
            context_epoch=1,
            resident_generation=1,
            tool_profile=dr.DAILY_TOOL_PROFILE,
            manifest={'transcript_mapping_status': 'MAPPED'},
            transcript_claude_session_id=session_id,
            transcript_process_generation=1,
        )

    @staticmethod
    def _session_backfill_binding(session_id=None):
        return dr.LocalResidentBinding(
            resident_key='default:e1:g1',
            context_id=7,
            context_epoch=1,
            resident_generation=1,
            bound_cursor_message_id=3,
            process_generation=1,
            tool_profile=dr.DAILY_TOOL_PROFILE,
            claude_session_id=session_id,
        )

    def test_close_with_expected_key_and_no_binding_is_noop(self):
        resident = _FakeResident()
        resident._alive = True
        self.assertFalse(dr.close_local_resident_if_bound(resident, expected_key='daily:x:1:1'))
        self.assertEqual(resident.killed, 0)

    def test_empty_binding_backfills_only_from_mapped_session_identity(self):
        plan = self._session_backfill_plan('session-abc')
        binding = self._session_backfill_binding()
        self.assertTrue(
            dr._backfill_local_binding_session_identity(plan, binding=binding)
        )
        self.assertEqual(binding.claude_session_id, 'session-abc')

    def test_existing_same_session_is_preserved(self):
        plan = self._session_backfill_plan('session-abc')
        binding = self._session_backfill_binding('session-abc')
        self.assertFalse(
            dr._backfill_local_binding_session_identity(plan, binding=binding)
        )
        self.assertEqual(binding.claude_session_id, 'session-abc')

    def test_session_mismatch_is_fail_closed_without_overwrite(self):
        plan = self._session_backfill_plan('session-new')
        binding = self._session_backfill_binding('session-old')
        self.assertFalse(
            dr._backfill_local_binding_session_identity(plan, binding=binding)
        )
        self.assertEqual(binding.claude_session_id, 'session-old')

    def test_missing_session_proof_does_not_write_a_guess(self):
        plan = self._session_backfill_plan(None)
        binding = self._session_backfill_binding()
        self.assertFalse(
            dr._backfill_local_binding_session_identity(plan, binding=binding)
        )
        self.assertIsNone(binding.claude_session_id)

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
                with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(dr, 'LeaseHeartbeat', _Heartbeat))
            stack.enter_context(mock.patch.object(dr, 'verify_epoch_token'))
            stack.enter_context(
                mock.patch.object(dr, 'get_local_binding', return_value=None)
            )
            stack.enter_context(
                mock.patch.object(dr, '_resident_is_alive', return_value=False)
            )
            stack.enter_context(
                mock.patch.object(
                    dr,
                    'peek_registered_respawn_decision',
                    return_value={},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    dr,
                    'format_resident_turn_content',
                    return_value='body',
                )
            )
            stack.enter_context(
                mock.patch.object(
                    dr,
                    '_apply_daily_cold_prompt_fence',
                    side_effect=lambda _plan, **kwargs: kwargs['content'],
                )
            )
            stack.enter_context(mock.patch.object(dr, 'set_local_binding'))
            stack.enter_context(
                mock.patch.object(
                    dr.dc,
                    'get_resident_history_cursor',
                    return_value=0,
                )
            )
            stack.enter_context(mock.patch.object(dr.dc, 'upsert_resident_owner'))
            stack.enter_context(mock.patch.object(dr, '_capture_transcript_start'))
            stack.enter_context(mock.patch.object(dr, '_capture_transcript_end'))
            stack.enter_context(
                mock.patch.object(dr, '_validate_hot_no_op_payload')
            )
            stack.enter_context(
                mock.patch.object(
                    dr,
                    '_observe_continuity_shadow',
                    side_effect=_observe,
                )
            ):
            events = list(dr.ensure_resident_and_stream(
                plan,
                resident=resident,
                env={},
                static_system=raw,
            ))

        effective = raw + '\n\n' + carrier
        expected_hash = hashlib.sha256(effective.encode()).hexdigest()
        self.assertEqual(captured['static_system'], effective)
        self.assertEqual(resident._spawn_args[0][0], effective)
        self.assertEqual(observed['static_system'], effective)
        self.assertEqual(
            observed['fixed_sections'][0].content_hash,
            expected_hash,
        )
        self.assertEqual(plan.manifest['static_system_sha256'], expected_hash)
        self.assertEqual(plan._provider_prompt_system_snapshot, effective)
        self.assertEqual(effective.count(carrier), 1)
        self.assertTrue(any(evt == 'done' for evt, _payload in events))
        self.assertEqual(resident._system_text, effective)

    def test_respawn_uses_same_effective_system_authority_as_cold(self):
        raw = 'RESPAWN STATIC SYSTEM'
        carrier = '<global_flow_guide>RESPAWN GUIDE</global_flow_guide>'
        hidden_plan = self._hidden_plan_for_system_identity(
            enabled=True,
            eligible=True,
            carrier=carrier,
        )
        cold_plan, cold_capture = self._assemble_identity_plan(
            static_system=raw,
            hidden_plan=hidden_plan,
            is_cold=True,
            is_respawn=False,
        )
        respawn_plan, respawn_capture = self._assemble_identity_plan(
            static_system=raw,
            hidden_plan=hidden_plan,
            is_cold=False,
            is_respawn=True,
        )
        expected = raw + '\n\n' + carrier
        expected_hash = hashlib.sha256(expected.encode()).hexdigest()
        self.assertEqual(cold_capture['static_system'], expected)
        self.assertEqual(respawn_capture['static_system'], expected)
        self.assertEqual(
            cold_plan.manifest['static_system_sha256'],
            expected_hash,
        )
        self.assertEqual(
            respawn_plan.manifest['static_system_sha256'],
            expected_hash,
        )

    def test_inconsistent_global_guide_fixed_section_still_fails_closed(self):
        raw = 'COLD STATIC SYSTEM'
        carrier = '<global_flow_guide>GUIDE</global_flow_guide>'
        hidden_plan = self._hidden_plan_for_system_identity(
            enabled=True,
            eligible=True,
            carrier=carrier,
        )
        resident = _FakeResident()
        plan, _captured = self._assemble_identity_plan(
            static_system=raw,
            hidden_plan=hidden_plan,
            resident=resident,
        )
        effective = raw + '\n\n' + carrier
        stale_sections = dr._build_continuity_shadow_fixed_sections(
            plan=plan,
            resident=resident,
            static_system=raw,
            require_full_state_snapshot=True,
        )
        plan.continuity_plan = types.SimpleNamespace(
            ordered_sections=stale_sections,
        )
        with self.assertRaises(dr.DailyRuntimeError) as raised:
            dr._validate_production_context_install(
                plan=plan,
                resident=resident,
                static_system=effective,
                content='body',
            )
        self.assertEqual(
            raised.exception.error_code,
            'context_plan_fixed_section_parity_failed',
        )
        self.assertEqual(
            plan.manifest['context_plan_fixed_section_parity'],
            'FAILED',
        )
        self.assertEqual(
            plan.manifest['context_plan_fixed_section_diagnostic']['mismatch_type'],
            'content_hash',
        )

    def test_cold_rebuild_uses_latest_ready_chunk_plan(self):
        assembly = {'manifest': {}, 'current_day_history': []}
        chunk_plan = types.SimpleNamespace(
            plan_id='plan:cold-chunk',
            plan_hash='cold-chunk-hash',
            source_hash='cold-source-hash',
            valid=True,
            budget_status='fit',
        )
        captured = {}

        def _build(plan, *, resident, static_system, mode='hot'):
            captured['mode'] = mode
            return chunk_plan, {'chunk:one': 'compressed'}

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
            '_build_production_context_plan',
            side_effect=_build,
        ), mock.patch.object(
            dr,
            '_project_context_plan_history',
            return_value=assembly,
        ):
            plan = dr._assemble_plan(
                req_id='request-1',
                owner='owner-1',
                chat_id='default',
                local_day='2026-07-27',
                refreshed={
                    'id': 7,
                    'context_epoch': 3,
                    'resident_generation': 2,
                },
                user_message_id=3,
                user_content='current',
                is_cold=True,
                is_respawn=False,
                turn_kind='cold',
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
        self.assertEqual(captured['mode'], 'cold')
        self.assertIs(plan.continuity_plan, chunk_plan)
        self.assertEqual(plan.manifest['context_plan_id'], 'plan:cold-chunk')

    def test_gate_off_hot_does_not_reconcile_chunk_availability(self):
        assembly = {'manifest': {}, 'current_day_history': []}
        with mock.patch.object(
            dr,
            '_context_plan_consumer_enabled',
            return_value=False,
        ), mock.patch.object(
            dh,
            'build_daily_window_context',
            return_value=assembly,
        ), mock.patch.object(
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
                turn_kind='hot',
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
        build_plan.assert_not_called()
        self.assertFalse(plan.manifest.get('context_plan_hot_pending'))
        self.assertIsNone(plan.hot_desired_plan)

    def test_capacity_rebuild_selects_latest_ready_chunk(self):
        chunk_plan = types.SimpleNamespace(
            plan_id='plan:capacity-chunk',
            plan_hash='capacity-chunk-hash',
            source_hash='capacity-source-hash',
            valid=True,
            budget_status='fit',
        )
        plan = types.SimpleNamespace(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            cursor_before=2,
            manifest={},
            db_path='production.db',
        )
        captured = {}

        def _build(plan_arg, *, resident, static_system, mode='hot'):
            captured['mode'] = mode
            captured['resident'] = resident
            return chunk_plan, {'chunk:one': 'compressed'}

        with mock.patch.object(
            dr,
            '_context_plan_consumer_enabled',
            return_value=True,
        ), mock.patch.object(
            dr,
            '_freeze_hot_receipt',
            return_value={'expected_receipt_revision': 0},
        ), mock.patch.object(
            dr,
            '_build_production_context_plan',
            side_effect=_build,
        ), mock.patch.object(
            dr,
            '_freeze_capacity_context_bootstrap',
            return_value={'ok': True},
        ), mock.patch.object(
            dr,
            'prepare_capacity_swap_for_context_plan',
            side_effect=dr.DailyRuntimeError(
                'stop after latest plan',
                error_code='capacity_test_stop',
            ),
        ):
            result = dr._attempt_capacity_swap_before_stdin(
                plan,
                resident=object(),
                static_system='STATIC',
                env={},
                trigger_reason='hard_context',
            )
        self.assertEqual(captured['mode'], 'capacity')
        self.assertIsNone(captured['resident'])
        self.assertIs(plan.capacity_context_plan, chunk_plan)
        self.assertEqual(result.get('error_code'), 'capacity_test_stop')

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

    def _native_tail_fixture(
        self,
        *,
        assistant_offsets=(200,),
        assistant_sessions=None,
        user_offset=100,
        last_good_end=400,
        duplicate_user=False,
    ):
        from continuity.sources import evidence_ref

        offsets = tuple(assistant_offsets)
        sessions = tuple(
            assistant_sessions
            if assistant_sessions is not None
            else ('session-1',) * len(offsets)
        )
        if len(sessions) != len(offsets):
            raise AssertionError('assistant session fixture length mismatch')
        db = self._context_plan_runtime_db()
        conn = sqlite3.connect(db)
        conn.execute(
            'DROP INDEX IF EXISTS uq_chat_message_claude_events_user_msg'
        )
        user_id = int(conn.execute(
            'INSERT INTO chat_messages '
            '(author, content, source_kind, created_at) VALUES (?,?,?,?)',
            ('hayana', 'native-tail-user', 'chat', '2026-07-27 10:00:00'),
        ).lastrowid)
        assistant_id = int(conn.execute(
            'INSERT INTO chat_messages '
            '(author, content, source_kind, created_at) VALUES (?,?,?,?)',
            ('assistant', 'native-tail-assistant', 'chat', '2026-07-27 10:00:01'),
        ).lastrowid)
        for message_id, role in ((user_id, 'user'), (assistant_id, 'assistant')):
            conn.execute(
                'INSERT INTO daily_message_contexts '
                '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
                'VALUES (?,?,?,?,?,?)',
                (message_id, 7, 3, 1, role, '2026-07-27 10:00:00'),
            )
        mapping_rows = [
            (
                'u-native-tail',
                user_id,
                'user',
                'session-1',
                user_offset,
            ),
        ]
        mapping_rows.extend(
            (
                'a-native-tail-%d' % index,
                assistant_id,
                'assistant',
                sessions[index],
                offset,
            )
            for index, offset in enumerate(offsets)
        )
        if duplicate_user:
            mapping_rows.append(
                (
                    'u-native-tail-duplicate',
                    user_id,
                    'user',
                    'session-1',
                    120,
                )
            )
        conn.executemany(
            '''INSERT INTO chat_message_claude_events (
                event_uuid, message_id, role, claude_session_id,
                context_id, context_epoch, resident_generation,
                jsonl_byte_offset
            ) VALUES (?,?,?,?,?,?,?,?)''',
            (
                (
                    event_uuid,
                    message_id,
                    role,
                    session_id,
                    7,
                    3,
                    1,
                    offset,
                )
                for event_uuid, message_id, role, session_id, offset
                in mapping_rows
            ),
        )
        conn.commit()
        row = conn.execute(
            'SELECT * FROM chat_messages WHERE id=?', (user_id,)
        ).fetchone()
        columns = tuple(column[1] for column in conn.execute(
            'PRAGMA table_info(chat_messages)'
        ).fetchall())
        request_evidence = evidence_ref(
            dict(zip(columns, row)),
            prefix='message',
        )
        conn.close()
        request_member = types.SimpleNamespace(
            source_kind='current_request',
            source_ref='message:%d' % user_id,
            source_revision=request_evidence.source_revision,
            content_hash=request_evidence.content_hash,
        )
        plan = types.SimpleNamespace(
            db_path=db,
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            user_message_id=assistant_id + 1,
        )
        frozen = {
            'receipt': types.SimpleNamespace(
                installed_source_watermark=assistant_id,
            ),
            'members': (request_member,),
        }
        registry = {
            'scan_status': 'READY',
            'last_mapped_message_id': assistant_id,
            'scan_offset': last_good_end,
            'claude_session_id': 'session-1',
        }
        return db, plan, frozen, registry

    def _native_tail_result(self, **kwargs):
        db, plan, frozen, registry = self._native_tail_fixture(**kwargs)
        try:
            with mock.patch.object(
                dr,
                'get_same_context_last_good',
                return_value={
                    'context_id': 7,
                    'context_epoch': 3,
                    'resident_generation': 1,
                    'claude_session_id': 'session-1',
                    'transcript_end_offset': kwargs.get('last_good_end', 400),
                },
            ):
                return dr._hot_native_tail_proof(
                    plan,
                    frozen=frozen,
                    registry=registry,
                )
        finally:
            os.unlink(db)

    def test_native_tail_user_one_assistant_one_passes(self):
        result = self._native_tail_result(assistant_offsets=(200,))
        self.assertEqual(result['status'], 'pass')

    def test_native_tail_user_one_assistant_two_passes_and_keeps_rows(self):
        db, plan, frozen, registry = self._native_tail_fixture(
            assistant_offsets=(200, 300),
        )
        try:
            with mock.patch.object(
                dr,
                'get_same_context_last_good',
                return_value={
                    'context_id': 7,
                    'context_epoch': 3,
                    'resident_generation': 1,
                    'claude_session_id': 'session-1',
                    'transcript_end_offset': 400,
                },
            ):
                result = dr._hot_native_tail_proof(
                    plan,
                    frozen=frozen,
                    registry=registry,
                )
            self.assertEqual(result['status'], 'pass')
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM chat_message_claude_events "
                    "WHERE role='assistant'"
                ).fetchone()[0],
                2,
            )
            conn.close()
        finally:
            os.unlink(db)

    def test_native_tail_assistant_zero_is_incomplete(self):
        result = self._native_tail_result(assistant_offsets=())
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'native_tail_mapping_incomplete')

    def test_native_tail_duplicate_user_mapping_is_ambiguous(self):
        result = self._native_tail_result(
            assistant_offsets=(200,),
            duplicate_user=True,
        )
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['reason'], 'native_tail_mapping_ambiguous')

    def test_native_tail_assistant_session_mismatch_fails_closed(self):
        result = self._native_tail_result(
            assistant_offsets=(200, 300),
            assistant_sessions=('session-other', 'session-1'),
        )
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'native_tail_session_mismatch')

    def test_native_tail_assistant_offset_before_user_fails_closed(self):
        result = self._native_tail_result(assistant_offsets=(90, 200))
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'native_tail_mapping_offset_invalid')

    def test_native_tail_assistant_offset_at_or_after_end_fails_closed(self):
        result = self._native_tail_result(
            assistant_offsets=(200, 400),
            last_good_end=400,
        )
        self.assertEqual(result['status'], 'missing')
        self.assertEqual(result['reason'], 'native_tail_mapping_offset_invalid')

    def test_native_tail_corrupt_assistant_offset_fails_closed(self):
        result = self._native_tail_result(assistant_offsets=('corrupt',))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['reason'], 'native_tail_mapping_offset_corrupt')

    def test_native_tail_legal_multi_assistant_is_not_ambiguous(self):
        result = self._native_tail_result(assistant_offsets=(200, 300, 350))
        self.assertEqual(result['status'], 'pass')
        self.assertNotEqual(result.get('reason'), 'native_tail_mapping_ambiguous')

    def test_native_tail_duplicate_assistant_offsets_are_ambiguous(self):
        result = self._native_tail_result(assistant_offsets=(200, 200))
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['reason'], 'native_tail_mapping_offset_ambiguous')

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
            plan.transcript_model_identity = 'model-1'
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
                transcript_model_identity='explicit:claude-opus-5-5',
                manifest={
                    'transcript_mapping_status': 'MAPPED',
                    'provider': 'claude_code',
                    'model': '[display-only] claude-opus-4-6-thinking',
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
            self.assertEqual(receipt.model_identity, 'explicit:claude-opus-5-5')
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
            transcript_model_identity='model-1',
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
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            is_cold=False,
            is_respawn=False,
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

    def test_fixed_section_set_failure_records_hash_only_diagnostic(self):
        sentinel = 'FIXED_SECTION_SET_SENTINEL'
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture(sentinel)
        )
        try:
            plan.capacity_context_plan.ordered_sections = (
                plan.capacity_context_plan.ordered_sections[1:]
            )
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_fixed_section_parity_failed',
            )
            diagnostic = plan.manifest['context_plan_fixed_section_diagnostic']
            self.assertEqual(diagnostic['scope'], 'fixed_sections')
            self.assertEqual(diagnostic['mismatch_type'], 'section_set')
            self.assertNotIn(sentinel, json.dumps(diagnostic, ensure_ascii=False))
            self.assertEqual(plan.manifest['context_plan_fixed_section_parity'], 'FAILED')
        finally:
            os.unlink(db)

    def test_fixed_section_source_ref_mismatch_is_classified(self):
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('SOURCE_REF_DIAGNOSTIC_SENTINEL')
        )
        try:
            sections = list(plan.capacity_context_plan.ordered_sections)
            original = sections[0]
            sections[0] = types.SimpleNamespace(
                kind=original.kind,
                source_ref='different-source-ref',
                content_hash=original.content_hash,
                estimated_tokens=original.estimated_tokens,
            )
            plan.capacity_context_plan.ordered_sections = tuple(sections)
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_fixed_section_parity_failed',
            )
            diagnostic = plan.manifest['context_plan_fixed_section_diagnostic']
            self.assertEqual(diagnostic['mismatch_type'], 'source_ref')
            self.assertEqual(
                diagnostic['identity_field_mismatches'],
                ['source_ref'],
            )
            self.assertNotIn(
                'different-source-ref',
                json.dumps(diagnostic, ensure_ascii=False),
            )
        finally:
            os.unlink(db)

    def test_fixed_section_estimated_tokens_mismatch_is_classified(self):
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture('ESTIMATED_TOKENS_DIAGNOSTIC_SENTINEL')
        )
        try:
            sections = list(plan.capacity_context_plan.ordered_sections)
            original = sections[0]
            sections[0] = types.SimpleNamespace(
                kind=original.kind,
                source_ref=original.source_ref,
                content_hash=original.content_hash,
                estimated_tokens=int(original.estimated_tokens) + 1,
            )
            plan.capacity_context_plan.ordered_sections = tuple(sections)
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_fixed_section_parity_failed',
            )
            diagnostic = plan.manifest['context_plan_fixed_section_diagnostic']
            self.assertEqual(
                diagnostic['mismatch_type'],
                'estimated_tokens',
            )
            self.assertEqual(
                diagnostic['identity_field_mismatches'],
                ['estimated_tokens'],
            )
        finally:
            os.unlink(db)

    def test_fixed_body_hash_failure_records_hash_only_diagnostic(self):
        sentinel = 'FIXED_BODY_HASH_SENTINEL'
        db, plan, resident, target_system, content = (
            self._capacity_install_fixture(sentinel)
        )
        try:
            plan.assembly['_context_install_render_receipt'][
                'fixed_section_body_hashes'
            ] = ['0' * 64]
            with self.assertRaises(dr.DailyRuntimeError) as raised:
                dr._validate_production_context_install(
                    plan=plan,
                    resident=resident,
                    static_system=target_system,
                    content=content,
                )
            self.assertEqual(
                raised.exception.error_code,
                'context_plan_fixed_section_parity_failed',
            )
            diagnostic = plan.manifest['context_plan_fixed_section_diagnostic']
            self.assertEqual(diagnostic['scope'], 'fixed_carrier')
            self.assertEqual(diagnostic['mismatch_type'], 'body_hash')
            self.assertEqual(diagnostic['actual_hashes'], ['0' * 64])
            self.assertNotIn(sentinel, json.dumps(diagnostic, ensure_ascii=False))
        finally:
            os.unlink(db)

    def test_accepted_state_and_open_loops_presence_failures_are_hash_only(self):
        cases = (
            ('accepted_state', 'state', 'ACCEPTED_STATE_SENTINEL'),
            ('accepted_open_loops', 'open_loops', 'ACCEPTED_OPEN_LOOPS_SENTINEL'),
        )
        for scope, field, sentinel in cases:
            with self.subTest(scope=scope):
                db, plan, resident, target_system, content = (
                    self._capacity_install_fixture(sentinel)
                )
                try:
                    if field == 'state':
                        plan.assembly['state'] = sentinel
                    else:
                        plan.assembly['context_plan_accepted_open_loops'] = []
                    with self.assertRaises(dr.DailyRuntimeError) as raised:
                        dr._validate_production_context_install(
                            plan=plan,
                            resident=resident,
                            static_system=target_system,
                            content=content,
                        )
                    self.assertEqual(
                        raised.exception.error_code,
                        'context_plan_fixed_section_parity_failed',
                    )
                    diagnostic = plan.manifest[
                        'context_plan_fixed_section_diagnostic'
                    ]
                    self.assertEqual(diagnostic['scope'], scope)
                    self.assertEqual(
                        diagnostic['mismatch_type'],
                        'section_presence',
                    )
                    self.assertEqual(
                        diagnostic['expected_section_kinds'],
                        [scope] if field == 'state' else [],
                    )
                    self.assertNotIn(
                        sentinel,
                        json.dumps(diagnostic, ensure_ascii=False),
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


from types import SimpleNamespace


class HiddenFlowHotNoopParityTests(unittest.TestCase):
    def _plan(self, *, block='', enabled=False, suffix='', assembly=None):
        return SimpleNamespace(
            hot_decision='NO_OP',
            assembly=dict(assembly or {}),
            user_content='hello',
            provider_display_thinking_suffix=suffix,
            hidden_flow_enabled=enabled,
            hidden_flow_eligible=enabled,
            hidden_flow_plan=(
                SimpleNamespace(private_request_block=block)
                if enabled else None
            ),
            manifest={},
        )

    def _assert_rejected(self, plan, content):
        with self.assertRaises(dr.DailyRuntimeError) as raised:
            dr._validate_hot_no_op_payload(plan, content)
        self.assertEqual(
            raised.exception.error_code,
            'context_plan_hot_hidden_flow_private_payload_invalid',
        )

    def test_gate_off_plain_hot_noop_remains_valid(self):
        plan = self._plan()
        dr._validate_hot_no_op_payload(plan, 'hello')
        self.assertEqual(plan.manifest['context_plan_current_request_count'], 1)

    def test_gate_on_available_block_and_active_guidance_are_valid(self):
        for block in (
            '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>',
            '<hidden_flow_guidance flow="demo-flow" stage="approach"></hidden_flow_guidance>',
        ):
            with self.subTest(block=block):
                plan = self._plan(block=block, enabled=True)
                content = 'hello' + chr(10) + chr(10) + block
                original = content
                dr._validate_hot_no_op_payload(plan, content)
                self.assertEqual(content, original)
                self.assertEqual(
                    plan.manifest['context_plan_current_request_count'],
                    1,
                )

    def test_display_thinking_suffix_is_checked_after_private_payload(self):
        block = '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>'
        suffix = chr(10) + chr(10) + 'DISPLAY_THINKING_SUFFIX'
        plan = self._plan(block=block, enabled=True, suffix=suffix)
        dr._validate_hot_no_op_payload(
            plan,
            'hello' + suffix + chr(10) + chr(10) + block,
        )

    def test_multimodal_removes_only_final_exact_private_text_part(self):
        block = '<hidden_flow_guidance flow="demo-flow" stage="approach"></hidden_flow_guidance>'
        suffix = chr(10) + chr(10) + 'DISPLAY_THINKING_SUFFIX'
        plan = self._plan(block=block, enabled=True, suffix=suffix)
        image = {
            'type': 'image',
            'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='},
        }
        content = [
            {'type': 'text', 'text': 'hello'},
            image,
            {'type': 'text', 'text': suffix},
            {'type': 'text', 'text': chr(10) + chr(10) + block},
        ]
        original = [dict(item) if isinstance(item, dict) else item for item in content]
        dr._validate_hot_no_op_payload(plan, content)
        self.assertEqual(content, original)

    def test_private_payload_missing_repeated_misplaced_or_tampered_fails_closed(self):
        block = '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>'
        plan = self._plan(block=block, enabled=True)
        cases = (
            'hello',
            'hello' + chr(10) + chr(10) + block + chr(10) + chr(10) + block,
            'hello' + chr(10) + chr(10) + block + 'tail',
            'hello' + chr(10) + chr(10) + block.replace('demo-flow', 'other-flow'),
        )
        for content in cases:
            with self.subTest(content=content):
                self._assert_rejected(plan, content)

    def test_private_payload_without_authorized_plan_fails_closed(self):
        block = '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>'
        self._assert_rejected(
            self._plan(),
            'hello' + chr(10) + chr(10) + block,
        )

    def test_original_history_handoff_guards_remain_active(self):
        block = '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>'
        for assembly, expected in (
            ({'context_plan_representation_blocks': [{'body': 'replayed'}]},
             'context_plan_hot_history_replay'),
            ({'day_handoff': 'handoff'},
             'context_plan_hot_legacy_replay'),
        ):
            with self.subTest(expected=expected):
                plan = self._plan(block=block, enabled=True, assembly=assembly)
                with self.assertRaises(dr.DailyRuntimeError) as raised:
                    dr._validate_hot_no_op_payload(
                        plan,
                        'hello' + chr(10) + chr(10) + block,
                    )
                self.assertEqual(raised.exception.error_code, expected)


if __name__ == '__main__':
    unittest.main()
