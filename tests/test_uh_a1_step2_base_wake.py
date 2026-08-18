import json
import unittest
from unittest import mock

from chat.authoritative_planner import BasicWakePlannerInput, run_authoritative_cc_planner
from chat.behavior_authority_b2 import plan_b2_wake_action


class UhA1Step2BaseWakeTests(unittest.TestCase):
    def setUp(self):
        import datetime
        self.input = BasicWakePlannerInput(
            wake_run_id='uh-a1-test-run',
            mode='normal',
            observed_at=datetime.datetime(2026, 8, 18, 16, 0),
            user_idle_hours=2.0,
            effective_idle_hours=2.0,
        )

    def _planner_json(self, action='none'):
        return json.dumps({
            'intent': 'test_intent',
            'action_candidate': action,
            'confidence': 0.9,
            'blocked': False,
            'reason_codes': [],
            'wake_run_id': self.input.wake_run_id,
        })

    def test_default_normal_planner_is_cc_not_shadow_relay(self):
        calls = []

        def invoke(*, user_payload, timeout_sec):
            calls.append((user_payload, timeout_sec))
            return {'text': self._planner_json()}

        with mock.patch(
            'chat.authoritative_planner.invoke_cc_planner',
            side_effect=invoke,
        ), mock.patch(
            'chat.planner_shadow.invoke_shadow_planner_relay',
            side_effect=AssertionError('production must not use Shadow Relay'),
        ):
            status, decision = run_authoritative_cc_planner(
                planner_input=self.input,
                wake_run_id=self.input.wake_run_id,
                decision_attempt_id='uh-a1-da',
            )

        self.assertEqual(status, 'valid')
        self.assertEqual(decision['source'], 'authoritative_cc_planner')
        self.assertEqual(len(calls), 1)

    def test_basic_payload_has_no_internal_state_fields(self):
        payload = json.loads(self.input.build_user_payload())
        self.assertEqual(payload['extra_context'], {})
        for forbidden in (
            'affect', 'drive', 'longing', 'internal_state', 'Internal State',
            'primary_drive', 'contributors', 'state_version',
        ):
            self.assertNotIn(forbidden, payload)

    def test_message_decision_keeps_b3_takeover(self):
        import datetime
        from chat.behavior_authority_b2 import B2WakePlan
        decision = json.loads(self._planner_json('message'))

        class Skill:
            wake_run_id = 'uh-a1-test-run'
            resolved_action_capability = ('none', 'message')

        with mock.patch(
            'chat.behavior_authority_b2.consumer_enabled',
            return_value=True,
        ), mock.patch(
            'chat.behavior_authority_b3.effective_b3_enabled',
            return_value=True,
        ), mock.patch(
            'chat.planner_shadow.run_authoritative_planner_decision',
            return_value=('valid', {
                **decision,
                'source': 'authoritative_cc_planner',
                'authoritative': True,
                'shadow_only': False,
            }),
        ), mock.patch(
            'chat.behavior_authority_b3.evaluate_message_gate',
            return_value=('allow', 'message_gate_allow'),
        ):
            plan = plan_b2_wake_action(
                planner_view=self.input,
                skill_view=Skill(),
                wake_run_id=self.input.wake_run_id,
                decision_attempt_id='uh-a1-da',
                get_db_fn=lambda: None,
                now=datetime.datetime(2026, 8, 18, 16, 0),
                mode='normal',
            )

        self.assertEqual(plan.route, 'message_takeover')

    def test_cc_failure_is_blocked_without_legacy_or_relay(self):
        with mock.patch(
            'chat.authoritative_planner.invoke_cc_planner',
            side_effect=RuntimeError('cc unavailable'),
        ), mock.patch(
            'chat.planner_shadow.invoke_shadow_planner_relay',
            side_effect=AssertionError('Relay fallback forbidden'),
        ):
            status, decision = run_authoritative_cc_planner(
                planner_input=self.input,
                wake_run_id=self.input.wake_run_id,
                decision_attempt_id='uh-a1-da',
            )
        self.assertEqual(status, 'error')
        self.assertEqual(decision['source'], 'authoritative_cc_planner')

    def test_gateway_visible_path_keeps_shared_resident_and_outer_fence(self):
        from pathlib import Path
        gateway = Path(__file__).resolve().parents[1].joinpath('gateway.py').read_text(
            encoding='utf-8'
        )
        section = gateway[gateway.index("if _b2_plan is not None and _b2_plan.route == 'message_takeover'"):
            gateway.index("try:\n        runner = _wake_runners.get_wake_runner")
        ]
        self.assertIn('invoke_renderer', section)
        self.assertIn('_CC_RESIDENT', section)
        self.assertIn('NORMAL_WAKE_UNIFIED_UNOWNED_SKIP', gateway)


if __name__ == '__main__':
    unittest.main()
