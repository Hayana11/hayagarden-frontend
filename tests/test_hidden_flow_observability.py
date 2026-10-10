import json
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from chat.hidden_flow import observability
from chat.hidden_flow.config import DEFAULT_FLOW_CONFIG, normalize_flow_config
from chat.hidden_flow.control import classify_hidden_flow_control
from chat.hidden_flow.engine import apply_flow_control, start_flow
from chat.hidden_flow.runtime import (
    HiddenFlowTransition,
    HiddenFlowTurnPlan,
    build_pending_snapshot,
)
from chat.hidden_flow import runtime_store
from chat.hidden_flow.types import FlowControl, FlowState


class HiddenFlowObservabilityTests(unittest.TestCase):
    def setUp(self):
        observability.reset_for_tests()

    def tearDown(self):
        observability.reset_for_tests()

    @staticmethod
    def _fake_config_store(*, enabled, max_events=120, retention_days=7):
        def get_bool(key, default=False):
            if key == "HIDDEN_FLOW_OBSERVABILITY_ENABLED":
                return bool(enabled)
            return True if key == "HIDDEN_FLOW_ENGINE_ENABLED" else default

        def get_int(key, default=0):
            if key == "HIDDEN_FLOW_OBSERVABILITY_MAX_EVENTS_PER_MINUTE":
                return max_events
            if key == "HIDDEN_FLOW_OBSERVABILITY_RETENTION_DAYS":
                return retention_days
            return default

        return types.SimpleNamespace(get_bool=get_bool, get_int=get_int)

    @staticmethod
    def _payloads(calls):
        return [json.loads(call.args[0]) for call in calls]

    def test_gate_closed_emits_nothing(self):
        fake = self._fake_config_store(enabled=False)
        with mock.patch.dict(sys.modules, {"config_store": fake}):
            with mock.patch.object(observability._LOGGER, "info") as info:
                context = observability.begin_turn(
                    provider="claude_code",
                    turn_kind="hot",
                    gate_enabled=False,
                    eligible=False,
                )
                context.record_prepare(outcome="skipped", error_code="gate_off")
                context.record_control_parse("none")
                self.assertFalse(context.enabled)
                info.assert_not_called()

    def test_rate_limit_and_retention_metadata(self):
        fake = self._fake_config_store(
            enabled=True,
            max_events=1,
            retention_days=3,
        )
        with mock.patch.dict(sys.modules, {"config_store": fake}):
            with mock.patch.object(observability._LOGGER, "info") as info:
                context = observability.begin_turn(
                    provider="claude_code",
                    turn_kind="cold",
                    gate_enabled=True,
                    eligible=True,
                )
                context.record_prepare(
                    outcome="success",
                    available_config_count=1,
                    private_guidance_nonempty=True,
                )
                context.record_request_attachment(attached=True)
                self.assertEqual(info.call_count, 1)
                payload = self._payloads(info.call_args_list)[0]
                self.assertEqual(payload["event"], "hidden_flow_observation")
                self.assertGreater(payload["retention_until"], payload["observed_at"])
                self.assertEqual(payload["available_config_count"], 1)
                self.assertTrue(payload["private_guidance_nonempty"])
                self.assertNotIn("message_id", payload)
                self.assertNotIn("user_message_id", payload)
                self.assertNotIn("assistant_message_id", payload)

    def test_prepare_success_failure_and_ineligible_are_classified(self):
        fake = self._fake_config_store(enabled=True)
        with mock.patch.dict(sys.modules, {"config_store": fake}):
            with mock.patch.object(observability._LOGGER, "info") as info:
                context = observability.begin_turn(
                    provider="claude_code",
                    turn_kind="hot",
                    gate_enabled=True,
                    eligible=True,
                )
                context.record_prepare(
                    outcome="success",
                    available_config_count=1,
                    private_guidance_nonempty=True,
                )
                context.record_prepare(outcome="failed", error_code="RuntimeError")
                context.record_prepare(outcome="skipped", error_code="ineligible")
                payloads = self._payloads(info.call_args_list)
                self.assertEqual(
                    [item["prepare"] for item in payloads],
                    ["success", "failed", "skipped"],
                )

    def test_attachment_control_transition_and_no_leak(self):
        fake = self._fake_config_store(enabled=True)
        secret = "PRIVATE_GUIDANCE_SENTINEL"
        with mock.patch.dict(sys.modules, {"config_store": fake}):
            with mock.patch.object(observability._LOGGER, "info") as info:
                context = observability.begin_turn(
                    provider="claude_code",
                    turn_kind="respawn",
                    gate_enabled=True,
                    eligible=True,
                )
                context.record_prepare(
                    outcome="success",
                    available_config_count=1,
                    private_guidance_nonempty=True,
                )
                context.record_request_attachment(attached=True)
                context.record_control_parse("none")
                context.record_transition(created=False)
                context.record_control_parse("valid")
                context.record_transition(created=True)
                for status in ("pending", "committed", "conflict", "rollback"):
                    context.record_snapshot(status, error_code="test")
                rendered = json.dumps(
                    [json.loads(call.args[0]) for call in info.call_args_list],
                    ensure_ascii=False,
                )
                self.assertNotIn(secret, rendered)
                self.assertNotIn("<hidden_flow_control", rendered)
                self.assertNotIn("message_id", rendered)
                self.assertNotIn("token", rendered.lower())
                self.assertNotIn("cookie", rendered.lower())

    def test_control_classification(self):
        self.assertEqual(classify_hidden_flow_control("", expected_flow_id=None), "none")
        self.assertEqual(
            classify_hidden_flow_control(
                '<hidden_flow_control flow="demo" action="start"/>',
            ),
            "valid",
        )
        self.assertEqual(
            classify_hidden_flow_control(
                '<hidden_flow_control flow="other" action="start"/>',
                expected_flow_id="demo",
            ),
            "flow_id_mismatch",
        )
        self.assertEqual(
            classify_hidden_flow_control(
                '<hidden_flow_control flow="demo" action="start"/> trailing',
            ),
            "invalid",
        )

    def test_started_flow_without_advance_stays_in_stage(self):
        config = normalize_flow_config(DEFAULT_FLOW_CONFIG)
        self.assertIsNotNone(config)
        control = FlowControl(flow_id=config.flow_id, action="start", keys=())
        state = start_flow(config, control, started_at="diagnostic", engine_enabled=True)
        continued = apply_flow_control(config, state, None, engine_enabled=True)
        self.assertTrue(state.active)
        self.assertEqual(continued.stage, state.stage)
        self.assertEqual(continued.stage_turn, state.stage_turn + 1)

    def test_snapshot_outcomes_and_cas_conflict_are_observable(self):
        config = normalize_flow_config(DEFAULT_FLOW_CONFIG)
        self.assertIsNotNone(config)
        with tempfile.TemporaryDirectory(prefix="hidden-flow-observability-") as tmp:
            db = str(Path(tmp) / "runtime.sqlite3")
            runtime_store.ensure_hidden_flow_schema(db_path=db)
            now = "2026-10-11 00:00:00"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO hidden_flow_configs(flow_id,schema_version,enabled,config_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        config.flow_id,
                        config.schema_version,
                        1,
                        json.dumps(DEFAULT_FLOW_CONFIG, ensure_ascii=False),
                        1,
                        now,
                        now,
                    ),
                )
                conn.commit()
            plan = HiddenFlowTurnPlan(
                enabled=True,
                eligible=True,
                chat_id="diagnostic-chat",
                user_message_id=1,
                config_version=1,
                state_before=FlowState.inactive(),
                available_configs=((config, 1),),
                private_request_block="",
            )
            control = FlowControl(flow_id=config.flow_id, action="start", keys=())
            transition = HiddenFlowTransition(
                config=config,
                config_version=1,
                state_before=FlowState.inactive(),
                state_after=start_flow(
                    config,
                    control,
                    started_at="diagnostic",
                    engine_enabled=True,
                ),
                control=control,
                applied_guide=None,
            )
            snapshot = build_pending_snapshot(
                plan,
                transition,
                assistant_message_id=2,
            )
            context = observability.HiddenFlowObservation(
                enabled=True,
                turn_id="test-correlation",
                provider="claude_code",
                turn_kind="hot",
                gate_enabled=True,
                eligible=True,
            )
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                runtime_store.stage_snapshot(conn, snapshot)
                conn.commit()
                context.record_snapshot("pending")
                runtime_store.finalize_snapshot(conn, 2)
                conn.commit()
                context.record_snapshot("committed")
            conflict_snapshot = build_pending_snapshot(
                plan,
                transition,
                assistant_message_id=3,
            )
            with sqlite3.connect(db) as conn:
                conn.row_factory = sqlite3.Row
                runtime_store.stage_snapshot(conn, conflict_snapshot)
                conn.commit()
                with self.assertRaises(runtime_store.HiddenFlowConflict):
                    runtime_store.finalize_snapshot(conn, 3)
            with mock.patch.object(observability._LOGGER, "info") as info:
                context.record_snapshot("conflict", error_code="HiddenFlowConflict")
                with sqlite3.connect(db) as bad_conn:
                    with self.assertRaises(ValueError):
                        runtime_store.stage_snapshot(bad_conn, {"invalid": True})
                context.record_snapshot("rollback", error_code="transaction_rollback")
                payloads = self._payloads(info.call_args_list)
                self.assertEqual(
                    [item["snapshot_status"] for item in payloads],
                    ["conflict", "rollback"],
                )
                self.assertEqual(
                    [item["turn_id"] for item in payloads],
                    ["test-correlation", "test-correlation"],
                )


if __name__ == "__main__":
    unittest.main()
