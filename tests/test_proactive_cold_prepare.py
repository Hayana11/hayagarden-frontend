"""2a: proactive cold start through the Chat runtime (fake resident, temp DB)."""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import sqlite3
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident
import config_store
from chat import daily_context as dc
from chat import daily_runtime as dr
from tests import test_daily_runtime as tdr  # module import: keep its tests uncollected

_FakeResident, _insert = tdr._FakeResident, tdr._insert

NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)
TRIGGER = '【系统主动轮】trigger-text'


class _ColdResident(_FakeResident):
    """Cold fake that reports the flush hooks like the real resident."""
    cwd = ''
    session_id = None
    fail_at = None   # 'spawn' | 'before_flush' | 'after_flush'

    def __init__(self):
        super().__init__()
        self.envs = []

    def ensure_alive(self, system_text, env, tool_profile=cc_resident.TOOL_PROFILE_LEGACY):
        self.envs.append(dict(env))
        if self.fail_at == 'spawn':
            raise RuntimeError('spawn exploded')
        return super().ensure_alive(system_text, env, tool_profile=tool_profile)

    def send_turn(self, content, commit_meta=None, turn_lease=None, **kw):
        self.sent.append(str(content))
        if self.fail_at == 'before_flush':
            raise RuntimeError('stdin never reached the model')
        kw['on_stdin_begin']()
        kw['on_stdin_flushed']()
        if self.fail_at == 'after_flush':
            raise RuntimeError('provider died after flush')
        yield ('text', 'hello there')
        yield ('done', ('hello there', '', {'input_tokens': 3}, {}))


class _Base(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        self.db = tdr.ContextPlanConsumerTests._context_plan_runtime_db()
        _insert(self.db, 'hayana', 'previous user', '2026-07-27 09:00:00')
        _insert(self.db, 'assistant', 'previous assistant', '2026-07-27 09:01:00')
        _insert(self.db, 'hayana', 'latest real message', '2026-07-27 09:58:00')
        self.latest = _insert(self.db, 'assistant', 'latest real reply', '2026-07-27 09:59:00')
        original = config_store.get
        self.cfg = {'CONTEXT_PLAN_CONSUMER_ENABLED': '1', 'CONTEXT_PLAN_TOKEN_BUDGET': '1000',
                    'CONTEXT_PLAN_RESERVE_BUDGET': '0', 'CONTEXT_PLAN_RECENT_RAW_TARGET': '100'}
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.object(
            config_store, 'get',
            side_effect=lambda k, d=None: self.cfg.get(k, original(k, d))))
        self.stack.enter_context(mock.patch.object(config_store, 'get_bool', return_value=True))
        self.stack.enter_context(mock.patch(
            'chat.daily_history._build_state_text', return_value=('', 'none', {})))
        self.addCleanup(self.stack.close)
        self.addCleanup(os.unlink, self.db)
        self.total = self.counts()[0]

    def counts(self):
        conn = sqlite3.connect(self.db)
        try:
            return (
                conn.execute('SELECT COUNT(*) FROM chat_messages').fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM chat_messages WHERE author='hayana'").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM daily_message_contexts WHERE role='user'").fetchone()[0],
            )
        finally:
            conn.close()

    def prepare(self, resident, rid='rid-1', **kw):
        return dr.prepare_proactive_cold_turn(
            wake_run_id=rid, trigger_text=TRIGGER, now=NOW, wall_now=NOW,
            db_path=self.db, resident=resident, static_system='STATIC', **kw)


class IdentityTests(unittest.TestCase):
    def _plan(self, **kw):
        base = dict(user_message_id=None, turn_mode='chat', wake_run_id=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_dispatch_follows_request_identity(self):
        self.assertEqual(dr._request_identity(self._plan(user_message_id=7)), ('message', 'message:7'))
        self.assertEqual(
            dr._request_identity(self._plan(turn_mode='wake', wake_run_id='r1')),
            ('wake', 'wake_request:r1'))

    def test_inconsistent_identity_is_rejected(self):
        for plan in (
            self._plan(turn_mode='wake'),                       # no rid
            self._plan(turn_mode='wake', wake_run_id='  '),
            self._plan(user_message_id=7, wake_run_id='r1'),    # chat disguised with a rid
            self._plan(turn_mode='other', user_message_id=7),
        ):
            with self.assertRaises(dr.DailyRuntimeError) as caught:
                dr._request_identity(plan)
            self.assertEqual(caught.exception.error_code, 'request_identity_missing')
        with self.assertRaises(dr.DailyRuntimeError) as caught:
            dr._request_identity(self._plan())                  # chat without a request id
        self.assertEqual(caught.exception.error_code, 'request_message_id_missing')


class PrepareAndContextPlanTests(_Base):
    def test_wake_current_request_is_wake_ref_and_only_assistant_rows_added(self):
        before = self.counts()
        resident = _ColdResident()
        plan = self.prepare(resident)
        sections = [s for s in plan.continuity_plan.ordered_sections if s.kind == 'current_request']
        self.assertEqual([s.source_ref for s in sections], ['wake_request:rid-1'])
        self.assertEqual(sections[0].content_hash, hashlib.sha256(TRIGGER.encode()).hexdigest())
        self.assertFalse(any(s.source_ref.startswith('message:') and s.kind == 'current_request'
                             for s in plan.continuity_plan.ordered_sections))
        self.assertEqual(plan.turn_mode, 'wake')
        self.assertIsNone(plan.user_message_id)
        self.assertEqual(plan.history_anchor_message_id, self.latest)
        self.assertEqual(plan.lease_owner, 'wake:rid-1')
        self.assertEqual(plan.manifest['memory_recall_mode'], 'skipped_wake')
        self.assertFalse(plan.hidden_flow_eligible)
        events = list(dr.stream_daily_resident_turn(
            plan, resident=resident, env={}, static_system='STATIC'))
        self.assertTrue(any(evt == 'done' for evt, _ in events))
        self.assertEqual(len(resident.sent), 1)
        self.assertEqual(resident.sent[0].count(TRIGGER), 1)
        self.assertIn('latest real reply', resident.sent[0])   # H is included
        self.assertEqual(plan.manifest['context_plan_fixed_section_parity'], 'PASS')
        self.assertEqual(self.counts(), before)                   # no user row, no new row at all

    def test_wake_request_tamper_fails_install_proof(self):
        resident = _ColdResident()
        plan = self.prepare(resident)
        plan.request_content_sha256 = 'f' * 64
        with self.assertRaises(dr.DailyRuntimeError) as caught:
            list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC'))
        self.assertEqual(caught.exception.error_code, 'context_plan_current_request_parity_failed')
        self.assertEqual(resident.sent, [])

    def test_hot_resident_is_not_started_cold(self):
        resident = _ColdResident()
        with mock.patch.object(dr, '_can_hot_turn', return_value=True):
            with self.assertRaises(dr.DailyRuntimeError) as caught:
                self.prepare(resident)
        self.assertEqual(caught.exception.error_code, 'proactive_cold_resident_became_hot')
        self.assertIsNone(dc.get_resident_history_cursor(1, 1, db_path=self.db))


class ReprepareTests(_Base):
    def _assert_same_wake(self, replacement, plan):
        self.assertEqual(replacement.turn_mode, 'wake')
        self.assertEqual(replacement.wake_run_id, 'rid-1')
        self.assertEqual(replacement.user_content, TRIGGER)
        self.assertIsNone(replacement.user_message_id)
        self.assertEqual(replacement.history_anchor_message_id, plan.history_anchor_message_id)
        self.assertEqual(replacement.lease_owner, 'wake:rid-1')
        self.assertEqual(self.counts()[1:], (2, 2))               # still no extra user context row

    def test_registered_session_change_and_hot_cold_mismatch(self):
        for fn in (dr.reprepare_after_registered_session_change, dr.reprepare_after_hot_cold_mismatch):
            resident = _ColdResident()
            plan = self.prepare(resident)
            replacement = fn(plan, resident=resident, static_system='STATIC')
            self._assert_same_wake(replacement, plan)
            self.assertTrue(replacement.is_respawn)
            dr._release_lease(replacement)

    def test_capacity_swap_reprepare(self):
        resident = _ColdResident()
        plan = self.prepare(resident)
        replacement = dr.reprepare_after_capacity_swap(
            plan, resident=resident, static_system='STATIC', cursor_watermark=self.latest)
        self._assert_same_wake(replacement, plan)
        self.assertEqual(replacement.manifest['turn_kind'], 'capacity_swap')
        dr._release_lease(replacement)


class GatewayColdRouteTests(_Base):
    MODES = ('normal', 'morning', 'nightwatch', 'self_trigger')

    def setUp(self):
        super().setUp()
        import gateway
        from chat import behavior_authority_b3 as b3
        self.gw, self.b3 = gateway, b3
        self.resident = _ColdResident()
        self.fence = mock.Mock()
        self.ready = (False, 'no_local_binding')
        self.cfg['PROACTIVE_COLD_START_MODES'] = ','.join(self.MODES)
        self.authority = types.SimpleNamespace(provider='claude_code', model_identity='m-1')
        patches = [
            mock.patch.object(gateway, '_CC_RESIDENT', self.resident),
            mock.patch.object(gateway, 'DB_PATH', self.db),
            mock.patch.object(gateway, '_gen_acquire_or_wait', return_value=('own', None)),
            mock.patch.object(gateway, '_gen_release'),
            mock.patch.object(gateway, '_wake_live_trace'),
            mock.patch.object(gateway, 'jsonify', side_effect=lambda payload: payload),
            mock.patch.object(b3, '_hot_chat_resident_ready', side_effect=lambda *a, **k: self.ready),
            mock.patch('chat.unified_heartbeat_a1.begin_shared_wake_delivery_fence', return_value=self.fence),
            mock.patch('chat.provider_router.capture_generation_authority', side_effect=lambda **k: self.authority),
            mock.patch('chat.cc_auth.build_cc_provider_env',
                       side_effect=lambda provider, model, token_getter=None: {'P': provider}),
            mock.patch('chat.system_builder.build_cc_daily_static_parts',
                       return_value={'full_system': 'STATIC', 'persona': 'P'}),
            mock.patch('chat.display_thinking.get_display_thinking_snapshot', return_value=('auto', '[[自我状态]]x[[/自我状态]]')),
            mock.patch('chat.cc_history_rewrite.guard_cc_generation', side_effect=lambda events: events),
        ]
        for patch in patches:
            self.stack.enter_context(patch)

    def run_wake(self, mode='normal', rid='rid-gw'):
        return self.gw._run_unified_normal_main_chat_turn(
            wake_run_id=rid, now=NOW, t2_hours=1.0, t_hours=1.0, mode=mode)

    def test_switch_off_or_unsafe_reason_still_skips(self):
        from chat.behavior_authority_b3 import UnifiedNormalWakeSharedUnavailable
        cases = [
            ({'PROACTIVE_COLD_START_MODES': ''}, 'no_local_binding'),
            ({'PROACTIVE_COLD_START_MODES': 'normal'}, 'context_epoch_mismatch'),
            ({'PROACTIVE_COLD_START_MODES': 'normal'}, 'process_generation_mismatch'),
            ({'PROACTIVE_COLD_START_MODES': 'normal', 'CONTEXT_PLAN_CONSUMER_ENABLED': '0'}, 'no_local_binding'),
            ({'PROACTIVE_COLD_START_MODES': 'morning'}, 'no_local_binding'),   # mode not enabled
        ]
        for cfg, reason in cases:
            self.cfg.update({'CONTEXT_PLAN_CONSUMER_ENABLED': '1'})
            self.cfg.update(cfg)
            self.ready = (False, reason)
            with self.assertRaises(UnifiedNormalWakeSharedUnavailable, msg=(cfg, reason)):
                self.run_wake('normal')
        self.assertEqual(self.resident.sent, [])
        self.assertIsNone(dr.get_local_binding())

    def test_allowed_matrix(self):
        self.cfg['PROACTIVE_COLD_START_MODES'] = 'normal,ritual'
        for mode, reason, expected in (
            ('normal', 'no_local_binding', True), ('', 'resident_not_hot', True),
            ('normal', 'resident_stale:cache', True), ('ritual', 'no_local_binding', False),
            ('morning', 'no_local_binding', False), ('normal', 'owner_mismatch', False),
        ):
            self.assertEqual(self.gw._proactive_cold_start_allowed(mode, reason), expected, (mode, reason))

    def test_four_modes_cold_round_then_pending_finalizer(self):
        before = self.counts()
        for mode in self.MODES:
            dr.reset_bindings_for_tests()
            self.resident = _ColdResident()
            self.stack.enter_context(mock.patch.object(self.gw, '_CC_RESIDENT', self.resident))
            cursor = dc.get_resident_history_cursor(1, 1, db_path=self.db)
            turn = self.run_wake(mode, rid='rid-' + mode)
            plan = turn['_proactive_cold_plan']
            self.assertTrue(turn['proactive_cold_start'])
            self.assertEqual(turn['text'], 'hello there')
            self.assertEqual(
                [s.source_ref for s in plan.continuity_plan.ordered_sections if s.kind == 'current_request'],
                ['wake_request:rid-' + mode])
            self.assertEqual(turn['cache_info']['wake_request_ref'], 'wake_request:rid-' + mode)
            self.assertEqual(turn['cache_info']['history_anchor_message_id'], self.latest)
            self.assertTrue(turn['cache_info']['proactive_cold_start'])
            self.assertIs(turn['_shared_delivery_fence'], self.fence)
            self.assertIn('start_offset', turn['_proactive_cold_transcript'])
            self.assertIsNotNone(dr.get_local_binding())          # temporary binding is live
            out = self.gw._proactive_cold_finalizer_pending(turn, 'rid-' + mode, None)
            self.assertEqual(out['reason'], 'proactive_cold_finalizer_pending')
            self.assertTrue(out['skipped'])
            self.assertIsNone(dr.get_local_binding())
            self.assertGreaterEqual(self.resident.killed, 1)
            self.assertEqual(dc.get_resident_history_cursor(1, plan.resident_generation, db_path=self.db), None)
            self.assertIsNone(plan.manifest.get('context_receipt_status'))
            self.assertIsNone(cursor)
        self.assertEqual(self.counts(), before)

    def test_env_rules_are_identical_for_relay_and_claude_p(self):
        for provider in ('api_relay', 'claude_code'):
            dr.reset_bindings_for_tests()
            self.authority = types.SimpleNamespace(provider=provider, model_identity='m-1')
            self.resident = _ColdResident()
            self.stack.enter_context(mock.patch.object(self.gw, '_CC_RESIDENT', self.resident))
            turn = self.run_wake('normal', rid='rid-env-' + provider)
            self.assertEqual(self.resident.envs, [{'P': provider}])
            dr.abort_daily_turn(turn['_proactive_cold_plan'], error_code='t', resident=self.resident)

    def test_failure_before_flush_restores_pre_start_state(self):
        for stage in ('spawn', 'before_flush'):
            dr.reset_bindings_for_tests()
            self.fence.reset_mock()
            self.resident.fail_at = stage
            with self.assertRaises(self.gw.UnifiedProactivePreModelFailed):
                self.run_wake('normal', rid='rid-pre-' + stage)
            self.assertIsNone(dr.get_local_binding())
            self.assertIsNone(dc.get_resident_history_cursor(1, 1, db_path=self.db))
            self.assertEqual(self.fence.finish.call_args.args[0], False)
            self.assertEqual(self.counts()[0], self.total)
            conn = sqlite3.connect(self.db)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_resident_turn_leases').fetchone()[0], 0)
            conn.close()

    def test_failure_after_flush_kills_resident_and_clears_binding(self):
        self.resident.fail_at = 'after_flush'
        with self.assertRaises(RuntimeError) as caught:
            self.run_wake('normal', rid='rid-post')
        self.assertNotIsInstance(caught.exception, self.gw.UnifiedProactivePreModelFailed)
        self.assertIsNone(dr.get_local_binding())
        self.assertGreaterEqual(self.resident.killed, 1)
        self.fence.finish.assert_called_once()
        self.assertEqual(self.counts()[0], self.total)


if __name__ == '__main__':
    unittest.main()
