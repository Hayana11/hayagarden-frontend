"""Focused contracts for the disabled-by-default hidden flow foundation."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

from chat.hidden_flow.config import DEFAULT_FLOW_CONFIG, normalize_flow_config
from chat.hidden_flow.control import parse_hidden_flow_control, sanitize_hidden_flow_text
from chat.hidden_flow.draw import draw_for_guide, draw_pool, render_private_guide
from chat.hidden_flow.engine import apply_flow_control as _apply_flow_control
from chat.hidden_flow.engine import start_flow as _start_flow
from chat.hidden_flow.stream_filter import HiddenFlowStreamFilter
from chat.hidden_flow.types import (
    AppliedGuide,
    FlowConfig,
    FlowControl,
    FlowState,
    PoolEntry,
    PoolSpec,
)


def start_flow(config, control, **kwargs):
    return _start_flow(config, control, engine_enabled=True, **kwargs)


def apply_flow_control(config, state, control=None, **kwargs):
    return _apply_flow_control(config, state, control, engine_enabled=True, **kwargs)


def _control(flow: str, action: str | None = None, keys: str = "") -> FlowControl:
    pieces = [f'<hidden_flow_control flow="{flow}"']
    if action is not None:
        pieces.append(f' action="{action}"')
    if keys:
        pieces.append(f' keys="{keys}"')
    pieces.append("/>")
    return parse_hidden_flow_control("".join(pieces))  # type: ignore[return-value]


def _raw_config(*, enabled: bool = True, terminal: bool = True) -> dict:
    return {
        "schemaVersion": 1,
        "flowId": " demo-flow ",
        "enabled": enabled,
        "initialStage": "s1",
        "stages": [
            {
                "id": "s1",
                "enabled": True,
                "minTurns": 2,
                "repeatMinTurns": 13,
                "nextStage": "s2",
                "holdable": True,
                "poolIds": ["shared", "missing"],
            },
            {
                "id": "s2",
                "enabled": True,
                "minTurns": 1,
                "nextStage": "s3",
                "poolIds": ["cycle-pool"],
            },
            {
                "id": "s3",
                "enabled": True,
                "terminalWithoutContinue": terminal,
                "continueTarget": "s1",
            },
        ],
        "cues": [
            {"id": "cue-a", "key": " Key-A ", "enabled": True, "poolIds": ["shared"]},
            {"id": "cue-b", "key": "key-a", "enabled": True, "poolIds": ["cycle-pool"]},
            {"id": "cue-off", "key": "off", "enabled": False, "poolIds": ["shared"]},
        ],
        "pools": [
            {
                "id": "shared",
                "enabled": True,
                "drawMode": "turn",
                "drawCount": 8,
                "entries": [
                    {"id": "a", "text": " A "},
                    {"id": "disabled", "enabled": False, "text": "do not draw"},
                    {"id": "b", "text": "B"},
                ],
            },
            {
                "id": "cycle-pool",
                "enabled": True,
                "drawMode": "cycle",
                "drawCount": 1,
                "entries": [{"id": "c", "text": "C"}],
            },
            {"id": "off-pool", "enabled": False, "drawMode": "turn", "entries": [{"text": "x"}]},
        ],
    }


class HiddenFlowParserTests(unittest.TestCase):
    def test_explicit_start_hold_continue_stop_parse(self) -> None:
        for action in ("start", "hold", "continue", "stop"):
            parsed = parse_hidden_flow_control(
                f'<hidden_flow_control flow="demo-flow" action="{action}"/>'
            )
            self.assertEqual(parsed, FlowControl("demo-flow", action, ()))

    def test_keys_only_never_means_start(self) -> None:
        parsed = parse_hidden_flow_control(
            '<hidden_flow_control flow="demo-flow" keys="context-a|context-b"/>'
        )
        self.assertEqual(parsed, FlowControl("demo-flow", None, ("context-a", "context-b")))

    def test_unknown_or_wrong_flow_fails_closed(self) -> None:
        self.assertIsNone(parse_hidden_flow_control('<hidden_flow_control flow="x" action="unknown"/>'))
        self.assertIsNone(
            parse_hidden_flow_control(
                '<hidden_flow_control flow="other" action="start"/>',
                expected_flow_id="demo-flow",
            )
        )

    def test_only_one_tail_control_can_drive_transition(self) -> None:
        self.assertIsNone(
            parse_hidden_flow_control(
                '正文 <hidden_flow_control flow="demo-flow" action="hold"/> 后面'
            )
        )
        self.assertIsNone(
            parse_hidden_flow_control(
                '<hidden_flow_control flow="demo-flow" action="hold"/>正文'
            )
        )


class HiddenFlowSanitizerTests(unittest.TestCase):
    def test_complete_valid_and_invalid_controls_are_invisible(self) -> None:
        self.assertEqual(
            sanitize_hidden_flow_text(
                'hello<hidden_flow_control flow="x" action="hold"/>world'
            ),
            "helloworld",
        )
        self.assertEqual(
            sanitize_hidden_flow_text('a<hidden_flow_control bad="x"/>b'),
            "ab",
        )
        self.assertEqual(
            sanitize_hidden_flow_text('a<hidden_flow_control flow="x/>y" action="hold"/>b'),
            "ab",
        )

    def test_partial_control_is_invisible(self) -> None:
        self.assertEqual(sanitize_hidden_flow_text("hello<hidden_flow_control"), "hello")
        self.assertEqual(sanitize_hidden_flow_text('hello<hidden_flow_control flow="x" act'), "hello")
        self.assertEqual(sanitize_hidden_flow_text("hello<hidden_flow_cont"), "hello")

    def test_ordinary_angle_brackets_survive(self) -> None:
        self.assertEqual(sanitize_hidden_flow_text("<hello> 2 < 3"), "<hello> 2 < 3")


class HiddenFlowStreamingTests(unittest.TestCase):
    @staticmethod
    def _single_chars(text: str) -> str:
        stream = HiddenFlowStreamFilter()
        result = "".join(stream.feed(char) for char in text)
        return result + stream.finish()

    def test_complete_tag_never_emits(self) -> None:
        tag = '<hidden_flow_control flow="x" action="hold"/>'
        stream = HiddenFlowStreamFilter()
        self.assertEqual(stream.feed("正文" + tag), "正文")
        self.assertEqual(stream.finish(), "")

    def test_single_character_complete_tag_never_emits(self) -> None:
        tag = '<hidden_flow_control flow="x" action="hold"/>'
        self.assertEqual(self._single_chars("正文" + tag), "正文")

    def test_partial_tag_at_eof_never_emits(self) -> None:
        self.assertEqual(self._single_chars("正文<hidden_flow_cont"), "正文")
        self.assertEqual(self._single_chars('正文<hidden_flow_control flow="x" act'), "正文")

    def test_ordinary_text_is_realtime_and_false_prefix_recovers(self) -> None:
        stream = HiddenFlowStreamFilter()
        self.assertEqual(stream.feed("你"), "你")
        self.assertEqual(stream.feed("好"), "好")
        self.assertEqual(stream.feed("<hello>"), "<hello>")
        self.assertEqual(stream.finish(), "")
        self.assertEqual(self._single_chars("<hidden_flow_controller>"), "<hidden_flow_controller>")

    def test_visible_text_and_tag_only_emit_visible_text(self) -> None:
        stream = HiddenFlowStreamFilter()
        outputs = [
            stream.feed(chunk)
            for chunk in ["ok", '<hidden_flow_control flow="x"', ' action="stop"/>', "!"]
        ]
        self.assertEqual("".join(outputs) + stream.finish(), "ok!")


class HiddenFlowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = normalize_flow_config(_raw_config())
        self.assertIsNotNone(self.config)

    def test_context_alone_does_not_start(self) -> None:
        state = start_flow(self.config, _control("demo-flow", keys="only-context"))
        self.assertFalse(state.active)

    def test_explicit_start_initializes_cycle_and_stage(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start", "a|b"), started_at="t0")
        self.assertEqual((state.active, state.cycle, state.stage, state.stage_turn), (True, 1, "s1", 1))
        self.assertEqual(state.context_keys, ("a", "b"))

    def test_minimum_and_hold_are_enforced(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start"))
        state = apply_flow_control(self.config, state)
        self.assertEqual((state.stage, state.stage_turn), ("s1", 2))
        state = apply_flow_control(self.config, state, _control("demo-flow", "hold"))
        self.assertEqual((state.stage, state.stage_turn), ("s1", 3))
        state = apply_flow_control(self.config, state)
        self.assertEqual((state.stage, state.stage_turn), ("s2", 1))

    def test_wrong_flow_is_a_noop_and_stop_is_immediate(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start"))
        wrong = apply_flow_control(self.config, state, _control("other", "stop"))
        self.assertEqual(wrong, state)
        self.assertFalse(apply_flow_control(self.config, state, _control("demo-flow", "stop")).active)

    def test_terminal_without_continue_ends_and_continue_starts_new_cycle(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start"))
        state = apply_flow_control(self.config, state)
        state = apply_flow_control(self.config, state)
        self.assertEqual(state.stage, "s2")
        state = apply_flow_control(self.config, state)
        self.assertEqual(state.stage, "s3")
        state.fixed_draws["cycle"] = "old"
        ended = apply_flow_control(self.config, state)
        self.assertFalse(ended.active)
        continued = apply_flow_control(self.config, state, _control("demo-flow", "continue"))
        self.assertEqual((continued.stage, continued.cycle, continued.stage_turn), ("s1", 2, 1))
        self.assertEqual(continued.fixed_draws, {})

    def test_boundary_and_disabled_config_fail_closed(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start"))
        self.assertFalse(_start_flow(self.config, _control("demo-flow", "start")).active)
        self.assertFalse(apply_flow_control(self.config, state, boundary_override=True).active)
        disabled = normalize_flow_config(_raw_config(enabled=False))
        self.assertFalse(start_flow(disabled, _control("demo-flow", "start")).active)
        self.assertFalse(start_flow(DEFAULT_FLOW_CONFIG, _control("demo-flow", "start")).active)

    def test_invalid_graph_and_schema_fail_closed(self) -> None:
        invalid = _raw_config()
        invalid["stages"][0]["nextStage"] = "missing"
        self.assertIsNone(normalize_flow_config(invalid))
        cyclic = _raw_config()
        cyclic["stages"][1]["nextStage"] = "s1"
        self.assertIsNone(normalize_flow_config(cyclic))
        terminal_missing = _raw_config()
        terminal_missing["stages"][2]["continueTarget"] = None
        self.assertIsNone(normalize_flow_config(terminal_missing))
        terminal_next = _raw_config()
        terminal_next["stages"][2]["nextStage"] = "s1"
        self.assertIsNone(normalize_flow_config(terminal_next))
        malformed_object = FlowConfig(
            flow_id="demo-flow",
            enabled=True,
            initial_stage="s1",
            stages=(),
        )
        self.assertIsNone(normalize_flow_config(malformed_object))
        self.assertIsNone(normalize_flow_config({"schemaVersion": 99}))


class HiddenFlowDrawTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = normalize_flow_config(_raw_config())
        self.assertIsNotNone(self.config)
        self.state = start_flow(self.config, _control("demo-flow", "start"), started_at="flow-t0")

    def test_turn_draw_is_deterministic_and_without_replacement(self) -> None:
        pool = self.config.pool("shared")
        first = draw_pool(pool, source_message_id="m1", flow_id="demo-flow", draw_index=0)
        retry = draw_pool(pool, source_message_id="m1", flow_id="demo-flow", draw_index=0)
        other = draw_pool(pool, source_message_id="m2", flow_id="demo-flow", draw_index=0)
        self.assertEqual(first, retry)
        self.assertNotEqual(first.draws[0].seed, other.draws[0].seed)
        self.assertEqual(len({item.entry_id for item in first.draws}), len(first.draws))
        self.assertEqual(
            draw_pool(
                PoolSpec("p", True, "turn", 1, (PoolEntry("e", "text"),)),
                source_message_id="a\x00b",
                flow_id="demo-flow",
            ).draws,
            (),
        )

    def test_cycle_draw_is_fixed_per_cycle_and_changes_with_cycle(self) -> None:
        pool = self.config.pool("cycle-pool")
        first = draw_pool(pool, flow_started_at="flow-t0", flow_id="demo-flow", cycle=1)
        retry = draw_pool(pool, flow_started_at="flow-t0", flow_id="demo-flow", cycle=1)
        next_cycle = draw_pool(pool, flow_started_at="flow-t0", flow_id="demo-flow", cycle=2)
        self.assertEqual(first, retry)
        self.assertNotEqual(first.draws[0].seed, next_cycle.draws[0].seed)

    def test_disabled_items_and_shared_pools_are_excluded_or_deduped(self) -> None:
        self.assertIsNone(self.config.pool("off-pool"))
        shared = self.config.pool("shared")
        self.assertEqual({item.entry_id for item in shared.entries}, {"a", "b"})
        result = draw_for_guide(self.config, self.state, source_message_id="m1", cue_keys=["KEY-A"])
        self.assertEqual(len([item for item in result.draws if item.pool_id == "shared"]), 2)

    def test_composite_draw_and_text_caps(self) -> None:
        pools = []
        stage_pools = []
        for index in range(20):
            pool_id = f"p{index}"
            stage_pools.append(pool_id)
            pools.append(
                {
                    "id": pool_id,
                    "enabled": True,
                    "drawMode": "turn",
                    "drawCount": 1,
                    "entries": [{"id": "e", "text": "x" * 1200}],
                }
            )
        raw = _raw_config()
        raw["stages"][0]["poolIds"] = stage_pools
        raw["pools"] = pools
        config = normalize_flow_config(raw)
        state = start_flow(config, _control("demo-flow", "start"))
        result = draw_for_guide(config, state, source_message_id="m1")
        self.assertLessEqual(len(result.draws), 12)
        self.assertLessEqual(sum(len(item.text) for item in result.draws), 8000)


class HiddenFlowSerializationTests(unittest.TestCase):
    def test_flow_state_and_applied_guide_round_trip(self) -> None:
        state = FlowState(
            active=True,
            flow_id="demo-flow",
            stage="s1",
            cycle=2,
            stage_turn=3,
            context_keys=("a", "b"),
            fixed_draws={"p": {"entryId": "e"}},
            started_at="t0",
        )
        decoded = FlowState.from_dict(json.loads(json.dumps(state.to_dict())))
        self.assertEqual(decoded.to_dict(), state.to_dict())
        guide = AppliedGuide(
            source_message_id="assistant-1",
            keys=("a",),
            draws=({"poolId": "p", "text": "anchor"},),
            created_at="t0",
            flow=state,
        )
        guide_decoded = AppliedGuide.from_dict(json.loads(json.dumps(guide.to_dict())))
        self.assertEqual(guide_decoded.to_dict(), guide.to_dict())

    def test_unknown_schema_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            FlowState.from_dict({"schemaVersion": 99})
        with self.assertRaises(ValueError):
            AppliedGuide.from_dict({"schemaVersion": 99})
        with self.assertRaises(ValueError):
            FlowState.from_dict({
                "schemaVersion": True,
                "active": False,
                "flowId": "",
                "stage": "",
                "cycle": 0,
                "stageTurn": 0,
                "contextKeys": [],
                "fixedDraws": {},
                "startedAt": "",
            })
        with self.assertRaises(ValueError):
            FlowState.from_dict({
                "schemaVersion": 1,
                "active": True,
                "flowId": "",
                "stage": "",
                "cycle": 0,
                "stageTurn": 0,
                "contextKeys": [],
                "fixedDraws": {},
                "startedAt": "",
            })


class HiddenFlowGuideAndPrivacyTests(unittest.TestCase):
    def test_private_guide_escapes_configured_text_and_is_bounded(self) -> None:
        raw = _raw_config()
        raw["pools"][0]["entries"][0]["text"] = "<unsafe> & value"
        config = normalize_flow_config(raw)
        state = start_flow(config, _control("demo-flow", "start"))
        guide = render_private_guide(
            config,
            state,
            draw_for_guide(config, state, source_message_id="m1"),
            protocol_hints=["hint <x>", "y"],
        )
        self.assertLessEqual(len(guide), 8000)
        self.assertIn("&lt;unsafe&gt;", guide)
        self.assertNotIn("<unsafe>", guide)
        self.assertTrue(guide.endswith("</hidden_flow_guidance>"))

    def test_new_modules_have_no_network_or_runtime_writer_imports(self) -> None:
        root = Path(__file__).resolve().parents[1] / "chat" / "hidden_flow"
        forbidden = {"request" + "s", "url" + "lib", "sub" + "process", "ran" + "dom"}
        sources = []
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            sources.append(source)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(alias.name.split(".")[0] not in forbidden for alias in node.names), path)
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden, path)
        joined = "\n".join(sources)
        self.assertNotIn("chat_" + "messages", joined)
        self.assertNotIn("memory" + ".write", joined)
        self.assertNotIn("hand" + "off", joined.lower())
        self.assertNotIn("request" + "s.", joined)
        self.assertNotIn("ran" + "dom.", joined)

    def test_importing_foundation_does_not_import_provider_or_writer_modules(self) -> None:
        self.assertNotIn("cc_resident", sys.modules)
        self.assertNotIn("gateway", sys.modules)


if __name__ == "__main__":
    unittest.main()
