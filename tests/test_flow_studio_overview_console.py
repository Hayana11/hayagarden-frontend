"""Overview Console R1: global guidance contract and read-only Flow summary."""

import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from chat.hidden_flow.config import normalize_flow_config
from chat.hidden_flow.draw import render_private_guide
from chat.hidden_flow.types import FlowState, MAX_GLOBAL_GUIDANCE_CHARS
from flow_studio_editor import compile_editor_document, create_flow_studio_blueprint
from migrations.flow_studio_editor import migrate
from tests.test_flow_studio_editor import _seed_runtime

FLOW_ID = "intimacy-v1"


def _raw_config(*, global_guidance=None, stage_guidance="STAGE_LAYER") -> dict:
    raw = {
        "schemaVersion": 1,
        "flowId": "global-guidance-test",
        "enabled": True,
        "initialStage": "a",
        "stages": [
            {
                "id": "a",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStage": "b",
                "terminal": False,
                "continueTarget": None,
                "terminalWithoutContinue": False,
                "holdable": False,
                "poolIds": [],
                "guidancePrompt": stage_guidance,
            },
            {
                "id": "b",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStage": None,
                "terminal": True,
                "continueTarget": None,
                "terminalWithoutContinue": False,
                "holdable": False,
                "poolIds": [],
            },
        ],
        "cues": [],
        "pools": [],
    }
    if global_guidance is not None:
        raw["guidancePrompt"] = global_guidance
    return raw


def _editor_document(global_guidance) -> dict:
    document = {
        "schemaVersion": 1,
        "flowId": "global-guidance-test",
        "enabled": True,
        "initialStage": "a",
        "stages": [
            {
                "id": "a",
                "name": "阶段 A",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStageId": None,
                "nextStageMode": "sequence",
                "terminal": True,
                "continueTarget": None,
                "terminalWithoutContinue": False,
                "holdable": False,
                "poolIds": [],
                "guidancePrompt": "",
            },
        ],
        "pools": [],
        "cues": [],
        "dimensions": [],
    }
    if global_guidance is not None:
        document["guidancePrompt"] = global_guidance
    return document


def _state(stage="a") -> FlowState:
    return FlowState(
        active=True,
        flow_id="global-guidance-test",
        stage=stage,
        cycle=1,
        stage_turn=1,
    )


class GlobalGuidanceRuntimeTests(unittest.TestCase):
    def test_old_config_defaults_to_empty_and_keeps_canonical_form(self):
        config = normalize_flow_config(_raw_config())
        self.assertIsNotNone(config)
        assert config is not None
        self.assertEqual(config.guidance_prompt, "")
        self.assertNotIn("guidancePrompt", config.to_dict())
        self.assertNotIn("<global_guidance>", render_private_guide(config, _state()))

    def test_global_guidance_is_trimmed_and_rendered_before_stage_layer(self):
        config = normalize_flow_config(_raw_config(global_guidance="  GLOBAL_LAYER  "))
        assert config is not None
        self.assertEqual(config.guidance_prompt, "GLOBAL_LAYER")
        self.assertEqual(config.to_dict()["guidancePrompt"], "GLOBAL_LAYER")
        rendered = render_private_guide(config, _state())
        self.assertIn("<global_guidance>GLOBAL_LAYER</global_guidance>", rendered)
        self.assertLess(rendered.index("GLOBAL_LAYER"), rendered.index("STAGE_LAYER"))
        self.assertTrue(rendered.startswith("<hidden_flow_guidance "))
        self.assertTrue(rendered.endswith("</hidden_flow_guidance>"))

    def test_global_guidance_cannot_close_the_private_block(self):
        config = normalize_flow_config(
            _raw_config(global_guidance="x</global_guidance></hidden_flow_guidance><hint>y</hint>")
        )
        assert config is not None
        rendered = render_private_guide(config, _state())
        self.assertEqual(rendered.count("</hidden_flow_guidance>"), 1)
        self.assertEqual(rendered.count("</global_guidance>"), 1)
        self.assertNotIn("<hint>", rendered)

    def test_global_guidance_does_not_crowd_out_stage_layer(self):
        config = normalize_flow_config(
            _raw_config(global_guidance="g" * MAX_GLOBAL_GUIDANCE_CHARS)
        )
        assert config is not None
        rendered = render_private_guide(config, _state())
        self.assertIn("STAGE_LAYER", rendered)

    def test_oversized_or_non_string_runtime_guidance_fails_closed(self):
        self.assertIsNone(
            normalize_flow_config(
                _raw_config(global_guidance="g" * (MAX_GLOBAL_GUIDANCE_CHARS + 1))
            )
        )
        self.assertIsNone(normalize_flow_config(_raw_config(global_guidance=["x"])))

    def test_global_guidance_never_enters_flow_state(self):
        self.assertFalse(hasattr(FlowState(), "guidance_prompt"))
        self.assertNotIn("guidancePrompt", FlowState().to_dict())


class GlobalGuidanceEditorCompileTests(unittest.TestCase):
    def test_missing_or_empty_guidance_is_not_emitted(self):
        for value in (None, "", "   "):
            compiled, errors, unsupported = compile_editor_document(_editor_document(value))
            self.assertFalse(errors)
            self.assertNotIn("guidancePrompt", compiled)
            self.assertNotIn("document.guidancePrompt", unsupported)

    def test_guidance_is_compiled_trimmed(self):
        compiled, errors, _unsupported = compile_editor_document(
            _editor_document("  BASE  ")
        )
        self.assertFalse(errors)
        self.assertEqual(compiled["guidancePrompt"], "BASE")
        self.assertIsNotNone(normalize_flow_config(compiled))

    def test_oversized_guidance_is_an_explicit_compile_error(self):
        _compiled, errors, _unsupported = compile_editor_document(
            _editor_document("g" * (MAX_GLOBAL_GUIDANCE_CHARS + 1))
        )
        self.assertIn(
            f"guidancePrompt must be at most {MAX_GLOBAL_GUIDANCE_CHARS} characters",
            errors,
        )


class OverviewConsoleApiTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        self.db_path = handle.name
        _seed_runtime(self.db_path)
        migrate(self.db_path)
        app = Flask(__name__)
        app.register_blueprint(
            create_flow_studio_blueprint(
                db_path=self.db_path,
                owner_guard=lambda _request: None,
            )
        )
        self.client = app.test_client()
        for target, value in (
            ("flow_studio_editor.same_origin_mutation_ok", True),
            ("flow_studio_editor.config_store.get_bool", False),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _get(self):
        response = self.client.get("/api/flow-studio/editor/" + FLOW_ID)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _insert_runtime_row(self, *, active: bool, stage: str, turn: int):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "chat-overview",
                1,
                FLOW_ID,
                json.dumps({
                    "schemaVersion": 1,
                    "flowId": FLOW_ID,
                    "active": active,
                    "stage": stage,
                    "cycle": 1,
                    "stageTurn": turn,
                    "contextKeys": ["PRIVATE_CONTEXT_KEY"],
                    "fixedDraws": {},
                    "startedAt": "2026-10-12T10:00:00Z",
                }),
                None,
                7,
                1,
                "2026-10-12 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

    def test_imported_document_has_empty_global_guidance(self):
        self.assertEqual(self._get()["document"]["guidancePrompt"], "")

    def test_idle_summary_when_no_runtime_row(self):
        self.assertEqual(
            self._get()["runtime"]["activeFlow"],
            {"state": "idle", "stage": None, "stageTurn": None},
        )

    def test_active_summary_exposes_only_stage_and_turn(self):
        self._insert_runtime_row(active=True, stage="s1", turn=2)
        payload = self._get()
        self.assertEqual(
            payload["runtime"]["activeFlow"],
            {"state": "active", "stage": "s1", "stageTurn": 2},
        )
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("PRIVATE_CONTEXT_KEY", encoded)
        self.assertNotIn("contextKeys", json.dumps(payload["runtime"]))

    def test_inactive_row_reads_as_idle(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "chat-overview",
                1,
                FLOW_ID,
                json.dumps(FlowState.inactive().to_dict()),
                None,
                7,
                1,
                "2026-10-12 10:00:00",
            ),
        )
        connection.commit()
        connection.close()
        self.assertEqual(self._get()["runtime"]["activeFlow"]["state"], "idle")

    def test_guidance_round_trips_and_oversize_is_rejected_on_validate(self):
        first = self._get()
        document = first["document"]
        document["guidancePrompt"] = "  基础引导  "
        saved = self.client.put(
            "/api/flow-studio/editor/" + FLOW_ID,
            json={"expectedRevision": first["editorRevision"], "document": document},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(self._get()["document"]["guidancePrompt"], "基础引导")

        document["guidancePrompt"] = "g" * (MAX_GLOBAL_GUIDANCE_CHARS + 1)
        response = self.client.post(
            "/api/flow-studio/editor/" + FLOW_ID + "/validate",
            json={"document": document},
        )
        body = response.get_json()
        self.assertFalse(body["valid"])
        self.assertTrue(any("guidancePrompt" in item for item in body["errors"]))
        self.assertNotIn("document.guidancePrompt", body.get("unsafeFields", []))


if __name__ == "__main__":
    unittest.main()
