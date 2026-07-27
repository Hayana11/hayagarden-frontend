"""P-CONTEXT-DAILY-SOFT-WINDOW-R1 resident integration tests — no model calls."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident
import config_store
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.daily_context import ConflictError, DeferredError
from chat.system_builder import build_cc_daily_static_parts, build_cc_static_parts


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

_real_current_chat_day = dc._current_chat_day


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


class _FakeResident:
    generation = 1
    tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY

    def __init__(self):
        self._alive = False
        self._cold = True
        self.last_state_snapshot = {'mood': 'calm'}
        self.sent: list[str] = []
        self.killed = 0
        self._spawn_args: list[tuple] = []

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
        legacy = build_cc_static_parts()
        self.assertIn('[[SAVE', legacy['save_instr'])
        self.assertIn('你拥有真实的工具', legacy['stable_note'])


class DailyStaticProfileTests(unittest.TestCase):
    def test_daily_static_excludes_tools_and_save(self):
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
                    dr.prepare_daily_turn(user_message_id=uid, db_path=db, now=_FIXED_NOW, static_system='S')
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
            uid = _insert(db, 'hayana', 'hb', '2026-07-27 10:00:00')
            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
                 mock.patch.object(dr, 'LEASE_HEARTBEAT_INTERVAL', 0.02):
                plan = dr.prepare_daily_turn(
                    user_message_id=uid, db_path=db, static_system='S',
                )
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

    def _prepare(self, db, uid, *, resident=None, now=None):
        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('STATE', 'snapshot', {'k': 'v'})):
            return dr.prepare_daily_turn(
                user_message_id=uid,
                db_path=db,
                now=now,
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
            list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system='STATIC'))
            aid = dr.persist_daily_assistant_for_plan(
                plan, content='reply', thinking='', tool_calls='', cache_info='', choices='',
            )
            out = dr.complete_daily_turn(plan, assistant_message_id=aid)
            self.assertEqual(out['cursor_after'], aid)
            self.assertIsNotNone(dc.get_message_context(aid, db_path=db))
        finally:
            os.unlink(db)

    def test_text_only_spawn_args(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            uid = _insert(db, 'hayana', 'spawn', '2026-07-27 10:00:00')
            plan = self._prepare(db, uid)
            resident = _FakeResident()
            list(dr.stream_daily_resident_turn(plan, resident=resident, env={}, static_system='STATIC'))
            self.assertEqual(resident._spawn_args[-1][1], cc_resident.TOOL_PROFILE_TEXT_ONLY)
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
                plan.is_cold = False
                plan.manifest['turn_kind'] = 'hot'
                resident = _SurpriseColdResident()
                dr.set_local_binding(_binding_for_plan(plan, cursor=0))
                events = list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC',
                ))
            self.assertTrue(any(e[0] == 'done' for e in events))
            self.assertEqual(len(resident.sent), 1)
            self.assertNotIn('新增正式对话', resident.sent[0])
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
            dr.abort_daily_turn(plan, error_code='DailyWindowToolFencePending', resident=resident)
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
                plan_a = dr.prepare_daily_turn(
                    user_message_id=uid, db_path=db,
                    lease_owner='worker-a', resident=resident, static_system='S',
                )
                self.assertFalse(plan_a.is_cold is False and plan_a.manifest.get('turn_kind') == 'hot')
                dr._release_lease(plan_a)
                uid2 = _insert(db, 'hayana', 'b', '2026-07-27 10:01:00')
                with mock.patch.object(dr, 'WORKER_ID', 'worker-b'):
                    plan_b = dr.prepare_daily_turn(
                        user_message_id=uid2, db_path=db,
                        lease_owner='worker-b', resident=resident, static_system='S',
                    )
                dr._release_lease(plan_b)
                refreshed = dc.get_daily_context_by_id(plan_b.context_id, db_path=db)
                owner = dc.get_resident_owner(
                    plan_b.context_id, int(refreshed['resident_generation']), db_path=db,
                )
                self.assertEqual(str(owner.get('worker_id')), 'worker-b')
                uid3 = _insert(db, 'hayana', 'c', '2026-07-27 10:02:00')
                with mock.patch.object(dr, 'WORKER_ID', 'worker-a'):
                    plan_c = dr.prepare_daily_turn(
                        user_message_id=uid3, db_path=db,
                        lease_owner='worker-a', resident=resident, static_system='S',
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
            t_renew = t0 + datetime.timedelta(seconds=1)
            token = dc.make_epoch_token(
                chat_id='ttl', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
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
                def send_turn(self, content, commit_meta=None):
                    time.sleep(0.08)
                    yield from super().send_turn(content, commit_meta=commit_meta)

            with mock.patch.object(config_store, 'get_bool', return_value=True), \
                 mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
                 mock.patch('chat.daily_context.renew_resident_turn_lease', side_effect=dc.ConflictError('stale')):
                plan = dr.prepare_daily_turn(
                    user_message_id=uid, db_path=db, static_system='S',
                )
                resident = _SlowResident()
                with mock.patch.object(dr, 'LEASE_HEARTBEAT_INTERVAL', 0.01):
                    with self.assertRaises(dr.LeaseConflictError):
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
                chat_id='mid', context_epoch=int(ctx['context_epoch']), resident_generation=1,
            )
            dc.acquire_resident_turn_lease(
                int(ctx['id']), 1, lease_owner='night-owner', request_message_id=uid,
                db_path=db, now=start,
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


if __name__ == '__main__':
    unittest.main()
