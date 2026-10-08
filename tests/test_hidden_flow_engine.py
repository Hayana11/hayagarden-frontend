"""Focused contracts for the disabled-by-default hidden flow foundation."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

from chat.hidden_flow.config import DEFAULT_FLOW_CONFIG, normalize_flow_config
from chat.hidden_flow.control import parse_hidden_flow_control, sanitize_hidden_flow_text
from chat.hidden_flow.draw import draw_for_guide, draw_for_guide_with_state, draw_pool, render_private_guide
from chat.hidden_flow.engine import apply_flow_control as _apply_flow_control
from chat.hidden_flow.engine import start_flow as _start_flow
from chat.hidden_flow.stream_filter import HiddenFlowStreamFilter
from chat.hidden_flow.types import (
    AppliedGuide,
    FlowConfig,
    FlowControl,
    FlowState,
    DrawItem,
    DrawResult,
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


def _raw_config(*, enabled: bool = True, terminal: bool = True, terminal_min: int = 1) -> dict:
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
                "repeatMinTurns": 12,
                "nextStage": "s2",
                "holdable": True,
                "poolIds": ["shared", "missing"],
            },
            {
                "id": "s2",
                "enabled": True,
                "minTurns": 3,
                "nextStage": "s3",
                "poolIds": ["cycle-pool"],
            },
            {
                "id": "s3",
                "enabled": True,
                "minTurns": 1,
                "nextStage": "s4",
                "poolIds": [],
            },
            {
                "id": "s4",
                "enabled": True,
                "minTurns": terminal_min,
                "terminal": terminal,
                "poolIds": [],
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


class HiddenFlowParserTestsclass HiddenFlowParserTests(unittest.TestCase):
    def test_explicit_start_hold_continue_stop_parse(self) -> None:
        for action in ("start", "advance", "hold", "continue", "stop"):
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


    def test_private_block_visible_tails_across_chunk_boundaries(self) -> None:
        stream = HiddenFlowStreamFilter()
        self.assertEqual(
            stream.feed("before<hidden_flow_guidance>sec"),
            "before",
        )
        self.assertEqual(
            stream.feed("ret</hidden_flow_guidance>after"),
            "after",
        )
        stream = HiddenFlowStreamFilter()
        self.assertEqual(
            stream.feed("before<hidden_flow_guidance>secret</hidden_flow_guid"),
            "before",
        )
        self.assertEqual(
            stream.feed("ance>after"),
            "after",
        )

    def test_available_and_multiple_private_blocks_preserve_visible_tails(self) -> None:
        stream = HiddenFlowStreamFilter()
        self.assertEqual(
            stream.feed("A<available_hidden_flows><flow id=\"x\"/></available_hidden_flows>B"),
            "AB",
        )
        self.assertEqual(
            self._single_chars(
                "A<hidden_flow_guidance>x</hidden_flow_guidance>\n"
                "B<available_hidden_flows>y</available_hidden_flows>C"
            ),
            "A\nBC",
        )

    def test_thinking_filter_preserves_visible_tail_after_guidance(self) -> None:
        stream = HiddenFlowStreamFilter()
        self.assertEqual(
            stream.feed(
                "thought<hidden_flow_guidance>x</hidden_flow_guidance>visible thought"
            ),
            "thoughtvisible thought",
        )


    def test_guidance_and_activation_blocks_are_invisible(self) -> None:
        value = (
            "visible"
            '<hidden_flow_guidance flow="demo-flow">private</hidden_flow_guidance>'
            '<available_hidden_flows><flow id="demo-flow"/></available_hidden_flows>'
            "tail"
        )
        self.assertEqual(self._single_chars(value), "visibletail")

    def test_malformed_confirmed_private_block_drops_to_eof(self) -> None:
        stream = HiddenFlowStreamFilter()
        output = stream.feed(
            'visible<hidden_flow_guidance flow="demo-flow">private without close'
        )
        self.assertEqual(output, "visible")
        self.assertEqual(stream.finish(), "")


class HiddenFlowStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = normalize_flow_config(_raw_config())
        self.assertIsNotNone(self.config)

    def _at_stage_s2(self, config=None):
        config = config or self.config
        state = start_flow(config, _control("demo-flow", "start"))
        state = apply_flow_control(config, state)
        return apply_flow_control(config, state, _control("demo-flow", "advance"))

    def _at_stage_s3(self, config=None):
        config = config or self.config
        state = self._at_stage_s2(config)
        state = apply_flow_control(config, state)
        state = apply_flow_control(config, state, _control("demo-flow", "advance"))
        state = apply_flow_control(config, state)
        state = apply_flow_control(config, state)
        return apply_flow_control(config, state, _control("demo-flow", "advance"))

    def _at_stage_s4(self, config=None):
        config = config or self.config
        return apply_flow_control(
            config,
            self._at_stage_s3(config),
            _control("demo-flow", "advance"),
        )

    def test_start_requires_explicit_start_and_keys_only_does_not_start(self) -> None:
        self.assertFalse(start_flow(self.config, _control("demo-flow", keys="only-context")).active)
        state = start_flow(self.config, _control("demo-flow", "start", "a|b"), started_at="t0")
        self.assertEqual((state.active, state.cycle, state.stage, state.stage_turn), (True, 1, "s1", 1))
        self.assertEqual(state.context_keys, ("a", "b"))

    def test_minimum_is_lower_bound_and_advance_is_explicit(self) -> None:
        state = self._at_stage_s2()
        self.assertEqual((state.stage, state.stage_turn, state.cycle), ("s2", 1, 1))
        state = apply_flow_control(self.config, state)
        self.assertEqual((state.stage, state.stage_turn), ("s2", 2))
        state = apply_flow_control(self.config, state, _control("demo-flow", "advance"))
        self.assertEqual((state.stage, state.stage_turn), ("s2", 3))
        state = apply_flow_control(self.config, state)
        self.assertEqual((state.stage, state.stage_turn), ("s2", 4))
        state = apply_flow_control(self.config, state)
        self.assertEqual((state.stage, state.stage_turn), ("s2", 5))
        state = apply_flow_control(self.config, state, _control("demo-flow", "advance"))
        self.assertEqual((state.stage, state.stage_turn, state.cycle), ("s3", 1, 1))

    def test_hold_and_continue_are_legacy_stay_actions_after_minimum(self) -> None:
        state = self._at_stage_s2()
        state = apply_flow_control(self.config, state)
        state = apply_flow_control(self.config, state, _control("demo-flow", "hold", "hold-key"))
        self.assertEqual((state.stage, state.stage_turn, state.context_keys), ("s2", 3, ("hold-key",)))
        state = apply_flow_control(self.config, state, _control("demo-flow", "continue"))
        self.assertEqual((state.stage, state.stage_turn, state.cycle), ("s2", 4, 1))

    def test_active_keys_update_without_action_and_wrong_flow_does_not_update(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start", "initial"))
        state = apply_flow_control(self.config, state, _control("demo-flow", keys="latest"))
        self.assertEqual(state.context_keys, ("latest",))
        wrong = apply_flow_control(self.config, state, _control("other", keys="wrong"))
        self.assertEqual(wrong, state)
        restarted = apply_flow_control(self.config, state, _control("demo-flow", "start", "new"))
        self.assertEqual(restarted.context_keys, ("latest",))
        self.assertEqual((restarted.stage, restarted.stage_turn, restarted.cycle), ("s1", 3, 1))
        advanced = apply_flow_control(self.config, restarted, _control("demo-flow", "advance"))
        self.assertEqual((advanced.stage, advanced.context_keys, advanced.cycle), ("s2", ("latest",), 1))

    def test_stop_is_immediate_before_and_after_minimum(self) -> None:
        state = self._at_stage_s2()
        before = apply_flow_control(self.config, state, _control("demo-flow", "stop"))
        self.assertFalse(before.active)
        state = apply_flow_control(self.config, state)
        state = apply_flow_control(self.config, state)
        after = apply_flow_control(self.config, state, _control("demo-flow", "stop"))
        self.assertFalse(after.active)

    def test_terminal_minimum_one_ends_first_reply(self) -> None:
        state = self._at_stage_s4()
        ended = apply_flow_control(self.config, state)
        self.assertFalse(ended.active)

    def test_terminal_minimum_greater_than_one_ignores_actions_until_complete(self) -> None:
        config = normalize_flow_config(_raw_config(terminal_min=2))
        state = self._at_stage_s4(config)
        state = apply_flow_control(config, state, _control("demo-flow", "advance"))
        self.assertEqual((state.active, state.stage, state.stage_turn), (True, "s4", 2))
        for action in ("continue", "hold", "advance"):
            self.assertFalse(
                apply_flow_control(config, state, _control("demo-flow", action)).active
            )

    def test_final_non_terminal_stage_stays_until_explicit_advance(self) -> None:
        raw = _raw_config()
        raw["stages"][2]["nextStage"] = None
        raw["stages"][3]["enabled"] = False
        config = normalize_flow_config(raw)
        self.assertIsNotNone(config)
        state = self._at_stage_s3(config)
        state = apply_flow_control(config, state)
        self.assertEqual((state.active, state.stage, state.stage_turn), (True, "s3", 2))
        self.assertFalse(
            apply_flow_control(config, state, _control("demo-flow", "advance")).active
        )

    def test_fixed_draws_are_preserved_across_advance_and_cycle_never_increments(self) -> None:
        state = self._at_stage_s2()
        state = FlowState(
            active=True,
            flow_id=state.flow_id,
            stage=state.stage,
            cycle=1,
            stage_turn=3,
            fixed_draws={"cycle-pool": [{"poolId": "cycle-pool", "entryId": "c", "text": "C", "drawIndex": 0, "seed": "seed"}]},
            started_at=state.started_at,
        )
        advanced = apply_flow_control(self.config, state, _control("demo-flow", "advance"))
        self.assertEqual((advanced.stage, advanced.stage_turn, advanced.cycle), ("s3", 1, 1))
        self.assertEqual(advanced.fixed_draws, state.fixed_draws)

    def test_graph_accepts_ordinary_final_and_rejects_invalid_terminal_edges(self) -> None:
        ordinary = _raw_config()
        ordinary["stages"][2]["nextStage"] = None
        ordinary["stages"][3]["enabled"] = False
        self.assertIsNotNone(normalize_flow_config(ordinary))

        invalid_next = _raw_config()
        invalid_next["stages"][0]["nextStage"] = "missing"
        self.assertIsNone(normalize_flow_config(invalid_next))

        cyclic = _raw_config()
        cyclic["stages"][1]["nextStage"] = "s1"
        self.assertIsNone(normalize_flow_config(cyclic))

        terminal_next = _raw_config()
        terminal_next["stages"][3]["nextStage"] = "s1"
        self.assertIsNone(normalize_flow_config(terminal_next))

        terminal_target = _raw_config()
        terminal_target["stages"][3]["continueTarget"] = "s1"
        self.assertIsNone(normalize_flow_config(terminal_target))

        self.assertIsNone(normalize_flow_config({"schemaVersion": 99}))

    def test_boundary_and_disabled_config_fail_closed(self) -> None:
        state = start_flow(self.config, _control("demo-flow", "start"))
        self.assertFalse(_start_flow(self.config, _control("demo-flow", "start")).active)
        self.assertFalse(apply_flow_control(self.config, state, boundary_override=True).active)
        disabled = normalize_flow_config(_raw_config(enabled=False))
        self.assertFalse(start_flow(disabled, _control("demo-flow", "start")).active)
        self.assertFalse(start_flow(DEFAULT_FLOW_CONFIG, _control("demo-flow", "start")).active)

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

    def test_cycle_draw_is_written_reused_and_turn_draw_is_not_fixed(self) -> None:
        first, cached_state = draw_for_guide_with_state(
            self.config,
            self.state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        self.assertIn("cycle-pool", cached_state.fixed_draws)
        self.assertNotIn("shared", cached_state.fixed_draws)
        self.assertEqual(cached_state.fixed_draws["cycle-pool"][0]["text"], "C")
        retry, retry_state = draw_for_guide_with_state(
            self.config,
            cached_state,
            source_message_id="m2",
            pool_ids=["cycle-pool"],
        )
        self.assertEqual(
            [item for item in retry.draws if item.pool_id == "cycle-pool"],
            [item for item in first.draws if item.pool_id == "cycle-pool"],
        )
        self.assertEqual(retry_state.fixed_draws, cached_state.fixed_draws)

    def test_new_explicit_start_redraws_flow_fixed_pool(self) -> None:
        first, cached = draw_for_guide_with_state(
            self.config,
            self.state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        restarted = start_flow(self.config, _control("demo-flow", "start"), started_at="flow-t1")
        second, _ = draw_for_guide_with_state(
            self.config,
            restarted,
            source_message_id="m2",
            pool_ids=["cycle-pool"],
        )
        self.assertNotEqual(first.draws[0].seed, second.draws[0].seed)
        self.assertNotEqual(cached.started_at, restarted.started_at)

    def test_invalid_cycle_cache_is_rebuilt_from_current_config(self) -> None:
        bad_state = FlowState(
            active=True,
            flow_id="demo-flow",
            stage="s1",
            cycle=1,
            stage_turn=1,
            fixed_draws={
                "cycle-pool": [
                    {
                        "poolId": "cycle-pool",
                        "entryId": "missing",
                        "text": "untrusted",
                        "drawIndex": 0,
                        "seed": "bad",
                    }
                ]
            },
            started_at="flow-t0",
        )
        result, safe_state = draw_for_guide_with_state(
            self.config,
            bad_state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        self.assertNotIn("untrusted", result.texts)
        self.assertEqual(safe_state.fixed_draws["cycle-pool"][0]["entryId"], "c")

    def test_cycle_cache_revalidates_changed_entry_text(self) -> None:
        _, cached_state = draw_for_guide_with_state(
            self.config,
            self.state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        raw = _raw_config()
        raw["pools"][1]["entries"][0]["text"] = "changed"
        changed_config = normalize_flow_config(raw)
        result, changed_state = draw_for_guide_with_state(
            changed_config,
            cached_state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        self.assertIn("changed", result.texts)
        self.assertEqual(changed_state.fixed_draws["cycle-pool"][0]["text"], "changed")

    def test_cycle_cache_identity_is_independent_of_pool_order(self) -> None:
        first, cached_state = draw_for_guide_with_state(
            self.config,
            self.state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        raw = _raw_config()
        raw["stages"][0]["poolIds"] = ["cycle-pool", "shared"]
        reordered_config = normalize_flow_config(raw)
        retry, retry_state = draw_for_guide_with_state(
            reordered_config,
            cached_state,
            source_message_id="m2",
            pool_ids=["cycle-pool"],
        )
        first_cycle = [item for item in first.draws if item.pool_id == "cycle-pool"]
        retry_cycle = [item for item in retry.draws if item.pool_id == "cycle-pool"]
        self.assertEqual(retry_cycle, first_cycle)
        self.assertEqual(retry_state.fixed_draws, cached_state.fixed_draws)

    def test_cycle_cache_rejects_another_current_entry(self) -> None:
        raw = _raw_config()
        raw["pools"][1]["entries"].append({"id": "d", "text": "D"})
        config = normalize_flow_config(raw)
        state = start_flow(config, _control("demo-flow", "start"), started_at="flow-t0")
        first, cached_state = draw_for_guide_with_state(
            config,
            state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        cached_item = cached_state.fixed_draws["cycle-pool"][0]
        alternate = "d" if cached_item["entryId"] == "c" else "c"
        alternate_text = "D" if alternate == "d" else "C"
        tampered = FlowState(
            active=True,
            flow_id=cached_state.flow_id,
            stage=cached_state.stage,
            cycle=cached_state.cycle,
            stage_turn=cached_state.stage_turn,
            fixed_draws={
                "cycle-pool": [
                    dict(cached_item, entryId=alternate, text=alternate_text)
                ]
            },
            started_at=cached_state.started_at,
        )
        retry, safe_state = draw_for_guide_with_state(
            config,
            tampered,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        self.assertEqual(
            [item for item in retry.draws if item.pool_id == "cycle-pool"],
            [item for item in first.draws if item.pool_id == "cycle-pool"],
        )
        self.assertEqual(safe_state.fixed_draws, cached_state.fixed_draws)


class HiddenFlowSerializationTests(unittest.TestCase):
    def test_flow_state_and_applied_guide_round_trip(self) -> None:
        state = FlowState(
            active=True,
            flow_id="demo-flow",
            stage="s1",
            cycle=1,
            stage_turn=3,
            context_keys=("a", "b"),
            fixed_draws={
                "p": [
                    {
                        "poolId": "p",
                        "entryId": "e",
                        "text": "anchor",
                        "drawIndex": 0,
                        "seed": "seed",
                    }
                ]
            },
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

    def test_real_cycle_draw_state_round_trips_and_fixed_draw_caps_are_strict(self) -> None:
        config = normalize_flow_config(_raw_config())
        state = start_flow(config, _control("demo-flow", "start"), started_at="flow-t0")
        _, with_draw = draw_for_guide_with_state(
            config,
            state,
            source_message_id="m1",
            pool_ids=["cycle-pool"],
        )
        decoded = FlowState.from_dict(json.loads(json.dumps(with_draw.to_dict())))
        self.assertEqual(decoded.to_dict(), with_draw.to_dict())

        too_many = dict(with_draw.to_dict())
        too_many["fixedDraws"] = {
            "cycle-pool": [
                {
                    "poolId": "cycle-pool",
                    "entryId": str(index),
                    "text": "x",
                    "drawIndex": index,
                    "seed": "seed",
                }
                for index in range(13)
            ]
        }
        with self.assertRaises(ValueError):
            FlowState.from_dict(too_many)

        too_long = dict(with_draw.to_dict())
        too_long["fixedDraws"] = {
            "cycle-pool": [
                {
                    "poolId": "cycle-pool",
                    "entryId": "c",
                    "text": "x" * 8001,
                    "drawIndex": 0,
                    "seed": "seed",
                }
            ]
        }
        with self.assertRaises(ValueError):
            FlowState.from_dict(too_long)

        not_json_safe = dict(with_draw.to_dict())
        not_json_safe["fixedDraws"] = {
            "cycle-pool": [
                {
                    "poolId": "cycle-pool",
                    "entryId": "c",
                    "text": object(),
                    "drawIndex": 0,
                    "seed": "seed",
                }
            ]
        }
        with self.assertRaises(ValueError):
            FlowState.from_dict(not_json_safe)

        unknown_not_json_safe = dict(with_draw.to_dict())
        unknown_not_json_safe["fixedDraws"] = {
            "cycle-pool": [
                {
                    "poolId": "cycle-pool",
                    "entryId": "c",
                    "text": "C",
                    "drawIndex": 0,
                    "seed": "seed",
                    "extra": object(),
                }
            ]
        }
        with self.assertRaises(ValueError):
            FlowState.from_dict(unknown_not_json_safe)

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

    def test_private_guide_does_not_trust_missing_cached_entry(self) -> None:
        config = normalize_flow_config(_raw_config())
        state = start_flow(config, _control("demo-flow", "start"))
        guide = render_private_guide(
            config,
            state,
            DrawResult((DrawItem("cycle-pool", "missing", "untrusted", 0, "bad"),)),
        )
        self.assertNotIn("untrusted", guide)

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
