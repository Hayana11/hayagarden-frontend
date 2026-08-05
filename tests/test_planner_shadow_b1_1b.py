"""B1-1B｜CapabilitySkillView + non-blocking Planner Shadow — minimum cases."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

import drive_engine as de
from chat.capability_skill_view import (
    freeze_capability_skill_view,
    mode_action_contract,
    resolved_action_capability_for,
)
from chat.planner_shadow import (
    classify_production_outcome,
    comparison_evidence_status,
    dispatch_planner_shadow,
    mark_production_attempt_outcome,
    new_decision_attempt_id,
    run_shadow_attempt,
    validate_shadow_decision,
)
from chat.planner_state_view import freeze_planner_state_view
from tests.test_drive_authority import _production_bootstrap

T_OBS = datetime.datetime(2026, 8, 4, 15, 0, 0)


class PlannerShadowB11BTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self.observe = str(Path(self.tmp.name) / 'planner_shadow.jsonl')
        os.environ['MEMORIES_DB'] = self.db_path
        os.environ['PLANNER_SHADOW_OBSERVE_PATH'] = self.observe
        de.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE chat_messages SET created_at='2026-08-04 12:00:00' "
            "WHERE id=(SELECT MAX(id) FROM chat_messages)"
        )
        conn.commit()
        conn.close()
        self.view = freeze_planner_state_view(
            db_path=self.db_path,
            observed_at=T_OBS,
            wake_run_id='b11b-run-1',
        )
        self.skill = freeze_capability_skill_view(
            wake_run_id='b11b-run-1',
            provider='api_relay',
            mode='normal',
            prepared_tools=[{'name': 'web_search'}, {'name': 'recall_photo'}],
            dry_run=False,
            captured_at=T_OBS,
        )
        self.attempt_id = new_decision_attempt_id()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop('PLANNER_SHADOW_OBSERVE_PATH', None)

    def _read_observations(self):
        if not os.path.exists(self.observe):
            return []
        rows = []
        with open(self.observe, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        return rows

    def test_case1_valid_shadow_decision_observation(self):
        """Case 1: V+K → valid structured Decision → observation; no V3 write."""
        before_ver = int(self.view.state_version)

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'reconnect_gently',
                'action_candidate': 'message',
                'confidence': 0.72,
                'primary_drive': 'attachment',
                'contributors': ['longing', 'bond.passion'],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'b11b-run-1',
                'state_version': before_ver,
                'source': 'planner_shadow',
                'shadow_only': True,
            })

        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=self.attempt_id,
            legacy_provenance={
                'source': 'drive_engine.decide',
                'primary_drive': 'curiosity',
                'blocked': False,
            },
            invoke_fn=fake_invoke,
        )
        self.assertEqual(rec['shadow_status'], 'valid')
        decision = rec['shadow_decision']
        self.assertEqual(decision['wake_run_id'], 'b11b-run-1')
        self.assertEqual(decision['decision_attempt_id'], self.attempt_id)
        self.assertEqual(decision['state_version'], before_ver)
        self.assertEqual(decision['source'], 'planner_shadow')
        self.assertTrue(decision['shadow_only'])
        self.assertEqual(decision['action_candidate'], 'message')
        self.assertEqual(decision['primary_drive'], 'attachment')
        rows = self._read_observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['shadow_status'], 'valid')
        self.assertEqual(rows[0]['decision_attempt_id'], self.attempt_id)
        self.assertEqual(rows[0]['comparison_status'], 'pending')
        self.assertFalse(rows[0]['authoritative'])
        # No V3 mutation from Shadow observation path.
        conn = sqlite3.connect(self.db_path)
        ver = conn.execute(
            'SELECT state_version FROM internal_state_v3 WHERE id=1'
        ).fetchone()[0]
        conn.close()
        self.assertEqual(int(ver), before_ver)

    def test_case2_valid_none_is_success(self):
        """Case 2: action_candidate=none is a legal successful Shadow Decision."""

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'rest_quietly',
                'action_candidate': 'none',
                'confidence': 0.81,
                'primary_drive': None,
                'contributors': ['fatigue'],
                'blocked': False,
                'reason_codes': ['quiet_contentment'],
                'wake_run_id': 'b11b-run-1',
                'state_version': int(self.view.state_version),
            })

        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=self.attempt_id,
            invoke_fn=fake_invoke,
        )
        self.assertEqual(rec['shadow_status'], 'valid')
        self.assertEqual(rec['shadow_decision']['action_candidate'], 'none')

    def test_case3_shadow_failure_fail_open(self):
        """Case 3: timeout/error → observation error; no raise to production."""

        def boom(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            raise TimeoutError('shadow timed out')

        # Must not raise into production path.
        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=self.attempt_id,
            invoke_fn=boom,
        )
        self.assertEqual(rec['shadow_status'], 'error')
        self.assertEqual(rec['error_category'], 'TimeoutError')
        rows = self._read_observations()
        self.assertEqual(rows[-1]['shadow_status'], 'error')

        # Dispatch is non-blocking: returns before slow worker finishes.
        def slow_attempt(**kwargs):
            del kwargs
            time.sleep(1.5)
            return {'shadow_status': 'error'}

        t0 = time.time()
        with mock.patch(
            'chat.planner_shadow.run_shadow_attempt',
            side_effect=slow_attempt,
        ):
            dispatch_planner_shadow(
                planner_view=self.view,
                skill_view=self.skill,
                wake_run_id='b11b-run-1',
                decision_attempt_id=self.attempt_id,
            )
        self.assertLess(time.time() - t0, 0.5)

    def test_c1_capability_intersection_relay_vs_cc(self):
        """C1: Relay advertises diary; CC must not under current content contract."""
        relay_caps = resolved_action_capability_for(
            provider='api_relay', mode='normal',
        )
        cc_caps = resolved_action_capability_for(
            provider='claude_code', mode='normal',
        )
        self.assertIn('diary', relay_caps)
        self.assertNotIn('diary', cc_caps)
        self.assertIn('message', relay_caps)
        self.assertIn('message', cc_caps)
        # Frozen views must reflect the same intersection truth.
        self.assertIn('diary', self.skill.resolved_action_capability)
        with mock.patch(
            'chat.cc_model.cc_model_snapshot',
            return_value=(
                'claude-opus-4-6',
                'explicit:claude-opus-4-6',
                ['--model', 'claude-opus-4-6'],
            ),
        ):
            cc_skill = freeze_capability_skill_view(
                wake_run_id='b11b-run-1',
                provider='claude_code',
                mode='normal',
                prepared_tools=[{'name': 'recall_photo'}],
                dry_run=False,
                captured_at=T_OBS,
            )
        self.assertEqual(cc_skill.model_identity, 'explicit:claude-opus-4-6')
        self.assertNotIn('diary', cc_skill.resolved_action_capability)
        self.assertFalse(cc_skill.preconditions['diary_executor_resolved'])
        self.assertEqual(
            cc_skill.mode_contract['provider_content_policy'],
            'cc_content_message_explore_only',
        )

    def test_c1_mode_intersection_nightwatch_and_ritual(self):
        """C1: mode contract excludes explore for nightwatch; ritual_type matters."""
        nw_mode, nw_id = mode_action_contract('nightwatch')
        self.assertEqual(nw_mode, ('none', 'message', 'diary'))
        self.assertEqual(nw_id, 'nightwatch_decision')

        nw_relay = resolved_action_capability_for(
            provider='api_relay', mode='nightwatch',
        )
        self.assertEqual(nw_relay, ('none', 'message', 'diary'))
        self.assertNotIn('explore', nw_relay)

        nw_cc = resolved_action_capability_for(
            provider='claude_code', mode='nightwatch',
        )
        self.assertEqual(nw_cc, ('none', 'message'))
        self.assertNotIn('diary', nw_cc)
        self.assertNotIn('explore', nw_cc)

        generic = resolved_action_capability_for(
            provider='api_relay', mode='ritual', ritual_type='',
        )
        self.assertIn('explore', generic)
        self.assertIn('diary', generic)

        solstice = resolved_action_capability_for(
            provider='api_relay', mode='ritual', ritual_type='solstice',
        )
        birthday = resolved_action_capability_for(
            provider='api_relay', mode='ritual', ritual_type='birthday',
        )
        self.assertEqual(solstice, ('message',))
        self.assertEqual(birthday, ('message',))

        # CC special ritual still message-only (provider diary cut is no-op).
        solstice_cc = resolved_action_capability_for(
            provider='claude_code', mode='ritual', ritual_type='solstice',
        )
        self.assertEqual(solstice_cc, ('message',))

        frozen = freeze_capability_skill_view(
            wake_run_id='b11b-run-1',
            provider='api_relay',
            mode='ritual',
            ritual_type='birthday',
            prepared_tools=[],
            dry_run=False,
            captured_at=T_OBS,
        )
        self.assertEqual(frozen.ritual_type, 'birthday')
        self.assertEqual(frozen.resolved_action_capability, ('message',))
        self.assertEqual(
            frozen.mode_contract['mode_contract_id'],
            'ritual_birthday_message_only',
        )

    def test_c2_attempt_pairing_accepted_vs_orphan(self):
        """C2: shared attempt ID + success ⇒ accepted; failed ⇒ orphan."""

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'reconnect_gently',
                'action_candidate': 'message',
                'confidence': 0.7,
                'primary_drive': 'attachment',
                'contributors': [],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'b11b-run-1',
                'state_version': int(self.view.state_version),
            })

        attempt_ok = new_decision_attempt_id()
        run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_ok,
            invoke_fn=fake_invoke,
        )
        # Before production outcome → pending (not accepted evidence).
        self.assertEqual(
            comparison_evidence_status(
                wake_run_id='b11b-run-1',
                decision_attempt_id=attempt_ok,
            ),
            'pending',
        )
        mark_production_attempt_outcome(
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_ok,
            status='success',
            action='message',
            provider='api_relay',
        )
        self.assertEqual(
            comparison_evidence_status(
                wake_run_id='b11b-run-1',
                decision_attempt_id=attempt_ok,
            ),
            'accepted',
        )

        attempt_fail = new_decision_attempt_id()
        run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_fail,
            invoke_fn=fake_invoke,
        )
        mark_production_attempt_outcome(
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_fail,
            status='failed',
            reason='runner_500',
            provider='api_relay',
        )
        self.assertEqual(
            comparison_evidence_status(
                wake_run_id='b11b-run-1',
                decision_attempt_id=attempt_fail,
            ),
            'orphan',
        )

    def test_c2_outcome_truth_gate_block_and_exception(self):
        """C2: delivered=False/settled=False ⇒ orphan; exception marker ⇒ orphan."""
        status, reason = classify_production_outcome({
            'delivered': True,
            'settled': True,
            'gate_reason': 'ok',
            'settle_status': 'applied',
        })
        self.assertEqual(status, 'success')
        self.assertEqual(reason, '')

        status, reason = classify_production_outcome({
            'delivered': False,
            'settled': False,
            'gate_reason': 'stale',
            'settle_status': None,
        })
        self.assertEqual(status, 'failed')
        self.assertTrue(reason.startswith('gate_blocked:'))

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return json.dumps({
                'intent': 'reconnect_gently',
                'action_candidate': 'message',
                'confidence': 0.7,
                'primary_drive': 'attachment',
                'contributors': [],
                'blocked': False,
                'reason_codes': [],
                'wake_run_id': 'b11b-run-1',
                'state_version': int(self.view.state_version),
            })

        # Soft-window gate block must never become accepted evidence.
        attempt_gate = new_decision_attempt_id()
        run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_gate,
            invoke_fn=fake_invoke,
        )
        gate_status, gate_reason = classify_production_outcome({
            'delivered': False,
            'settled': False,
            'gate_reason': 'stale',
        })
        mark_production_attempt_outcome(
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_gate,
            status=gate_status,
            action='message',
            reason=gate_reason,
            provider='api_relay',
        )
        self.assertEqual(
            comparison_evidence_status(
                wake_run_id='b11b-run-1',
                decision_attempt_id=attempt_gate,
            ),
            'orphan',
        )

        # Executor exception/rollback path writes failed marker.
        attempt_exc = new_decision_attempt_id()
        run_shadow_attempt(
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_exc,
            invoke_fn=fake_invoke,
        )
        mark_production_attempt_outcome(
            wake_run_id='b11b-run-1',
            decision_attempt_id=attempt_exc,
            status='failed',
            action='message',
            reason='wake action rejected: message requires non-empty CONTENT',
            provider='api_relay',
        )
        self.assertEqual(
            comparison_evidence_status(
                wake_run_id='b11b-run-1',
                decision_attempt_id=attempt_exc,
            ),
            'orphan',
        )

        # Gateway must classify from executor result, not unconditional success.
        locked = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        self.assertIn('classify_production_outcome', locked)
        self.assertIn('exec_out = _wake_exec(', locked)
        self.assertIn("_mark_production_attempt(\n            'failed', action=action, reason=str(_exec_exc)", locked)

    def test_c3_relay_isolation_no_global_singleton(self):
        """C3: K freeze / Shadow must not mutate production RelayManager singleton."""
        from relay.manager import RelayManager
        from relay import manager as relay_mod
        import chat.capability_skill_view as csv
        import chat.planner_shadow as ps

        global_relay = relay_mod.relay
        before_url = global_relay.api_url
        before_model = global_relay.model
        # Poison the singleton; isolated path must not read/write this object.
        global_relay.api_url = 'https://poisoned.example/v1'
        global_relay.model = 'poisoned-model'
        global_relay.api_key = 'poisoned-key'

        created = []

        class TrackingRelay(RelayManager):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                created.append(self)

            def call(self, payload, timeout=60, use_ws_model=False):
                del payload, timeout, use_ws_model
                return {'content': [{'type': 'text', 'text': '{}'}]}

            def extract_text(self, result):
                return '{}'

        # resolve_model_identity / Shadow import RelayManager from relay.manager.
        with mock.patch('relay.manager.RelayManager', TrackingRelay):
            identity = csv.resolve_model_identity('api_relay')
            ps.invoke_shadow_planner_relay(user_payload='{}', timeout_sec=1)
        self.assertGreaterEqual(len(created), 2)
        for inst in created:
            self.assertIsNot(inst, global_relay)
            self.assertNotEqual(inst.api_url, 'https://poisoned.example/v1')
            self.assertNotEqual(inst.model, 'poisoned-model')
        self.assertNotEqual(identity, 'poisoned-model')

        # Production singleton must remain the same object (not replaced / cleared).
        self.assertIs(relay_mod.relay, global_relay)
        self.assertEqual(global_relay.api_url, 'https://poisoned.example/v1')
        self.assertEqual(global_relay.model, 'poisoned-model')

        # Restore for other tests in-process.
        global_relay.api_url = before_url
        global_relay.model = before_model

        # Source-level: no import of the production singleton alias.
        cap_src = Path(ROOT, 'chat/capability_skill_view.py').read_text(encoding='utf-8')
        shadow_src = Path(ROOT, 'chat/planner_shadow.py').read_text(encoding='utf-8')
        self.assertNotIn('from relay.manager import relay', cap_src)
        self.assertNotIn('from relay.manager import relay', shadow_src)
        self.assertIn('RelayManager()', shadow_src)

    def test_shadow_relay_payload_omits_observation_metadata(self):
        """Anthropic metadata allows only user_id; Shadow must send none."""
        from relay.manager import RelayManager
        import chat.planner_shadow as ps

        captured = []

        class CaptureRelay(RelayManager):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)

            def call(self, payload, timeout=60, use_ws_model=False):
                del timeout, use_ws_model
                captured.append(dict(payload))
                return {'content': [{'type': 'text', 'text': '{}'}]}

            def extract_text(self, result):
                del result
                return '{}'

        with mock.patch('relay.manager.RelayManager', CaptureRelay):
            ps.invoke_shadow_planner_relay(user_payload='{}', timeout_sec=1)

        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertNotIn('metadata', payload)
        # Observation semantics remain in Decision/JSONL source, not HTTP.
        shadow_src = Path(ROOT, 'chat/planner_shadow.py').read_text(encoding='utf-8')
        self.assertNotIn(
            "'metadata': {'source': 'planner_shadow', 'shadow_only': True}",
            shadow_src,
        )

    def test_blocked_bool_and_none_coupling(self):
        """Optional hardening: blocked string 'false' ≠ True; blocked⇒none."""
        ok, decision = validate_shadow_decision(
            {
                'intent': 'stay_quiet',
                'action_candidate': 'none',
                'confidence': 0.5,
                'primary_drive': None,
                'contributors': [],
                'blocked': 'false',
                'reason_codes': [],
            },
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            planner_decision_id='pd-1',
            decision_attempt_id='da-1',
            captured_at='2026-08-04 15:00:00',
        )
        self.assertEqual(ok, 'valid')
        self.assertIs(decision['blocked'], False)

        bad, err = validate_shadow_decision(
            {
                'intent': 'say_hi',
                'action_candidate': 'message',
                'confidence': 0.5,
                'primary_drive': 'attachment',
                'contributors': [],
                'blocked': True,
                'reason_codes': [],
            },
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            planner_decision_id='pd-2',
            decision_attempt_id='da-2',
            captured_at='2026-08-04 15:00:00',
        )
        self.assertEqual(bad, 'invalid')
        self.assertEqual(err['error'], 'blocked_requires_none_action')

    def test_capability_view_model_identity_and_tools(self):
        self.assertEqual(self.skill.provider, 'api_relay')
        self.assertEqual(self.skill.frozen_after, 'prepare_tools_for_provider')
        self.assertTrue(self.skill.immutable)
        self.assertIn('web_search', self.skill.tool_allowlist)
        self.assertIn('none', self.skill.resolved_action_capability)
        self.assertIn('message', self.skill.resolved_action_capability)

    def test_evidence_gitignore_shadow_jsonl(self):
        """Runtime planner_shadow.jsonl must not dirty the production git worktree."""
        gi = Path(ROOT, '.gitignore').read_text(encoding='utf-8')
        self.assertIn('planner_shadow.jsonl', gi)

    def test_evidence_shadow_model_identity_is_relay_not_production_k(self):
        """Observation provider/model = Shadow Relay thinker, not production Wake K."""
        with mock.patch(
            'chat.cc_model.cc_model_snapshot',
            return_value=(
                'claude-opus-4-6',
                'explicit:claude-opus-4-6',
                ['--model', 'claude-opus-4-6'],
            ),
        ):
            cc_skill = freeze_capability_skill_view(
                wake_run_id='b11b-run-1',
                provider='claude_code',
                mode='normal',
                prepared_tools=[{'name': 'recall_photo'}],
                dry_run=False,
                captured_at=T_OBS,
            )
        self.assertEqual(cc_skill.provider, 'claude_code')

        def fake_invoke(*, user_payload, timeout_sec):
            del user_payload, timeout_sec
            return {
                'text': json.dumps({
                    'intent': 'reconnect_gently',
                    'action_candidate': 'message',
                    'confidence': 0.7,
                    'primary_drive': 'attachment',
                    'contributors': ['longing', 'bond.passion'],
                    'blocked': False,
                    'reason_codes': [],
                    'wake_run_id': 'b11b-run-1',
                    'state_version': int(self.view.state_version),
                }),
                'provider': 'api_relay',
                'model_identity': 'shadow-relay-model-x',
            }

        rec = run_shadow_attempt(
            planner_view=self.view,
            skill_view=cc_skill,
            wake_run_id='b11b-run-1',
            decision_attempt_id=self.attempt_id,
            invoke_fn=fake_invoke,
        )
        self.assertEqual(rec['shadow_status'], 'valid')
        self.assertEqual(rec['provider'], 'api_relay')
        self.assertEqual(rec['model_identity'], 'shadow-relay-model-x')
        self.assertEqual(rec['capability']['production_provider'], 'claude_code')
        self.assertEqual(
            rec['capability']['production_model_identity'],
            'explicit:claude-opus-4-6',
        )
        self.assertNotEqual(rec['provider'], 'claude_code')

    def test_evidence_contributors_must_be_state_factors(self):
        """contributors must be Drive/Affect/Bond/Longing factors — not free text."""
        ok, decision = validate_shadow_decision(
            {
                'intent': 'reconnect',
                'action_candidate': 'message',
                'confidence': 0.6,
                'primary_drive': 'attachment',
                'contributors': ['longing', 'bond.passion', 'fatigue'],
                'blocked': False,
                'reason_codes': [],
            },
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            planner_decision_id='pd-ok',
            decision_attempt_id='da-ok',
            captured_at='2026-08-04 15:00:00',
        )
        self.assertEqual(ok, 'valid')
        self.assertEqual(
            decision['contributors'],
            ['longing', 'bond.passion', 'fatigue'],
        )

        bad, err = validate_shadow_decision(
            {
                'intent': 'reconnect',
                'action_candidate': 'message',
                'confidence': 0.6,
                'primary_drive': 'attachment',
                'contributors': ['banana'],
                'blocked': False,
                'reason_codes': [],
            },
            planner_view=self.view,
            skill_view=self.skill,
            wake_run_id='b11b-run-1',
            planner_decision_id='pd-bad',
            decision_attempt_id='da-bad',
            captured_at='2026-08-04 15:00:00',
        )
        self.assertEqual(bad, 'invalid')
        self.assertEqual(err['error'], 'contributor_not_state_factor')
        self.assertEqual(err['illegal_contributor'], 'banana')

    def test_gateway_seat_non_blocking_order(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        locked = src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('freeze_capability_skill_view', locked)
        self.assertIn('dispatch_planner_shadow', locked)
        self.assertIn('new_decision_attempt_id', locked)
        self.assertIn('mark_production_attempt_outcome', locked)
        self.assertLess(
            locked.index('prepare_tools_for_provider'),
            locked.index('dispatch_planner_shadow'),
        )
        # Attempt ID must be minted before dispatch (not only imported nearby).
        self.assertLess(
            locked.index('decision_attempt_id = new_decision_attempt_id()'),
            locked.index('dispatch_planner_shadow('),
        )
        self.assertLess(
            locked.index('dispatch_planner_shadow'),
            locked.index('get_wake_runner'),
        )
        # Failed runner path must orphan the attempt.
        self.assertIn("_mark_production_attempt('failed'", locked)
        # Success is classified from executor delivered∧settled, not unconditional.
        self.assertIn('classify_production_outcome', locked)
        self.assertIn('ritual_type=ritual_type', locked)
        # Shadow must not use CC Wake resident.
        shadow_src = Path(ROOT, 'chat/planner_shadow.py').read_text(encoding='utf-8')
        self.assertIn('RelayManager()', shadow_src)
        self.assertNotIn('ClaudeCodeWakeRunner', shadow_src)
        self.assertNotIn('_CC_WAKE_RESIDENT', shadow_src)


if __name__ == '__main__':
    unittest.main()
