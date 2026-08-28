"""P-CONTEXT-DAILY-SOFT-WINDOW-R1 resident integration tests — no model calls."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
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
from chat import daily_runtime as dr
from chat.daily_context import ConflictError, DeferredError
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_READY,
    get_context_claude_session,
    register_context_claude_session,
)
from chat.system_builder import build_cc_daily_static_parts, build_cc_static_parts
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
                aid = dr.persist_daily_assistant_for_plan(plan, content='daily reply')
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
            err_evt = next(e for e in events if e.get('t') == 'err')
            done_evt = next(e for e in events if e.get('t') == 'done')
            self.assertEqual(err_evt.get('code'), 'cursor_cas_conflict')
            self.assertFalse(done_evt.get('ok'))
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
            err_evt = next(e for e in events if e.get('t') == 'err')
            done_evt = next(e for e in events if e.get('t') == 'done')
            self.assertEqual(err_evt.get('code'), 'cursor_cas_conflict')
            self.assertFalse(done_evt.get('ok'))
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
                    'chat.daily_context.advance_resident_history_cursor',
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
        ])
        yield ('done', ('cold reply', '', {'input_tokens': 2, 'output_tokens': 4}, {}))


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
        ])
        yield ('done', ('after-respawn', '', {'input_tokens': 1, 'output_tokens': 2}, {}))


class _MappingBlockedResident(_TranscriptColdResident):
    """Writes JSONL with no candidate_user → Mapping BLOCKED after chat success."""

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

        aid = dr.persist_daily_assistant_for_plan(plan, content='hot reply')
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text='hot reply',
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

        aid = dr.persist_daily_assistant_for_plan(plan, content='cold reply')
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text='cold reply',
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
        aid = dr.persist_daily_assistant_for_plan(plan, content='after-respawn')
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text='after-respawn',
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

    def test_mapping_blocked_does_not_fail_chat(self):
        """4) Mapping BLOCKED after persist+cursor; chat still succeeds."""
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
        aid = dr.persist_daily_assistant_for_plan(plan, content='blocked-map reply')
        out = dr.handle_provider_success(
            plan, assistant_message_id=aid, raw_text='blocked-map reply',
        )
        self.assertEqual(out['transcript_mapping_status'], 'BLOCKED')
        self.assertIsNotNone(out['transcript_mapping_error_code'])
        self.assertEqual(int(out['transcript_mapping_event_count']), 0)
        self.assertTrue(out.get('cursor_cas_success'))
        self.assertEqual(out.get('assistant_message_id'), aid)
        self.assertTrue(plan.lease_released)
        self.assertFalse(dc.is_resident_turn_active(
            plan.context_id, plan.resident_generation, db_path=self.db, now=_FIXED_NOW,
        ))

        conn = sqlite3.connect(self.db)
        asst = conn.execute(
            "SELECT id, content FROM chat_messages WHERE author='assistant'"
        ).fetchone()
        map_count = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(int(asst[0]), aid)
        self.assertEqual(asst[1], 'blocked-map reply')
        self.assertEqual(int(map_count), 0)

        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_BLOCKED)
        self.assertEqual(int(reg['scan_offset']), 0)


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
                     'chat.cold_bootstrap_budget.cold_prompt_target',
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
                     mock.patch.object(dr, 'finalize_transcript_mapping_after_success', return_value={}):
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
                with mock.patch.dict(sys.modules, {'command_store': failing_store}), \
                     mock.patch.object(
                         dr.dh,
                         'build_daily_window_context',
                         return_value={'current_day_history': [], 'manifest': {}},
                     ), \
                     mock.patch(
                         'chat.cold_bootstrap_budget.cold_prompt_target',
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
                 mock.patch.object(dr, 'finalize_transcript_mapping_after_success', return_value={}):
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
                 mock.patch.object(dr, 'finalize_transcript_mapping_after_success', return_value={}):
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
                 mock.patch.object(dr, 'finalize_transcript_mapping_after_success', return_value={}):
                with self.assertRaises(dr.CursorCASConflictAfterPersist):
                    dr.handle_provider_success(
                        plan, assistant_message_id=101, raw_text='回复',
                    )
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
                 mock.patch.object(dr, 'finalize_transcript_mapping_after_success', return_value={}):
                out = dr.handle_provider_success(
                    plan, assistant_message_id=102, raw_text='成功回复',
                )
            self.assertEqual(out['feedback_consume_status'], 'FAILED')
            self.assertEqual(out['feedback_consume_error'], 'RuntimeError')
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
