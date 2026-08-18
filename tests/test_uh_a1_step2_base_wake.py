import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
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

    def _run_basic_executor(self, action):
        from wake.executor import execute

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir).joinpath('basic.db'))
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE wake_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thoughts TEXT,
                    action TEXT,
                    content TEXT,
                    consumed INTEGER,
                    woke_at TEXT,
                    cache_info TEXT,
                    wake_run_id TEXT
                );
                CREATE TABLE chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    author TEXT,
                    content TEXT,
                    thinking TEXT,
                    cache_info TEXT,
                    source_kind TEXT
                );
                """
            )
            conn.commit()
            conn.close()

            def get_db():
                return sqlite3.connect(db_path)

            with mock.patch(
                'chat.drive_authority.apply_wake_outcome_on_conn',
                side_effect=AssertionError('basic Wake must not enter V3 Settlement'),
            ):
                return execute(
                    action,
                    'basic planner intent',
                    'basic rendered content' if action == 'message' else '',
                    'normal',
                    get_db_fn=get_db,
                    wake_run_id='uh-a1-basic-executor',
                    settle_fired_drive=None,
                    settle_provenance_present=False,
                    settlement_required=False,
                )

    def test_basic_message_skips_v3_settlement_without_state_provenance(self):
        result = self._run_basic_executor('message')
        self.assertTrue(result['delivered'])
        self.assertFalse(result['settled'])
        self.assertIsNone(result['settle_status'])

    def test_basic_none_skips_v3_settlement_without_internal_state(self):
        result = self._run_basic_executor('none')
        self.assertTrue(result['delivered'])
        self.assertFalse(result['settled'])
        self.assertIsNone(result['settle_status'])

    def test_basic_executor_result_is_success_without_required_settlement(self):
        from chat.planner_shadow import classify_production_outcome

        self.assertEqual(
            classify_production_outcome({
                'delivered': True,
                'settled': False,
                'settlement_required': False,
            }),
            ('success', ''),
        )

    def test_unified_switch_gates_basic_planner_and_preserves_legacy_gate(self):
        gateway = Path(__file__).resolve().parents[1].joinpath('gateway.py').read_text(
            encoding='utf-8'
        )
        self.assertIn('and unified_normal_on', gateway)
        self.assertIn(
            "str(mode or 'normal').strip() != 'normal'\n            or unified_normal_on",
            gateway,
        )
        self.assertIn('allow_side_effects=(live and not basic_normal)', gateway)
        self.assertIn('NORMAL_WAKE_UNIFIED_UNOWNED_SKIP', gateway)

    def test_gateway_visible_path_keeps_shared_resident_and_outer_fence(self):
        from pathlib import Path
        gateway = Path(__file__).resolve().parents[1].joinpath('gateway.py').read_text(
            encoding='utf-8'
        )
        section = gateway[gateway.index("if _b2_plan is not None and _b2_plan.route == 'message_takeover'"):
            gateway.index("try:\n        runner = _wake_runners.get_wake_runner")
        ]
        b3 = Path(__file__).resolve().parents[1].joinpath(
            'chat', 'behavior_authority_b3.py'
        ).read_text(encoding='utf-8')
        shared = b3[b3.index('def _try_invoke_shared_renderer'):
            b3.index('def invoke_renderer(')
        ]
        self.assertIn('invoke_renderer', section)
        self.assertIn("getattr(gateway, '_CC_RESIDENT'", shared)
        self.assertIn('_try_invoke_shared_renderer', b3)
        self.assertIn('NORMAL_WAKE_UNIFIED_UNOWNED_SKIP', gateway)


if __name__ == '__main__':
    unittest.main()
