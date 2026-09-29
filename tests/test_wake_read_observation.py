"""Wake Read Observation: derived read-only caps, hot-only, no new fences."""
from __future__ import annotations

import ast
import json
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

from chat.authoritative_planner import (
    BasicWakePlannerInput,
    run_authoritative_cc_planner,
)
from chat.wake_read_observation import (
    collect_wake_read_observation,
    empty_observation_bundle,
    observation_bundle_payload,
    observation_turn_lease,
    wake_observation_capabilities,
)
from tools.capability_manifest import (
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
    ordinary_auto_capabilities,
)
from tools.capability_state import RUNTIME_STATE_INHERIT
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import (
    TURN_LEASE_FIELDS,
    default_allowed_capabilities,
)


ROOT = Path(__file__).resolve().parents[1]

CURRENT_OBSERVATION_READS = (
    'memory.search',
    'home.light.status',
    'todo.read',
    'countdown.read',
    'ledger.read',
    'ledger.budget.read',
    'web.search',
    'web.read',
    'health.read',
    'gallery.recall',
)

WRITE_CAPS = (
    'todo.write',
    'diary.write',
    'gallery.save',
    'memory.write',
    'ledger.write',
    'gallery.screenshot',
    'task.timer.start',
)

TOOL_BY_CAPABILITY = {
    'web.search': ('WebSearch', {}),
    'web.read': ('WebFetch', {'url': 'https://example.com'}),
    'health.read': ('mcp__internal__get.health', {'metric': 'steps'}),
    'gallery.recall': ('mcp__capability__gallery_recall', {'keyword': '海边'}),
    'memory.search': ('mcp__home__search_memories', {'keyword': '昨天'}),
    'todo.write': ('mcp__home__add_todo', {'content': '寄快递'}),
    'diary.write': ('mcp__capability__diary_write', {'content': '今天'}),
    'gallery.save': ('mcp__capability__gallery_save', {'image_index': 0}),
}


class _FakeResident:
    def __init__(self, events=None, on_send=None, error=None):
        self.events = list(events or [])
        self.sent = []
        self.ensure_calls = 0
        self._model_identity = 'model:frozen'
        self.session_id = 'sid-obs'
        self._system_text = 'bound-system'
        self.generation = 4
        self.tool_profile = 'uh_a0'
        self.on_send = on_send
        self.error = error

    def _alive(self):
        return True

    def ensure_alive(self, *args, **kwargs):
        self.ensure_calls += 1
        raise AssertionError('observation must never ensure/respawn')

    def send_turn(self, content, **kwargs):
        self.sent.append((content, kwargs))
        if self.on_send is not None:
            self.on_send()
        if self.error is not None:
            raise self.error
        yield from self.events


class _FakeFence:
    def __init__(self):
        self.finished = []
        self.released = 0
        self.token = object()

    def finish(self, delivered, **kwargs):
        self.finished.append((bool(delivered), dict(kwargs)))
        self.release()

    def release(self):
        self.released += 1


def _synthetic_entry(capability_id, *, autonomy_mode, side_effect):
    return {
        'capability_id': capability_id,
        'display_name': capability_id,
        'kind': 'read' if autonomy_mode == 'read_auto' else 'write',
        'side_effect': side_effect,
        'autonomy_mode': autonomy_mode,
        'trigger': 'synthetic',
        'purpose': 'synthetic',
        'deny_when': 'synthetic',
        'failure_behavior': 'synthetic',
        'loading_policy': 'always_load',
        'provider_bindings': {
            'claude_code': 'mcp__capability__' + capability_id.replace('.', '_'),
        },
    }


class WakeObservationCapabilityTests(unittest.TestCase):
    def test_observation_caps_are_derived_not_hand_listed(self):
        src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        self.assertNotIn('WAKE_OBSERVATION_TOOLS', src)
        self.assertNotIn('WAKE_OBSERVATION_CAPABILITIES =', src)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {
                        'WAKE_OBSERVATION_TOOLS',
                        'WAKE_OBSERVATION_CAPABILITIES',
                    }:
                        self.fail('hand observation tool table is forbidden')

        derived = wake_observation_capabilities()
        defaults = default_allowed_capabilities('wake')
        self.assertEqual(derived, tuple(
            cid for cid in defaults
            if (get_capability(cid) or {}).get('autonomy_mode') == 'read_auto'
            and (get_capability(cid) or {}).get('side_effect') == 'none'
        ))
        self.assertEqual(defaults, default_allowed_capabilities('chat'))
        for cid in CURRENT_OBSERVATION_READS:
            self.assertIn(cid, derived)
        for cid in WRITE_CAPS:
            self.assertIn(cid, defaults)
            self.assertNotIn(cid, derived)
        self.assertNotIn('files.read', derived)
        self.assertNotIn('files.find', derived)
        self.assertNotIn('code.search', derived)
        self.assertNotIn('home.light.control', derived)
        self.assertNotIn('github.read', derived)
        self.assertNotIn('code.write', derived)

    def test_autonomy_filters_match_manifest_modes(self):
        derived = set(wake_observation_capabilities())
        self.assertTrue(derived)
        for cid in derived:
            entry = get_capability(cid)
            self.assertEqual(entry['autonomy_mode'], 'read_auto')
            self.assertEqual(entry['side_effect'], 'none')
        self.assertNotIn('files.read', derived)  # task_only
        self.assertNotIn('home.light.control', derived)  # never_auto
        self.assertTrue(set(P1_RESERVED_CAPABILITY_IDS).isdisjoint(derived))

    def _eval(self, capability_id, lease):
        tool_name, payload = TOOL_BY_CAPABILITY[capability_id]
        with mock.patch(
            'tools.execution_fence.read_capability_state',
            return_value=RUNTIME_STATE_INHERIT,
        ):
            return evaluate_tool_call(tool_name, payload, lease)

    def test_observation_lease_allows_reads_and_denies_writes(self):
        lease = observation_turn_lease(
            wake_run_id='obs-lease',
            issued_at='2026-09-29T00:00:00Z',
        )
        self.assertEqual(tuple(lease.keys()), TURN_LEASE_FIELDS)
        self.assertEqual(lease['issued_from'], 'default_policy')
        self.assertEqual(lease['turn_mode'], 'wake')
        self.assertEqual(lease['allowed_capabilities'], wake_observation_capabilities())
        self.assertTrue(
            set(lease['allowed_capabilities']).issubset(
                default_allowed_capabilities('wake')
            )
        )
        for cid in (
            'web.search', 'web.read', 'health.read',
            'gallery.recall', 'memory.search',
        ):
            result = self._eval(cid, lease)
            self.assertEqual(result['lease_decision'], 'ALLOW', cid)
            self.assertEqual(result['capability_id'], cid)
        for cid in ('todo.write', 'diary.write', 'gallery.save'):
            result = self._eval(cid, lease)
            self.assertEqual(result['lease_decision'], 'DENIED_CAPABILITY', cid)

    def _with_synthetic(self, entry):
        from tools import capability_manifest as cm

        cid = entry['capability_id']
        return mock.patch.multiple(
            cm,
            CAPABILITY_MANIFEST=cm.CAPABILITY_MANIFEST + (entry,),
            P1_ENABLED_CAPABILITY_IDS=frozenset(cm.P1_ENABLED_CAPABILITY_IDS | {cid}),
            _CAPABILITIES_BY_ID={**cm._CAPABILITIES_BY_ID, cid: entry},
        )

    def test_future_read_auto_inherits_into_chat_wake_and_observation(self):
        entry = _synthetic_entry(
            'calendar.read',
            autonomy_mode='read_auto',
            side_effect='none',
        )
        with self._with_synthetic(entry):
            self.assertIn('calendar.read', ordinary_auto_capabilities())
            self.assertIn('calendar.read', default_allowed_capabilities('chat'))
            self.assertIn('calendar.read', default_allowed_capabilities('wake'))
            self.assertIn('calendar.read', wake_observation_capabilities())

    def test_future_self_write_auto_enters_default_not_observation(self):
        entry = _synthetic_entry(
            'future.write',
            autonomy_mode='self_write_auto',
            side_effect='external_state',
        )
        with self._with_synthetic(entry):
            self.assertIn('future.write', ordinary_auto_capabilities())
            self.assertIn('future.write', default_allowed_capabilities('chat'))
            self.assertIn('future.write', default_allowed_capabilities('wake'))
            self.assertNotIn('future.write', wake_observation_capabilities())


class WakeObservationHotPathTests(unittest.TestCase):
    def _watermark(self):
        return types.SimpleNamespace(
            context_id=1,
            context_epoch=1,
            resident_generation=2,
            resident_key='chat:1:1:2',
            claude_session_id='sid-obs',
            transcript_path='/tmp/sid-obs.jsonl',
            expected_offset=40,
            process_generation=4,
        )

    def _gateway(self, resident):
        released = []
        return types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/obs.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_release=lambda *_a, **_k: released.append(True),
            released=released,
        ), released

    def _collect(self, resident, **extra):
        gateway, released = extra.pop('gateway', self._gateway(resident))
        if isinstance(gateway, tuple):
            gateway, released = gateway
        fence = _FakeFence()
        commit_calls = []

        def _commit(*args, **kwargs):
            commit_calls.append({'args': args, 'kwargs': kwargs})
            return {
                'skipped': True,
                'start_offset': 40,
                'end_offset': 80,
            }

        with mock.patch(
            'chat.behavior_authority_b3._hot_chat_resident_ready',
            return_value=(True, 'ok'),
        ), mock.patch(
            'chat.unified_heartbeat_a1.prepare_shared_transcript_watermark',
            return_value=(self._watermark(), 'ok'),
        ), mock.patch(
            'chat.unified_heartbeat_a1.commit_shared_transcript_watermark',
            side_effect=_commit,
        ), mock.patch(
            'chat.unified_heartbeat_a1.begin_shared_wake_delivery_fence',
            return_value=fence,
        ):
            result = collect_wake_read_observation(
                wake_run_id='obs-hot',
                resident=resident,
                db_path='/tmp/obs.db',
                gateway_module=gateway,
            )
        return result, fence, commit_calls, released

    def test_hot_resident_observation_path_pass(self):
        resident = _FakeResident(events=[
            ('tool_use', {
                'id': 'tu-1',
                'name': 'mcp__home__search_memories',
                'args': {'keyword': '昨天'},
            }),
            ('tool_result', {
                'tool_use_id': 'tu-1',
                'result': '昨天一起吃饭',
                'is_error': False,
            }),
            ('done', (
                json.dumps({
                    'observations': [
                        {'source': 'memory.search', 'fact': '昨天一起吃饭'},
                    ],
                }, ensure_ascii=False),
                '',
                {'jsonl_usage': {'stream_totals_match': True}, 'respawn_reason': ''},
                {},
            )),
        ])
        result, fence, commit_calls, released = self._collect(resident)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['started'])
        self.assertEqual(resident.ensure_calls, 0)
        self.assertEqual(len(resident.sent), 1)
        sent_text, sent_kwargs = resident.sent[0]
        self.assertIn('Wake Read Observation', sent_text)
        lease = sent_kwargs['turn_lease']
        self.assertEqual(lease['allowed_capabilities'], wake_observation_capabilities())
        self.assertNotIn('todo.write', lease['allowed_capabilities'])
        self.assertEqual(
            list(result['bundle']['observations']),
            [{'source': 'memory.search', 'fact': '昨天一起吃饭'}],
        )
        self.assertEqual(result['watermark_skip']['skipped'], True)
        self.assertEqual(len(commit_calls), 1)
        self.assertEqual(fence.finished, [])
        self.assertEqual(fence.released, 1)
        self.assertEqual(released, [])

    def test_cold_unavailable_does_not_bootstrap(self):
        resident = _FakeResident()
        gateway, released = self._gateway(resident)
        with mock.patch(
            'chat.behavior_authority_b3._hot_chat_resident_ready',
            return_value=(False, 'resident_not_hot'),
        ) as ready:
            result = collect_wake_read_observation(
                wake_run_id='obs-cold',
                resident=resident,
                db_path='/tmp/obs.db',
                gateway_module=gateway,
            )
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['started'])
        self.assertEqual(result['bundle']['status'], 'unavailable')
        self.assertEqual(resident.sent, [])
        self.assertEqual(resident.ensure_calls, 0)
        self.assertEqual(released, [])
        self.assertEqual(ready.call_count, 1)

    def test_pre_turn_unavailable_lets_planner_continue(self):
        bundle = empty_observation_bundle(status='unavailable')
        payload = observation_bundle_payload(bundle)
        captured = []

        def invoke(*, user_payload, timeout_sec):
            captured.append(json.loads(user_payload))
            return {'text': json.dumps({
                'intent': 'quiet',
                'action_candidate': 'none',
                'confidence': 0.4,
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'obs-cont',
            })}

        import datetime
        planner_input = BasicWakePlannerInput(
            wake_run_id='obs-cont',
            mode='normal',
            observed_at=datetime.datetime(2026, 9, 29, 8, 0),
            user_idle_hours=2.0,
            effective_idle_hours=2.0,
            extra_context={'ObservationBundle': payload},
        )
        status, decision = run_authoritative_cc_planner(
            planner_input=planner_input,
            wake_run_id='obs-cont',
            decision_attempt_id='da-1',
            invoke_fn=invoke,
        )
        self.assertEqual(status, 'valid')
        self.assertEqual(decision['action_candidate'], 'none')
        self.assertEqual(
            captured[0]['extra_context']['ObservationBundle']['status'],
            'unavailable',
        )

    def test_started_observation_failure_does_not_fake_success(self):
        resident = _FakeResident(error=RuntimeError('stream boom'))
        result, fence, commit_calls, released = self._collect(resident)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['started'])
        self.assertEqual(result['bundle']['status'], 'failed')
        self.assertIsNone(result['watermark_skip'])
        self.assertEqual(commit_calls, [])
        self.assertEqual(fence.finished, [])
        self.assertEqual(fence.released, 1)
        self.assertEqual(released, [])

    def test_shared_transcript_watermark_exact_skip(self):
        src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        self.assertIn('prepare_shared_transcript_watermark', src)
        self.assertIn('commit_shared_transcript_watermark', src)
        self.assertNotIn('cas_advance_scan_offset', src)
        resident = _FakeResident(events=[
            ('done', (
                '{"observations":[]}',
                '',
                {'jsonl_usage': {'stream_totals_match': True}},
                {},
            )),
        ])
        result, _fence, commit_calls, _released = self._collect(resident)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(
            commit_calls[0]['kwargs']['jsonl_finality']['stream_totals_match'],
            True,
        )

    def test_chat_mapping_not_written_by_observation(self):
        src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        for forbidden in (
            'chat_messages',
            'daily_message_contexts',
            'insert_message',
            'save_message',
            'user_turn=True',
        ):
            self.assertNotIn(forbidden, src)

    def _binding(self, *, generation=2):
        return types.SimpleNamespace(
            context_id=1,
            context_epoch=1,
            resident_generation=generation,
            resident_key='chat:1:1:%s' % generation,
        )

    def _real_fence_gateway(self, resident):
        import threading

        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/obs.db',
            _gen_busy=True,
            _gen_pending_delivery=None,
            _gen_cond=threading.Condition(),
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
        )

        def mark(token):
            fake_gateway._gen_pending_delivery = token

        def release(result, *, expected_pending_token=None):
            if expected_pending_token is not None and (
                fake_gateway._gen_pending_delivery is not expected_pending_token
            ):
                return False
            fake_gateway._gen_pending_delivery = None
            released.append(result)
            return True

        fake_gateway._gen_mark_pending_delivery = mark
        fake_gateway._gen_release = release
        return fake_gateway, released

    def _collect_with_real_fence(
        self,
        resident,
        *,
        binding,
        commit_error=None,
        watermark=None,
    ):
        import sys

        from chat import unified_heartbeat_a1 as uh

        watermark = watermark or self._watermark()
        gateway, released = self._real_fence_gateway(resident)
        close_calls = []
        state = {'binding': binding}

        def close(_resident, *, expected_key):
            close_calls.append(expected_key)
            state['binding'] = None
            return True

        commit_kwargs = {}
        if commit_error is None:
            commit_kwargs['return_value'] = {
                'skipped': True,
                'start_offset': 40,
                'end_offset': 80,
            }
        else:
            commit_kwargs['side_effect'] = commit_error

        with mock.patch(
            'chat.behavior_authority_b3._hot_chat_resident_ready',
            return_value=(True, 'ok'),
        ), mock.patch(
            'chat.unified_heartbeat_a1.prepare_shared_transcript_watermark',
            return_value=(watermark, 'ok'),
        ), mock.patch(
            'chat.unified_heartbeat_a1.commit_shared_transcript_watermark',
            **commit_kwargs,
        ), mock.patch.object(
            uh.dr, 'get_local_binding', side_effect=lambda: state['binding'],
        ), mock.patch.object(
            uh.dr, 'close_local_resident_if_bound', side_effect=close,
        ), mock.patch.dict(sys.modules, {'gateway': gateway}):
            result = collect_wake_read_observation(
                wake_run_id='obs-retire',
                resident=resident,
                db_path='/tmp/obs.db',
                gateway_module=gateway,
            )
        return result, gateway, released, close_calls, state

    def test_started_observation_failure_retires_matching_resident(self):
        resident = _FakeResident(error=RuntimeError('stream boom'))
        result, gateway, released, close_calls, state = self._collect_with_real_fence(
            resident,
            binding=self._binding(),
        )
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['started'])
        self.assertEqual(close_calls, ['chat:1:1:2'])
        self.assertIsNone(state['binding'])
        self.assertEqual(released, [None])
        self.assertIsNone(gateway._gen_pending_delivery)
        obs_src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(
            encoding='utf-8',
        )
        self.assertNotIn('b3_authority', obs_src)
        self.assertIn('retire_shared_resident_after_failed_internal_turn', obs_src)
        self.assertIn('wake_read_observation_failed', obs_src)
        self.assertIn('watermark.context_id', obs_src)
        self.assertIn('watermark.context_epoch', obs_src)
        self.assertIn('watermark.resident_generation', obs_src)
        self.assertIn('delivery_fence.release()', obs_src)

    def test_identity_mismatch_does_not_retire_newer_resident(self):
        resident = _FakeResident(error=RuntimeError('jsonl boom'))
        result, gateway, released, close_calls, state = self._collect_with_real_fence(
            resident,
            binding=self._binding(generation=9),
        )
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['started'])
        self.assertEqual(close_calls, [])
        self.assertEqual(state['binding'].resident_generation, 9)
        self.assertEqual(released, [None])
        self.assertIsNone(gateway._gen_pending_delivery)

    def test_pre_turn_failure_does_not_retire(self):
        import sys

        from chat import unified_heartbeat_a1 as uh

        resident = _FakeResident()
        gateway, released = self._real_fence_gateway(resident)
        with mock.patch(
            'chat.behavior_authority_b3._hot_chat_resident_ready',
            return_value=(False, 'resident_not_hot'),
        ), mock.patch.object(
            uh.dr, 'close_local_resident_if_bound',
        ) as close, mock.patch.dict(sys.modules, {'gateway': gateway}):
            result = collect_wake_read_observation(
                wake_run_id='obs-pre',
                resident=resident,
                db_path='/tmp/obs.db',
                gateway_module=gateway,
            )
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['started'])
        close.assert_not_called()
        self.assertEqual(released, [])
        self.assertIsNone(gateway._gen_pending_delivery)

    def test_successful_observation_does_not_retire(self):
        resident = _FakeResident(events=[
            ('done', (
                '{"observations":[]}',
                '',
                {'jsonl_usage': {'stream_totals_match': True}},
                {},
            )),
        ])
        result, gateway, released, close_calls, state = self._collect_with_real_fence(
            resident,
            binding=self._binding(),
        )
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['started'])
        self.assertEqual(close_calls, [])
        self.assertEqual(state['binding'].resident_key, 'chat:1:1:2')
        self.assertEqual(released, [None])
        self.assertIsNone(gateway._gen_pending_delivery)


class WakeObservationPlannerAndContractTests(unittest.TestCase):
    def test_planner_receives_observation_bundle_and_keeps_none_message(self):
        import datetime
        bundle = {
            'observations': [{'source': 'web.search', 'fact': '明天有雨'}],
            'status': 'ok',
        }
        captured = []

        def invoke(*, user_payload, timeout_sec):
            captured.append(json.loads(user_payload))
            return {'text': json.dumps({
                'intent': 'say_weather',
                'action_candidate': 'message',
                'confidence': 0.8,
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'obs-plan',
            })}

        planner_input = BasicWakePlannerInput(
            wake_run_id='obs-plan',
            mode='normal',
            observed_at=datetime.datetime(2026, 9, 29, 8, 0),
            user_idle_hours=3.0,
            effective_idle_hours=3.0,
        )
        status, decision = run_authoritative_cc_planner(
            planner_input=planner_input,
            wake_run_id='obs-plan',
            decision_attempt_id='da-2',
            invoke_fn=invoke,
            observation_bundle=bundle,
        )
        self.assertEqual(status, 'valid')
        self.assertEqual(decision['action_candidate'], 'message')
        extra = captured[0]['extra_context']['ObservationBundle']
        self.assertEqual(extra['status'], 'ok')
        self.assertEqual(extra['observations'][0]['source'], 'web.search')
        self.assertEqual(captured[0]['allowed_actions'], ['none', 'message'])

        shadow_src = (ROOT / 'chat' / 'planner_shadow.py').read_text(encoding='utf-8')
        self.assertIn("'ObservationBundle': bundle", shadow_src)
        planner_src = (ROOT / 'chat' / 'authoritative_planner.py').read_text(encoding='utf-8')
        self.assertIn("'ObservationBundle'", planner_src)
        self.assertIn("action not in ('none', 'message')", planner_src)

    def test_b3_renderer_tool_use_rejection_unchanged(self):
        from chat import behavior_authority_b3 as b3

        class Resident(_FakeResident):
            pass

        resident = Resident(events=[
            ('tool_use', {'name': 'WebSearch'}),
            ('tool_result', {'ok': True}),
            ('done', ('{"rendered_content":"我在。"}', '', {}, {})),
        ])
        renderer_input = b3.RendererInput(
            selected_intent='想主动告诉她自己还在这里',
            selected_action='message',
            content_target='wake_message',
            persona_context='persona',
            continuity_facts='facts',
            decision_identity={
                'wake_run_id': 'wake-1',
                'decision_attempt_id': 'attempt-1',
            },
        )
        with self.assertRaisesRegex(RuntimeError, 'uh_a1_shared_renderer_tool_use'):
            b3.invoke_renderer_cc_hot(
                renderer_input=renderer_input,
                resident=resident,
                turn_lease={'turn_id': 'render'},
            )
        src = (ROOT / 'chat' / 'behavior_authority_b3.py').read_text(encoding='utf-8')
        self.assertIn("raise RuntimeError('uh_a1_shared_renderer_tool_use')", src)
        renderer = src[
            src.index('def invoke_renderer_cc_hot'):
            src.index('def _try_invoke_shared_renderer')
        ]
        self.assertIn('if saw_tool:', renderer)

    def test_disabled_wake_modes_dream_summarize_and_single_resident(self):
        from wake.runners import (
            DISABLED_CC_WAKE_MODES,
            WAKE_MODE_DISABLED_REASON,
            wake_mode_disabled,
        )

        self.assertEqual(
            set(DISABLED_CC_WAKE_MODES),
            {'morning', 'nightwatch', 'ritual', 'self_trigger'},
        )
        for mode in DISABLED_CC_WAKE_MODES:
            self.assertTrue(wake_mode_disabled(mode))
        gateway = (ROOT / 'gateway.py').read_text(encoding='utf-8')
        obs_src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        self.assertIn("getattr(gateway_module, '_CC_RESIDENT', None)", obs_src)
        self.assertNotIn('ensure_alive', obs_src)
        self.assertEqual(gateway.count('_CC_RESIDENT = _SwappableResident('), 1)
        self.assertNotIn('WAKE_OBSERVATION_RESIDENT', obs_src)
        self.assertEqual(obs_src.count('_CC_RESIDENT'), 1)

        wake_decide = gateway[gateway.index('def _wake_decide_locked'):]
        basic_slice = wake_decide[
            wake_decide.index('if basic_normal:'):
            wake_decide.index('    planner_view = None')
        ]
        self.assertIn('collect_wake_read_observation', basic_slice)
        self.assertNotIn('ensure_alive', basic_slice)

        from wake.runners import select_wake_provider
        import config_store
        with mock.patch.object(config_store, 'get', side_effect=lambda key, default=None: {
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        }.get(key, default)):
            from wake.runners import UnsupportedWakeModeError
            with self.assertRaisesRegex(UnsupportedWakeModeError, 'surface-owned Background Generation Adapter'):
                select_wake_provider('dream')
            self.assertEqual(select_wake_provider('summarize'), 'api_relay')
            for mode in DISABLED_CC_WAKE_MODES:
                with self.assertRaisesRegex(UnsupportedWakeModeError, WAKE_MODE_DISABLED_REASON):
                    select_wake_provider(mode)

    def test_no_new_workflow_and_no_second_skip_system(self):
        diff = subprocess.check_output(
            ['git', 'diff', '--name-only', 'origin/main'],
            cwd=str(ROOT),
            text=True,
        )
        files = [line for line in diff.splitlines() if line.strip()]
        self.assertFalse(any(path.startswith('.github/workflows/') for path in files))
        obs_src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        self.assertIn('SharedTranscriptWatermark', Path(
            ROOT, 'chat', 'unified_heartbeat_a1.py'
        ).read_text(encoding='utf-8'))
        self.assertIn('commit_shared_transcript_watermark', obs_src)
        self.assertNotIn('observation_scan_offset', obs_src)

    def test_external_mcp_surface_untouched(self):
        diff = subprocess.check_output(
            ['git', 'diff', '--name-only', 'origin/main'],
            cwd=str(ROOT),
            text=True,
        )
        self.assertNotIn('tools/external_mcp_surface.py', diff.splitlines())
        obs_src = (ROOT / 'chat' / 'wake_read_observation.py').read_text(encoding='utf-8')
        self.assertNotIn('mcp__external__', obs_src)


if __name__ == '__main__':
    unittest.main()
