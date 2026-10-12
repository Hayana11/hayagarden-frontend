import copy
import unittest
from pathlib import Path

from chat.hidden_flow.config import normalize_flow_config
from chat.hidden_flow.draw import draw_for_guide
from chat.hidden_flow.types import FlowState
from flow_studio_editor import compile_editor_document


def _document() -> dict:
    return {
        "schemaVersion": 1,
        "flowId": "disabled-pool-flow",
        "enabled": True,
        "initialStage": "stage-1",
        "stages": [
            {
                "id": "stage-1",
                "name": "试探",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStageId": None,
                "nextStageMode": "sequence",
                "terminal": True,
                "continueTarget": None,
                "terminalWithoutContinue": False,
                "holdable": False,
                "poolIds": ["enabled-pool", "disabled-pool"],
                "guidancePrompt": "保持当前阶段。",
            }
        ],
        "pools": [
            {
                "id": "enabled-pool",
                "name": "启用池",
                "enabled": True,
                "mode": "perTurn",
                "count": 1,
                "entries": [{"id": "enabled-entry", "text": "enabled", "enabled": True}],
            },
            {
                "id": "disabled-pool",
                "name": "停用池",
                "enabled": False,
                "mode": "perTurn",
                "count": 1,
                "entries": [{"id": "disabled-entry", "text": "disabled", "enabled": True}],
            },
        ],
        "cues": [
            {
                "id": "cue-disabled",
                "key": "停用情境",
                "kind": "氛围",
                "enabled": True,
                "poolIds": ["disabled-pool"],
            }
        ],
    }


class DisabledPoolReferenceTests(unittest.TestCase):
    def test_enabled_and_disabled_stage_refs_compile_and_draw_only_enabled(self) -> None:
        compiled, errors, unsupported = compile_editor_document(_document())
        self.assertEqual(errors, [])
        self.assertNotIn("pools[disabled-pool].enabled", unsupported)
        self.assertIsNotNone(compiled)
        assert compiled is not None
        stage = compiled["stages"][0]
        self.assertTrue(stage["enabled"])
        self.assertEqual(stage["poolIds"], ["enabled-pool", "disabled-pool"])
        self.assertEqual(
            [(pool["id"], pool["enabled"]) for pool in compiled["pools"]],
            [("enabled-pool", True), ("disabled-pool", False)],
        )

        config = normalize_flow_config(compiled)
        self.assertIsNotNone(config)
        assert config is not None
        result = draw_for_guide(
            config,
            FlowState(active=True, flow_id=config.flow_id, stage="stage-1", cycle=1, stage_turn=1),
            source_message_id="message-1",
            cue_keys=["停用情境"],
        )
        self.assertEqual([item.pool_id for item in result.draws], ["enabled-pool"])

    def test_disabled_cue_ref_is_valid_and_reenable_restores_without_rebinding(self) -> None:
        document = _document()
        compiled, errors, _ = compile_editor_document(document)
        self.assertEqual(errors, [])
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertEqual(compiled["cues"][0]["poolIds"], ["disabled-pool"])
        self.assertEqual(compiled["stages"][0]["guidancePrompt"], "保持当前阶段。")

        reenabled = copy.deepcopy(document)
        reenabled["pools"][1]["enabled"] = True
        recompiled, errors, _ = compile_editor_document(reenabled)
        self.assertEqual(errors, [])
        self.assertIsNotNone(recompiled)
        assert recompiled is not None
        self.assertEqual(recompiled["stages"][0]["poolIds"], ["enabled-pool", "disabled-pool"])
        self.assertEqual(recompiled["cues"][0]["poolIds"], ["disabled-pool"])
        config = normalize_flow_config(recompiled)
        self.assertIsNotNone(config)
        assert config is not None
        result = draw_for_guide(
            config,
            FlowState(active=True, flow_id=config.flow_id, stage="stage-1", cycle=1, stage_turn=1),
            source_message_id="message-1",
            cue_keys=["停用情境"],
        )
        self.assertEqual({item.pool_id for item in result.draws}, {"enabled-pool", "disabled-pool"})

    def test_missing_stage_and_cue_refs_fail_with_specific_errors(self) -> None:
        document = _document()
        document["stages"][0]["poolIds"] = ["missing-stage-pool"]
        document["cues"][0]["poolIds"] = ["missing-cue-pool"]
        _, errors, _ = compile_editor_document(document)
        self.assertIn("stage stage-1: references a missing pool", errors)
        self.assertIn("cue cue-disabled: references a missing pool", errors)

    def test_all_disabled_pools_keep_stage_and_cue_valid_without_anchors(self) -> None:
        document = _document()
        document["pools"][0]["enabled"] = False
        compiled, errors, _ = compile_editor_document(document)
        self.assertEqual(errors, [])
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertTrue(compiled["stages"][0]["enabled"])
        self.assertEqual(compiled["stages"][0]["guidancePrompt"], "保持当前阶段。")
        config = normalize_flow_config(compiled)
        self.assertIsNotNone(config)
        assert config is not None
        result = draw_for_guide(
            config,
            FlowState(active=True, flow_id=config.flow_id, stage="stage-1", cycle=1, stage_turn=1),
            source_message_id="message-1",
            cue_keys=["停用情境"],
        )
        self.assertEqual(result.draws, ())

    def test_pool_delete_keeps_existing_frontend_reference_cleanup(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "app/src/screens/FlowStudioWorkspace.tsx").read_text()
        self.assertIn(
            "next.stages.forEach((stage) => { stage.poolIds = stage.poolIds.filter((poolId) => poolId !== id); })",
            source,
        )
        self.assertIn(
            "next.cues.forEach((cue) => { cue.poolIds = cue.poolIds.filter((poolId) => poolId !== id); })",
            source,
        )


if __name__ == "__main__":
    unittest.main()
