import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from flow_studio_editor import create_flow_studio_blueprint


def _seed_runtime(db_path: str) -> dict:
    raw = {
        "schemaVersion": 1,
        "flowId": "intimacy-v1",
        "enabled": True,
        "initialStage": "s1",
        "stages": [
            {
                "id": "s1",
                "enabled": True,
                "minTurns": 1,
                "repeatMinTurns": 1,
                "nextStage": "s2",
                "terminal": False,
                "continueTarget": None,
                "holdable": False,
                "poolIds": ["p1"],
            },
            {
                "id": "s2",
                "enabled": True,
                "minTurns": 2,
                "repeatMinTurns": 1,
                "nextStage": None,
                "terminal": True,
                "continueTarget": None,
                "holdable": False,
                "poolIds": ["p1"],
            },
            {
                "id": "disabled-stage",
                "enabled": False,
                "minTurns": 99,
                "repeatMinTurns": 99,
                "nextStage": None,
                "terminal": True,
                "continueTarget": None,
                "holdable": False,
                "poolIds": ["disabled-pool"],
            },
        ],
        "pools": [
            {
                "id": "p1",
                "enabled": True,
                "drawMode": "turn",
                "drawCount": 1,
                "entries": [
                    {"id": "e1", "text": "保留的启用条目"},
                    {"id": "e-disabled", "text": "保留的禁用条目", "enabled": False},
                ],
            },
            {
                "id": "disabled-pool",
                "enabled": False,
                "drawMode": "cycle",
                "drawCount": 5,
                "entries": [{"id": "hidden", "text": "不能被运行配置吞掉"}],
            },
        ],
        "cues": [
            {"id": "cue-1", "key": "雨夜", "enabled": True, "poolIds": ["p1"]},
            {"id": "cue-disabled", "key": "旧照片", "enabled": False, "poolIds": ["p1"]},
        ],
    }
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE hidden_flow_configs (
            flow_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            enabled INTEGER NOT NULL,
            config_json TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO hidden_flow_configs
        VALUES (?,?,?,?,?,?,?)
        """,
        (
            "intimacy-v1",
            1,
            1,
            json.dumps(raw, ensure_ascii=False),
            7,
            "2026-10-08 13:22:23",
            "2026-10-08 13:22:23",
        ),
    )
    connection.commit()
    connection.close()
    return raw


class FlowStudioEditorApiTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        self.db_path = handle.name
        self.runtime_raw = _seed_runtime(self.db_path)
        app = Flask(__name__)
        app.register_blueprint(
            create_flow_studio_blueprint(
                db_path=self.db_path,
                owner_guard=lambda _request: None,
            )
        )
        self.client = app.test_client()
        self.origin_patch = patch(
            "flow_studio_editor.same_origin_mutation_ok",
            return_value=True,
        )
        self.gate_patch = patch(
            "flow_studio_editor.config_store.get_bool",
            return_value=False,
        )
        self.origin_patch.start()
        self.gate_patch.start()
        self.addCleanup(self.origin_patch.stop)
        self.addCleanup(self.gate_patch.stop)

    def _get(self):
        response = self.client.get("/api/flow-studio/editor/intimacy-v1")
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _save(self, document, revision):
        return self.client.put(
            "/api/flow-studio/editor/intimacy-v1",
            json={"expectedRevision": revision, "document": document},
        )

    def test_existing_runtime_is_imported_without_dropping_disabled_editor_data(self):
        payload = self._get()
        self.assertEqual(payload["source"], "runtime-import")
        self.assertEqual(payload["runtime"]["schemaVersion"], 1)
        self.assertEqual(payload["runtime"]["version"], 7)
        self.assertFalse(payload["runtime"]["engineEnabled"])
        self.assertEqual(
            [stage["id"] for stage in payload["document"]["stages"]],
            ["s1", "s2", "disabled-stage"],
        )
        self.assertEqual(
            [entry["id"] for entry in payload["document"]["pools"][0]["entries"]],
            ["e1", "e-disabled"],
        )
        self.assertEqual(payload["document"]["cues"][1]["enabled"], False)

    def test_save_reload_and_disabled_entry_round_trip(self):
        first = self._get()
        document = first["document"]
        document["stages"][0]["name"] = "试探"
        document["pools"][0]["entries"][1]["enabled"] = True
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        saved_payload = saved.get_json()
        self.assertEqual(saved_payload["editorRevision"], 1)
        self.assertEqual(saved_payload["runtime"]["version"], 7)
        self.assertEqual(saved_payload["document"]["stages"][0]["nextStageId"], "s2")
        reloaded = self._get()
        self.assertEqual(reloaded["source"], "editor")
        self.assertEqual(reloaded["document"], saved_payload["document"])
        self.assertTrue(reloaded["document"]["pools"][0]["entries"][1]["enabled"])

        reloaded["document"]["pools"][0]["entries"][1]["enabled"] = False
        second = self._save(reloaded["document"], reloaded["editorRevision"])
        self.assertEqual(second.status_code, 200)
        again = self._get()
        self.assertFalse(again["document"]["pools"][0]["entries"][1]["enabled"])

        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 7)
        self.assertEqual(json.loads(row[1]), self.runtime_raw)

    def test_concurrent_revision_conflict_returns_server_document(self):
        first = self._get()
        document_a = first["document"]
        document_b = json.loads(json.dumps(document_a))
        document_a["stages"][0]["name"] = "先保存"
        document_b["stages"][0]["name"] = "后保存"
        self.assertEqual(self._save(document_a, 0).status_code, 200)
        conflict = self._save(document_b, 0)
        self.assertEqual(conflict.status_code, 409)
        body = conflict.get_json()
        self.assertEqual(body["code"], "EDITOR_REVISION_CONFLICT")
        self.assertEqual(body["currentRevision"], 1)
        self.assertEqual(body["currentDocument"]["stages"][0]["name"], "先保存")

    def test_reorder_disable_delete_and_reenable_round_trip(self):
        first = self._get()
        document = first["document"]
        document["stages"] = [
            document["stages"][1],
            document["stages"][0],
            document["stages"][2],
        ]
        document["pools"][1]["enabled"] = False
        document["pools"][0]["entries"] = [document["pools"][0]["entries"][0]]
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)

        reloaded = self._get()
        self.assertEqual(
            [stage["id"] for stage in reloaded["document"]["stages"]],
            ["s2", "s1", "disabled-stage"],
        )
        self.assertFalse(reloaded["document"]["pools"][1]["enabled"])
        self.assertEqual(
            [entry["id"] for entry in reloaded["document"]["pools"][0]["entries"]],
            ["e1"],
        )

        reloaded["document"]["pools"][1]["enabled"] = True
        reenabled = self._save(
            reloaded["document"],
            reloaded["editorRevision"],
        )
        self.assertEqual(reenabled.status_code, 200)
        again = self._get()
        self.assertTrue(again["document"]["pools"][1]["enabled"])

    def test_sequence_links_rematerialize_and_explicit_links_survive_reorder(self):
        first = self._get()
        document = first["document"]
        document["stages"][0]["nextStageMode"] = "sequence"
        document["stages"][0]["nextStageId"] = "disabled-stage"
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        saved_document = saved.get_json()["document"]
        first_stage = next(stage for stage in saved_document["stages"] if stage["id"] == "s1")
        self.assertEqual(first_stage["nextStageMode"], "sequence")
        self.assertEqual(first_stage["nextStageId"], "s2")

        reloaded = self._get()
        stages = reloaded["document"]["stages"]
        stage_one = next(stage for stage in stages if stage["id"] == "s1")
        stage_two = next(stage for stage in stages if stage["id"] == "s2")
        disabled = next(stage for stage in stages if stage["id"] == "disabled-stage")
        reloaded["document"]["stages"] = [stage_two, stage_one, disabled]
        reordered = self._save(reloaded["document"], reloaded["editorRevision"])
        self.assertEqual(reordered.status_code, 200)
        reordered_stage = next(
            stage for stage in reordered.get_json()["document"]["stages"]
            if stage["id"] == "s1"
        )
        self.assertEqual(reordered_stage["nextStageMode"], "sequence")
        self.assertIsNone(reordered_stage["nextStageId"])

        reloaded_again = self._get()
        stage_one = next(
            stage for stage in reloaded_again["document"]["stages"]
            if stage["id"] == "s1"
        )
        stage_one["nextStageMode"] = "explicit"
        stage_one["nextStageId"] = "s2"
        explicit = self._save(reloaded_again["document"], reloaded_again["editorRevision"])
        self.assertEqual(explicit.status_code, 200)
        explicit_stage = next(
            stage for stage in explicit.get_json()["document"]["stages"]
            if stage["id"] == "s1"
        )
        self.assertEqual(explicit_stage["nextStageMode"], "explicit")
        self.assertEqual(explicit_stage["nextStageId"], "s2")

    def test_validate_maps_modes_and_reports_runtime_limits_without_writing(self):
        document = self._get()["document"]
        document["pools"][0]["mode"] = "perStage"
        document["stages"][0]["minTurns"] = 99
        response = self.client.post(
            "/api/flow-studio/editor/intimacy-v1/validate",
            json={"document": document},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body["valid"])
        self.assertIn("stage s1: minTurns must be between 1 and 12", body["errors"])
        self.assertEqual(body["runtime"]["config"]["pools"][0]["drawMode"], "cycle")
        self.assertTrue(any("enabled" in item for item in body["unsupportedFields"]))

        connection = sqlite3.connect(self.db_path)
        count = connection.execute(
            "SELECT COUNT(*) FROM flow_studio_editor_documents"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(count, 0)

    def test_initial_stage_follows_enabled_order_and_terminal_validation(self):
        first = self._get()
        document = first["document"]
        stage_one = next(stage for stage in document["stages"] if stage["id"] == "s1")
        stage_two = next(stage for stage in document["stages"] if stage["id"] == "s2")
        disabled = next(stage for stage in document["stages"] if stage["id"] == "disabled-stage")
        document["stages"] = [stage_two, stage_one, disabled]
        reordered = self._save(document, first["editorRevision"])
        self.assertEqual(reordered.status_code, 200)
        self.assertEqual(reordered.get_json()["document"]["initialStage"], "s2")

        reloaded = self._get()
        stages = reloaded["document"]["stages"]
        stage_one = next(stage for stage in stages if stage["id"] == "s1")
        stage_two = next(stage for stage in stages if stage["id"] == "s2")
        disabled = next(stage for stage in stages if stage["id"] == "disabled-stage")
        stage_two["enabled"] = False
        reloaded["document"]["stages"] = [stage_two, stage_one, disabled]
        disabled_start = self._save(reloaded["document"], reloaded["editorRevision"])
        self.assertEqual(disabled_start.status_code, 200)
        self.assertEqual(disabled_start.get_json()["document"]["initialStage"], "s1")

        reloaded = self._get()
        stages = reloaded["document"]["stages"]
        stage_one = next(stage for stage in stages if stage["id"] == "s1")
        stage_two = next(stage for stage in stages if stage["id"] == "s2")
        disabled = next(stage for stage in stages if stage["id"] == "disabled-stage")
        stage_two["enabled"] = True
        reloaded["document"]["stages"] = [stage_one, stage_two, disabled]
        reenabled = self._save(reloaded["document"], reloaded["editorRevision"])
        self.assertEqual(reenabled.status_code, 200)
        self.assertEqual(reenabled.get_json()["document"]["initialStage"], "s1")

        valid = self.client.post(
            "/api/flow-studio/editor/intimacy-v1/validate",
            json={"document": reenabled.get_json()["document"]},
        )
        self.assertEqual(valid.status_code, 200)
        self.assertTrue(valid.get_json()["valid"])

        invalid_document = json.loads(json.dumps(reenabled.get_json()["document"]))
        invalid_document["stages"] = [
            invalid_document["stages"][1],
            invalid_document["stages"][0],
            invalid_document["stages"][2],
        ]
        invalid = self.client.post(
            "/api/flow-studio/editor/intimacy-v1/validate",
            json={"document": invalid_document},
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertFalse(invalid.get_json()["valid"])
        self.assertIn("final enabled stage must be marked as terminal", invalid.get_json()["errors"])

    def test_malformed_document_and_unknown_flow_are_rejected(self):
        malformed = self.client.put(
            "/api/flow-studio/editor/intimacy-v1",
            json={"expectedRevision": 0, "document": {"flowId": "wrong"}},
        )
        self.assertEqual(malformed.status_code, 422)
        missing = self.client.get("/api/flow-studio/editor/missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["code"], "FLOW_NOT_FOUND")

    def test_owner_auth_error_is_explicit_for_get_put_and_validate(self):
        from moments_auth import OwnerAuthError

        app = Flask(__name__)

        def reject(_request):
            raise OwnerAuthError("unauthorized", 401)

        app.register_blueprint(
            create_flow_studio_blueprint(
                db_path=self.db_path,
                owner_guard=reject,
            )
        )
        client = app.test_client()
        responses = [
            client.get("/api/flow-studio/editor/intimacy-v1"),
            client.put(
                "/api/flow-studio/editor/intimacy-v1",
                json={"expectedRevision": 0, "document": {}},
            ),
            client.post(
                "/api/flow-studio/editor/intimacy-v1/validate",
                json={"document": {}},
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["code"], "OWNER_AUTH_REQUIRED")
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            self.assertNotIn("document", response.get_json())
            self.assertNotIn("runtime", response.get_json())


if __name__ == "__main__":
    unittest.main()
