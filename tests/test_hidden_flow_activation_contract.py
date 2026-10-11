"""Activation-contract tests independent of the optional observability layer."""

from __future__ import annotations

import unittest

from chat.hidden_flow.control import parse_hidden_flow_control
from chat.hidden_flow.runtime import (
    HiddenFlowTurnPlan,
    build_activation_block,
    build_pending_snapshot,
    propose_transition,
)
from chat.hidden_flow.types import (
    CueSpec,
    FlowConfig,
    FlowControl,
    PoolEntry,
    PoolSpec,
    StageSpec,
)


def _config() -> FlowConfig:
    return FlowConfig(
        flow_id="activation-test",
        enabled=True,
        initial_stage="opening",
        stages=(
            StageSpec(
                stage_id="opening",
                min_turns=2,
                pool_ids=("pool",),
            ),
        ),
        cues=(
            CueSpec(
                cue_id="cue",
                key="context",
                pool_ids=("pool",),
            ),
        ),
        pools=(
            PoolSpec(
                pool_id="pool",
                entries=(PoolEntry(entry_id="entry", text="short-anchor"),),
            ),
        ),
    )


class ActivationContractTests(unittest.TestCase):
    def test_activation_block_contains_contract_and_stays_within_cap(self) -> None:
        configs = [
            (FlowConfig(
                flow_id=f"flow-{index}",
                enabled=True,
                initial_stage="opening",
                stages=(StageSpec(stage_id="opening"),),
                cues=(CueSpec(cue_id="cue", key=f"context-{index}"),),
            ), 1)
            for index in range(8)
        ]

        block = build_activation_block(configs)

        self.assertTrue(block)
        self.assertLessEqual(len(block), 4000)
        self.assertIn(
            "actively entered the intended intimate behavior scene",
            block,
        )
        self.assertIn("do not require a fixed keyword", block)
        self.assertIn("never start in anticipation", block)
        self.assertIn("A key-only tag is never a start.", block)
        self.assertIn("not a formal stage turn", block)
        self.assertIn("optional creative anchors", block)
        self.assertIn("ordinary duties", block)
        self.assertIn("changed intent", block)
        for index in range(8):
            self.assertIn(f'flow id="flow-{index}"', block)

    def test_context_or_keyword_alone_cannot_create_start(self) -> None:
        self.assertIsNone(
            parse_hidden_flow_control("A warm room, a bath, and a suggestive word.")
        )
        key_only = parse_hidden_flow_control(
            '<hidden_flow_control flow="activation-test" keys="context"/>'
        )
        self.assertIsNotNone(key_only)
        self.assertIsNone(key_only.action)

        plan = HiddenFlowTurnPlan(
            enabled=True,
            eligible=True,
            chat_id="chat",
            user_message_id=10,
            runtime_version_before=1,
            config_version=1,
            available_configs=((_config(), 1),),
        )
        self.assertIsNone(propose_transition(plan, key_only))

    def test_explicit_start_is_valid_and_activation_reply_is_not_consumed(self) -> None:
        raw_control = (
            '<hidden_flow_control flow="activation-test" action="start" '
            'keys="context"/>'
        )
        control = parse_hidden_flow_control(raw_control)
        self.assertIsNotNone(control)
        self.assertEqual(control.action, "start")
        self.assertEqual(control.flow_id, "activation-test")

        config = _config()
        plan = HiddenFlowTurnPlan(
            enabled=True,
            eligible=True,
            chat_id="chat",
            user_message_id=10,
            runtime_version_before=1,
            config_version=1,
            available_configs=((config, 1),),
        )
        transition = propose_transition(plan, control)
        self.assertIsNotNone(transition)
        self.assertTrue(transition.state_after.active)
        self.assertEqual(transition.state_after.stage, "opening")
        self.assertEqual(transition.state_after.stage_turn, 1)

        snapshot = build_pending_snapshot(
            plan,
            transition,
            assistant_message_id=11,
        )
        self.assertIsNone(snapshot["applied_guide"])
        self.assertEqual(snapshot["next_guide"]["status"], "pending")
        self.assertEqual(snapshot["next_guide"]["sourceMessageId"], "11")
        self.assertEqual(snapshot["state_after"]["stageTurn"], 1)


if __name__ == "__main__":
    unittest.main()
