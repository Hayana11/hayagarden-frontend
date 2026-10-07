"""Focused contracts for opt-in Hidden Flow Daily orchestration and storage."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest

from chat.hidden_flow import runtime, runtime_store
from chat.hidden_flow.config import normalize_flow_config
from chat.hidden_flow.control import parse_hidden_flow_control
from chat.hidden_flow.types import FlowState


def _raw_config() -> dict:
    return {
        "schemaVersion": 1,
        "flowId": "demo-flow",
        "enabled": True,
        "initialStage": "s1",
        "stages": [
            {
                "id": "s1",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStage": "s1",
                "holdable": True,
                "poolIds": ["cycle-pool", "turn-pool"],
            },
        ],
        "cues": [
            {"id": "cue-a", "key": "a", "enabled": True, "poolIds": ["cycle-pool"]},
        ],
        "pools": [
            {
                "id": "cycle-pool",
                "enabled": True,
                "drawMode": "cycle",
                "drawCount": 1,
                "entries": [
                    {"id": "one", "text": "cycle one"},
                    {"id": "two", "text": "cycle two"},
                ],
            },
            {
                "id": "turn-pool",
                "enabled": True,
                "drawMode": "turn",
                "drawCount": 1,
                "entries": [{"id": "turn", "text": "turn draw"}],
            },
        ],
    }


class HiddenFlowRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

    def tearDown(self) -> None:
        os.unlink(self.db_path)

    def test_gate_off_does_not_create_hidden_schema(self) -> None:
        plan = runtime.prepare_hidden_flow_turn(
            enabled=False,
            eligible=False,
            chat_id="default",
            user_message_id=1,
            db_path=self.db_path,
        )
        self.assertFalse(plan.enabled)
        conn = sqlite3.connect(self.db_path)
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'hidden_flow_%'"
        ).fetchall()
        conn.close()
        self.assertEqual(names, [])

    def test_activation_and_explicit_start_only(self) -> None:
        runtime_store.upsert_flow_config(_raw_config(), db_path=self.db_path)
        plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=10,
            db_path=self.db_path,
        )
        self.assertIn("<available_hidden_flows>", plan.private_request_block)
        self.assertNotIn("cycle one", plan.private_request_block)
        self.assertIsNone(
            runtime.propose_transition(
                plan,
                parse_hidden_flow_control(
                    '<hidden_flow_control flow="demo-flow" keys="a"/>'
                ),
            )
        )
        transition = runtime.propose_transition(
            plan,
            parse_hidden_flow_control(
                '<hidden_flow_control flow="demo-flow" action="start" keys="a"/>'
            ),
        )
        self.assertIsNotNone(transition)
        self.assertTrue(transition.state_after.active)
        self.assertEqual(transition.state_after.context_keys, ("a",))

    def test_pending_commit_contains_real_cycle_draw_and_roundtrips(self) -> None:
        version = runtime_store.upsert_flow_config(_raw_config(), db_path=self.db_path)
        plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=11,
            db_path=self.db_path,
        )
        transition = runtime.propose_transition(
            plan,
            parse_hidden_flow_control(
                '<hidden_flow_control flow="demo-flow" action="start" keys="a"/>'
            ),
        )
        assert transition is not None
        snapshot = runtime.build_pending_snapshot(
            plan, transition, assistant_message_id=12
        )
        self.assertTrue(snapshot["state_after"]["fixedDraws"])
        encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
        conn = runtime_store._connect(self.db_path)
        runtime_store.stage_snapshot(conn, decoded)
        runtime_store.finalize_snapshot(conn, 12)
        conn.commit()
        conn.close()
        loaded = runtime_store.load_runtime("default", db_path=self.db_path)
        self.assertEqual(loaded["version"], 1)
        self.assertTrue(loaded["state"].fixed_draws)
        self.assertEqual(loaded["config_version"], version)
        self.assertEqual(loaded["pending_guide"].source_message_id, "12")

    def test_invalid_snapshot_values_and_caps_fail_closed(self) -> None:
        config = normalize_flow_config(_raw_config())
        assert config is not None
        with self.assertRaises(ValueError):
            FlowState.from_dict({
                **FlowState.inactive().to_dict(),
                "active": True,
                "flowId": "demo-flow",
                "stage": "s1",
                "cycle": 1,
                "stageTurn": 1,
                "fixedDraws": {
                    "cycle-pool": [
                        {
                            "poolId": "cycle-pool",
                            "entryId": "one",
                            "text": "x",
                            "drawIndex": 0,
                            "seed": "s",
                        }
                    ] * 13
                },
            })
        conn = runtime_store._connect(self.db_path)
        with self.assertRaises(ValueError):
            runtime_store.stage_snapshot(conn, {
                "assistant_message_id": 1,
                "user_message_id": 1,
                "chat_id": "default",
                "runtime_version_before": 0,
                "state_before": FlowState.inactive().to_dict(),
                "state_after": FlowState.inactive().to_dict(),
                "control": {"flowId": "demo-flow", "keys": [object()]},
            })
        conn.close()


if __name__ == "__main__":
    unittest.main()
