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
                chat_id='mid', context_epoch=int(ctx['context_epoch']), resident_generation=1,
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
                with self.assertRaises(DeferredError):
                    _prepare_turn(
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
            self.assertEqual(resident.killed, 0)
            refreshed = dc.get_daily_context_by_id(int(old_ctx['id']), db_path=db)
            self.assertEqual(int(refreshed['resident_generation']), gen_before)
            self.assertTrue(dc.is_resident_turn_active(
                int(old_ctx['id']), gen_before, db_path=db, now=start,
            ))
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
            new_ctx = dc.get_context_for_local_day('expired', '2026-07-27', db_path=db)
            self.assertIsNotNone(new_ctx)
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
                with self.assertRaises(dr.DailyRuntimeError) as ctx:
                    dr.prepare_daily_turn(
                        user_message_id=uid,
                        chat_id='origin',
                        db_path=db,
                        now=start,
                        wall_now=finish,
                        static_system='S',
                    )
                self.assertEqual(ctx.exception.error_code, 'stale_origin_day')
            self.assertIsNone(
                dc.get_context_for_local_day('origin', '2026-07-26', db_path=db),
            )
            latest = dc.get_latest_active_context('origin', db_path=db)
            self.assertEqual(int(latest['id']), int(new_ctx['id']))
            self.assertEqual(str(latest['local_day']), '2026-07-27')
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
                with self.assertRaises(dr.DailyRuntimeError) as ctx:
                    _prepare_turn(
                        db, uid,
                        chat_id='exist',
                        now=start,
                        wall_now=finish,
                        resident=resident,
                    )
                self.assertEqual(ctx.exception.error_code, 'stale_origin_day')
                retire_mock.assert_not_called()
            self.assertEqual(resident.killed, 0)
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
                old_ctx = dc.get_context_for_local_day('default', '2026-07-26', db_path=db)
                self.assertIsNotNone(old_ctx)
                self.assertEqual(int(old_ctx['is_backfill']), 0)
                resident = _FakeResident()
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='S',
                ))
                self.assertEqual(len(resident.sent), 1)
                aid = dr.persist_daily_assistant_for_plan(
                    plan, content='cross reply', thinking='', tool_calls='', cache_info='', choices='',
                )
                mapping = dc.get_message_context(aid, db_path=db)
                self.assertEqual(int(mapping['context_id']), int(old_ctx['id']))
                dr._release_lease(plan)
                uid_new = _insert(db, 'hayana', 'new day', '2026-07-27 10:00:00')
                new_day = datetime.datetime(2026, 7, 27, 10, 0, 0)
                plan_new = _prepare_turn(db, uid_new, wall_now=new_day)
                new_ctx = dc.get_context_for_local_day('default', '2026-07-27', db_path=db)
                self.assertIsNotNone(new_ctx)
                self.assertEqual(int(new_ctx['is_backfill']), 0)
                self.assertGreater(int(new_ctx['context_epoch']), int(old_ctx['context_epoch']))
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
            old_ctx = dc.get_context_for_local_day('default', '2026-07-26', db_path=db)
            self.assertIsNotNone(old_ctx)
            self.assertEqual(int(old_ctx['is_backfill']), 0)
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
                chat_id='renew', context_epoch=int(ctx['context_epoch']), resident_generation=gen,
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
                def send_turn(self, content, commit_meta=None):
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
    def send_turn(self, content, commit_meta=None):
        self.sent.append(str(content))
        yield ('text', 'partial')
        deadline = time.time() + 30
        while time.time() < deadline and self._alive:
            time.sleep(0.01)
        yield ('done', ('never', '', {}, {}))


class _BlockBeforeYieldResident(_FakeResident):
    def send_turn(self, content, commit_meta=None):
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
            self.assertEqual(int(assistant_count), 0)
            self.assertEqual(int(mapping_count), 0)
            self.assertFalse(dc.is_resident_turn_active(
                int(ctx['id']), 1, db_path=db, now=_FIXED_NOW,
            ))
            self.assertGreaterEqual(resident.killed, 1)
            self.assertGreater(int(ctx['resident_generation']), 1)
            self.assertEqual(gen_release_calls, [None])
            self.assertTrue(release_turn_calls)
            self.assertFalse(any(c.get('persisted') for c in release_turn_calls))
            memo_mock.assert_not_called()
            moments_mock.assert_not_called()
            scoring_mock.assert_not_called()
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
        finally:
            os.unlink(db)


class GatewayDailyCasSseTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
