"""Focused contracts for opt-in Hidden Flow Daily orchestration and storage."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from chat.hidden_flow import runtime, runtime_store
from chat.hidden_flow.config import normalize_flow_config
from chat.hidden_flow.control import parse_hidden_flow_control
from chat.hidden_flow.types import FlowState
import chat.daily_runtime as daily_runtime


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
                "nextStage": "s2",
                "holdable": True,
                "poolIds": ["cycle-pool", "turn-pool"],
            },
            {
                "id": "s2",
                "enabled": True,
                "terminalWithoutContinue": True,
                "continueTarget": "s1",
                "poolIds": [],
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

    def _commit_started_runtime(self) -> None:
        runtime_store.upsert_flow_config(_raw_config(), db_path=self.db_path)
        plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=20,
            db_path=self.db_path,
        )
        transition = runtime.propose_transition(
            plan,
            parse_hidden_flow_control(
                '<hidden_flow_control flow="demo-flow" action="start" keys="a"/>'
            ),
        )
        self.assertIsNotNone(transition)
        snapshot = runtime.build_pending_snapshot(
            plan, transition, assistant_message_id=21
        )
        conn = runtime_store._connect(self.db_path)
        runtime_store.stage_snapshot(conn, snapshot)
        runtime_store.finalize_snapshot(conn, 21)
        conn.commit()
        conn.close()

    def test_active_runtime_with_new_config_version_fails_closed(self) -> None:
        self._commit_started_runtime()
        before = runtime_store.load_runtime("default", db_path=self.db_path)
        self.assertTrue(before["state"].active)
        self.assertEqual(before["config_version"], 1)
        runtime_store.upsert_flow_config(_raw_config(), db_path=self.db_path)
        plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=22,
            db_path=self.db_path,
        )
        self.assertFalse(plan.state_before.active)
        self.assertIsNone(plan.applied_guide)
        self.assertIsNone(plan.selected_config)
        self.assertIn("<available_hidden_flows>", plan.private_request_block)
        after = runtime_store.load_runtime("default", db_path=self.db_path)
        self.assertTrue(after["state"].active)
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["config_version"], before["config_version"])

    def test_active_runtime_without_matching_pending_guide_fails_closed(self) -> None:
        self._commit_started_runtime()
        conn = runtime_store._connect(self.db_path)
        conn.execute(
            "UPDATE hidden_flow_runtime SET pending_guide_json=NULL WHERE chat_id=?",
            ("default",),
        )
        conn.commit()
        conn.close()
        plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=23,
            db_path=self.db_path,
        )
        self.assertFalse(plan.state_before.active)
        self.assertIsNone(plan.applied_guide)
        self.assertIn("<available_hidden_flows>", plan.private_request_block)

    def test_activation_block_never_returns_a_cut_protocol_tag(self) -> None:
        config = normalize_flow_config(_raw_config())
        self.assertIsNotNone(config)
        block = runtime.build_activation_block(
            [(config, 1)] * 8,
            max_chars=4000,
        )
        self.assertLessEqual(len(block), 4000)
        self.assertTrue(block.startswith("<available_hidden_flows>"))
        self.assertTrue(block.endswith("</available_hidden_flows>"))
        short = runtime.build_activation_block(
            [(config, 1)] * 8,
            max_chars=240,
        )
        self.assertLessEqual(len(short), 240)
        self.assertTrue(
            short == "" or short.endswith("</available_hidden_flows>")
        )


    def _daily_plan(self, *, hidden_plan=None) -> daily_runtime.DailyTurnPlan:
        plan = daily_runtime.DailyTurnPlan(
            request_id="r1",
            chat_id="default",
            local_day="2026-10-08",
            context_id=1,
            context_epoch=1,
            resident_generation=1,
            resident_key="default:e1:g1",
            user_message_id=30,
            epoch_token={},
            lease_owner="owner",
            is_cold=False,
            is_respawn=False,
            cursor_before=0,
            assembly={},
            manifest={"provider": "claude_code", "model": "model"},
            user_content="hello",
            db_path=self.db_path,
            worker_id="worker",
            turn_lease={"lease": "test"},
        )
        if hidden_plan is not None:
            plan.hidden_flow_enabled = True
            plan.hidden_flow_eligible = True
            plan.hidden_flow_plan = hidden_plan
        return plan

    def test_real_daily_stream_injects_and_filters_text_and_thinking(self) -> None:
        hidden_plan = runtime.HiddenFlowTurnPlan(
            enabled=True,
            eligible=True,
            private_request_block="<available_hidden_flows><flow id=\"demo-flow\"/></available_hidden_flows>",
        )
        plan = self._daily_plan(hidden_plan=hidden_plan)
        sent: list[object] = []
        resident = mock.Mock()
        resident.generation = 1
        resident.session_id = "session-1"
        resident.ensure_alive.return_value = False

        def send_turn(content, **_kwargs):
            sent.append(content)
            yield "text", 'visible<hidden_flow_control flow="demo-flow" action="hold"/>'
            yield "think", (
                "reason<hidden_flow_guidance flow=\"demo-flow\">private"
                "</hidden_flow_guidance>tail"
            )
            yield "text", "tail"
            yield "done", {}

        resident.send_turn.side_effect = send_turn

        class Heartbeat:
            failed = False

            def __init__(self, *_args, **_kwargs):
                pass

            def start(self):
                return None

            def stop(self):
                return False

        patches = [
            mock.patch.object(daily_runtime, "verify_epoch_token"),
            mock.patch.object(daily_runtime, "LeaseHeartbeat", Heartbeat),
            mock.patch.object(
                daily_runtime,
                "peek_registered_respawn_decision",
                return_value={"requires_respawn": False},
            ),
            mock.patch.object(daily_runtime, "_validate_hot_no_op_payload"),
            mock.patch.object(daily_runtime, "_observe_continuity_shadow"),
            mock.patch.object(daily_runtime, "_capture_transcript_start"),
            mock.patch.object(daily_runtime, "_capture_transcript_end"),
            mock.patch.object(daily_runtime.dc, "get_resident_history_cursor", return_value=0),
            mock.patch.object(daily_runtime.dc, "upsert_resident_owner"),
        ]
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            events = list(
                daily_runtime.ensure_resident_and_stream(
                    plan,
                    resident=resident,
                    env={},
                    static_system="system",
                )
            )
        self.assertEqual(sent[0], "hello\n\n" + hidden_plan.private_request_block)
        self.assertEqual(
            [(kind, payload) for kind, payload in events if kind in {"text", "think"}],
            [("text", "visible"), ("think", "reasontail"), ("text", "tail")],
        )

        off_plan = self._daily_plan()
        off_sent: list[object] = []
        off_resident = mock.Mock()
        off_resident.generation = 1
        off_resident.session_id = "session-1"
        off_resident.ensure_alive.return_value = True
        off_resident.send_turn.side_effect = lambda content, **_kwargs: (
            off_sent.append(content) or iter([("done", {})])
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
            list(
                daily_runtime.ensure_resident_and_stream(
                    off_plan,
                    resident=off_resident,
                    env={},
                    static_system="system",
                )
            )
        self.assertEqual(off_sent, ["hello"])

    def test_daily_plan_canonical_persist_and_terminal_callbacks(self) -> None:
        self._commit_started_runtime()
        hidden_plan = runtime.prepare_hidden_flow_turn(
            enabled=True,
            eligible=True,
            chat_id="default",
            user_message_id=30,
            db_path=self.db_path,
        )
        plan = self._daily_plan(hidden_plan=hidden_plan)
        fd, transcript_path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        rows = [
            {
                "type": "assistant",
                "sessionId": "session-1",
                "uuid": "a1",
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": (
                                'reason<hidden_flow_control flow="demo-flow" '
                                'action="hold"/>tail'
                            ),
                        },
                        {"type": "text", "text": "answer"},
                    ],
                },
            },
            {"type": "result", "sessionId": "session-1", "stop_reason": "end_turn"},
        ]
        with open(transcript_path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        try:
            plan.transcript_path = transcript_path
            plan.transcript_start_offset = 0
            plan.transcript_end_offset = os.path.getsize(transcript_path)
            plan.transcript_claude_session_id = "session-1"
            plan.transcript_process_generation = 1
            turn = daily_runtime.build_canonical_turn_for_plan(plan)
            self.assertEqual(turn.content, "answer")
            self.assertEqual(turn.thinking, "reasontail")
            plan._hidden_flow_transition = runtime.propose_transition(
                hidden_plan,
                runtime.control_from_dict(turn.hidden_flow_control),
            )
            captured: dict[str, object] = {}

            def fake_persist(**kwargs):
                captured.update(kwargs)
                return 100

            with mock.patch.object(
                daily_runtime.dc,
                "persist_daily_assistant_if_current",
                side_effect=fake_persist,
            ):
                self.assertEqual(
                    daily_runtime.persist_daily_assistant_for_plan(
                        plan,
                        content=turn.content,
                        thinking=turn.thinking,
                        display_segments=turn.display_segments,
                    ),
                    100,
                )
            callback = captured["hidden_flow_stage_callback"]
            self.assertTrue(callable(callback))
            conn = runtime_store._connect(self.db_path)
            callback(conn, 100)
            conn.commit()
            conn.close()

            def fake_finalize(*_args, hidden_flow_finalize_callback=None, **_kwargs):
                conn = runtime_store._connect(self.db_path)
                hidden_flow_finalize_callback(conn, 100)
                conn.commit()
                conn.close()
                return {
                    "history_cursor_message_id": 100,
                    "context_id": 1,
                    "resident_generation": 1,
                    "advanced": True,
                }

            with mock.patch.object(daily_runtime, "verify_epoch_token"), \
                    mock.patch.object(daily_runtime, "_release_lease"), \
                    mock.patch.object(
                        daily_runtime.dc,
                        "finalize_daily_assistant_and_advance_cursor",
                        side_effect=fake_finalize,
                    ):
                daily_runtime.complete_daily_turn(
                    plan,
                    assistant_message_id=100,
                )
            conn = runtime_store._connect(self.db_path)
            status = conn.execute(
                "SELECT status FROM hidden_flow_message_snapshots "
                "WHERE assistant_message_id=?",
                (100,),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(status, runtime_store.SNAPSHOT_COMMITTED)
            self.assertEqual(
                runtime_store.load_runtime("default", db_path=self.db_path)["version"],
                2,
            )
        finally:
            os.unlink(transcript_path)



if __name__ == "__main__":
    unittest.main()
