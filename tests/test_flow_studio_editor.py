import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

import flow_studio_editor
from flow_studio_editor import create_flow_studio_blueprint
from migrations.flow_studio_editor import MigrationError, migrate


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
        CREATE TABLE hidden_flow_runtime (
            chat_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            flow_id TEXT,
            state_json TEXT,
            pending_guide_json TEXT,
            config_version INTEGER,
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE daily_resident_turn_leases (
            context_id INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            lease_owner TEXT NOT NULL,
            request_message_id INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE hidden_flow_message_snapshots (
            assistant_message_id INTEGER PRIMARY KEY,
            user_message_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            flow_id TEXT,
            status TEXT NOT NULL,
            runtime_version_before INTEGER NOT NULL,
            config_version INTEGER,
            applied_guide_json TEXT,
            control_json TEXT,
            state_before_json TEXT NOT NULL,
            state_after_json TEXT NOT NULL,
            next_guide_json TEXT,
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
        migrate(self.db_path)
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

    def _publish(self, document, revision, runtime_version=7):
        return self.client.post(
            "/api/flow-studio/editor/intimacy-v1/publish",
            json={
                "expectedRevision": revision,
                "expectedRuntimeVersion": runtime_version,
                "document": document,
            },
        )

    def _publishable_document(self, document):
        result = json.loads(json.dumps(document))
        result["stages"] = [
            stage for stage in result["stages"] if stage.get("enabled") is True
        ]
        result["pools"] = [
            pool for pool in result["pools"] if pool.get("enabled") is True
        ]
        for pool in result["pools"]:
            pool["entries"] = [
                entry for entry in pool["entries"] if entry.get("enabled") is not False
            ]
        result["cues"] = [
            cue for cue in result["cues"] if cue.get("enabled") is not False
        ]
        return result

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

    def test_global_guidance_save_conflict_publish_round_trip(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["guidancePrompt"] = "  ABC  "
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        saved_payload = saved.get_json()
        self.assertEqual(saved_payload["document"]["guidancePrompt"], "ABC")

        reloaded = self._get()
        self.assertEqual(reloaded["document"]["guidancePrompt"], "ABC")

        stale = json.loads(json.dumps(first["document"]))
        stale["guidancePrompt"] = "STALE"
        conflict = self._save(stale, first["editorRevision"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(
            conflict.get_json()["currentDocument"]["guidancePrompt"],
            "ABC",
        )

        published = self._publish(
            saved_payload["document"],
            saved_payload["editorRevision"],
            7,
        )
        self.assertEqual(published.status_code, 200)
        runtime_config = json.loads(
            sqlite3.connect(self.db_path).execute(
                "SELECT config_json FROM hidden_flow_configs WHERE flow_id=?",
                ("intimacy-v1",),
            ).fetchone()[0]
        )
        self.assertEqual(runtime_config["guidancePrompt"], "ABC")

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

    def test_publish_is_explicit_and_updates_runtime_only_after_saved_draft(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["runtime"]["version"], 7)

        connection = sqlite3.connect(self.db_path)
        before = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(before[0], 7)

        published = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(published.status_code, 200)
        body = published.get_json()
        self.assertTrue(body["runtime"]["published"])
        self.assertEqual(body["runtime"]["version"], 8)
        self.assertEqual(body["editorRevision"], 1)

        connection = sqlite3.connect(self.db_path)
        after = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(after[0], 8)
        self.assertEqual(json.loads(after[1])["stages"][0]["minTurns"], 2)

    def test_publish_validation_failure_does_not_change_runtime(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 99
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        response = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["code"], "PUBLISH_VALIDATION_FAILED")
        self.assertIn("stage s1: minTurns must be between 1 and 12", response.get_json()["errors"])

        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 7)
        self.assertEqual(json.loads(row[1]), self.runtime_raw)

    def test_publish_rejects_editor_and_runtime_version_conflicts(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)

        editor_conflict = self._publish(saved.get_json()["document"], 0, 7)
        self.assertEqual(editor_conflict.status_code, 409)
        self.assertEqual(editor_conflict.get_json()["code"], "EDITOR_REVISION_CONFLICT")
        self.assertEqual(editor_conflict.get_json()["currentRevision"], 1)

        runtime_conflict = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            6,
        )
        self.assertEqual(runtime_conflict.status_code, 409)
        self.assertEqual(runtime_conflict.get_json()["code"], "RUNTIME_VERSION_CONFLICT")
        self.assertEqual(runtime_conflict.get_json()["currentRuntimeVersion"], 7)

    def test_deferred_apply_uses_latest_saved_revision_after_active_flow_ends(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "chat-active",
                1,
                "intimacy-v1",
                json.dumps({
                    "schemaVersion": 1,
                    "flowId": "intimacy-v1",
                    "active": True,
                    "stage": "s1",
                    "cycle": 1,
                    "stageTurn": 1,
                    "contextKeys": [],
                    "fixedDraws": {},
                    "startedAt": "2026-10-11T10:00:00Z",
                }),
                None,
                7,
                1,
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        first = self._get()
        latest = self._publishable_document(first["document"])
        latest["guidancePrompt"] = "DEFERRED_GLOBAL"
        latest["stages"][0]["minTurns"] = 2
        saved = self._save(latest, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        latest = saved.get_json()["document"]
        latest["stages"][0]["minTurns"] = 3
        saved_again = self._save(latest, saved.get_json()["editorRevision"])
        self.assertEqual(saved_again.status_code, 200)
        self.assertTrue(saved_again.get_json()["runtime"]["applyPending"])

        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE hidden_flow_runtime SET flow_id=?, state_json=? WHERE chat_id=?",
            (
                "",
                json.dumps({
                    "schemaVersion": 1,
                    "flowId": "",
                    "active": False,
                    "stage": "",
                    "cycle": 0,
                    "stageTurn": 0,
                    "contextKeys": [],
                    "fixedDraws": {},
                    "startedAt": "",
                }),
                "chat-active",
            ),
        )
        connection.commit()
        connection.close()

        result = flow_studio_editor.apply_saved_editor_document_if_safe(
            "intimacy-v1",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["runtimeVersion"], 8)
        reloaded = self._get()
        self.assertFalse(reloaded["runtime"]["applyPending"])
        self.assertTrue(reloaded["runtime"]["published"])
        connection = sqlite3.connect(self.db_path)
        runtime_config = json.loads(
            connection.execute(
                "SELECT config_json FROM hidden_flow_configs WHERE flow_id=?",
                ("intimacy-v1",),
            ).fetchone()[0]
        )
        connection.close()
        self.assertEqual(runtime_config["stages"][0]["minTurns"], 3)
        self.assertEqual(runtime_config["guidancePrompt"], "DEFERRED_GLOBAL")


    def test_deferred_apply_does_not_fallback_when_latest_revision_is_invalid(self):
        first = self._get()
        valid = self._publishable_document(first["document"])
        valid["stages"][0]["minTurns"] = 2
        saved = self._save(valid, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)

        invalid = saved.get_json()["document"]
        invalid["stages"][0]["minTurns"] = 99
        invalid_saved = self._save(invalid, saved.get_json()["editorRevision"])
        self.assertEqual(invalid_saved.status_code, 200)

        result = flow_studio_editor.apply_saved_editor_document_if_safe(
            "intimacy-v1",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "blocked_validation")
        connection = sqlite3.connect(self.db_path)
        runtime_row = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(runtime_row[0], 7)
        self.assertEqual(json.loads(runtime_row[1]), self.runtime_raw)
        reloaded = self._get()
        self.assertFalse(reloaded["runtime"]["applyPending"])
        self.assertTrue(reloaded["runtime"]["applyBlocked"])
        self.assertFalse(reloaded["runtime"]["published"])

    def test_deferred_apply_waits_for_all_active_chats_sharing_flow(self):
        active_state = {
            "schemaVersion": 1,
            "flowId": "intimacy-v1",
            "active": True,
            "stage": "s1",
            "cycle": 1,
            "stageTurn": 1,
            "contextKeys": [],
            "fixedDraws": {},
            "startedAt": "2026-10-11T10:00:00Z",
        }
        connection = sqlite3.connect(self.db_path)
        for chat_id in ("chat-a", "chat-b"):
            connection.execute(
                """
                INSERT INTO hidden_flow_runtime
                    (chat_id, schema_version, flow_id, state_json,
                     pending_guide_json, config_version, version, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    chat_id,
                    1,
                    "intimacy-v1",
                    json.dumps(active_state),
                    None,
                    7,
                    1,
                    "2026-10-11 10:00:00",
                ),
            )
        connection.commit()
        connection.close()

        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)

        result = flow_studio_editor.apply_saved_editor_document_if_safe(
            "intimacy-v1",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "deferred")

        connection = sqlite3.connect(self.db_path)
        inactive_state = {
            **active_state,
            "flowId": "",
            "active": False,
            "stage": "",
            "cycle": 0,
            "stageTurn": 0,
            "startedAt": "",
        }
        connection.execute(
            "UPDATE hidden_flow_runtime SET flow_id=?, state_json=? WHERE chat_id=?",
            ("", json.dumps(inactive_state), "chat-a"),
        )
        connection.commit()
        connection.close()
        result = flow_studio_editor.apply_saved_editor_document_if_safe(
            "intimacy-v1",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "deferred")

        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "UPDATE hidden_flow_runtime SET flow_id=?, state_json=? WHERE chat_id=?",
            ("", json.dumps(inactive_state), "chat-b"),
        )
        connection.commit()
        connection.close()
        result = flow_studio_editor.apply_saved_editor_document_if_safe(
            "intimacy-v1",
            db_path=self.db_path,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["runtimeVersion"], 8)


    def test_publish_blocks_active_flow_without_changing_runtime(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "chat-active",
                1,
                "intimacy-v1",
                json.dumps({
                    "schemaVersion": 1,
                    "flowId": "intimacy-v1",
                    "active": True,
                    "stage": "s1",
                    "cycle": 1,
                    "stageTurn": 1,
                    "contextKeys": [],
                    "fixedDraws": {},
                    "startedAt": "2026-10-11T10:00:00Z",
                }),
                None,
                7,
                1,
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        first = self._get()
        document = self._publishable_document(first["document"])
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.get_json()["runtime"]["applyPending"])
        self.assertTrue(self._get()["runtime"]["applyPending"])
        response = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "FLOW_ACTIVE")
        connection = sqlite3.connect(self.db_path)
        self.assertEqual(
            connection.execute(
                "SELECT version FROM hidden_flow_configs WHERE flow_id=?",
                ("intimacy-v1",),
            ).fetchone()[0],
            7,
        )
        connection.close()


    def test_publish_rejects_unknown_nested_behavior_fields(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["behaviorFlag"] = True
        document["pools"][0]["behaviorMode"] = "hidden"
        document["pools"][0]["entries"][0]["drawWhen"] = "always"
        document["cues"][0]["triggerMode"] = "automatic"
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)

        response = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(response.status_code, 422)
        body = response.get_json()
        self.assertEqual(body["code"], "PUBLISH_VALIDATION_FAILED")
        for field in (
            "stages[s1].behaviorFlag",
            "pools[p1].behaviorMode",
            "pools[p1].entries[e1].drawWhen",
            "cues[cue-1].triggerMode",
        ):
            self.assertIn(field, body["unsafeFields"])

    def test_publish_blocks_unverifiable_runtime_state(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "chat-corrupt",
                1,
                "intimacy-v1",
                json.dumps({
                    "schemaVersion": 1,
                    "flowId": "intimacy-v1",
                    "active": "true",
                    "stage": "s1",
                    "cycle": 1,
                    "stageTurn": 1,
                    "contextKeys": [],
                    "fixedDraws": {},
                    "startedAt": "2026-10-11T10:00:00Z",
                }),
                None,
                7,
                1,
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        response = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "FLOW_ACTIVE_STATE_UNKNOWN")

    def test_publish_blocks_existing_turn_lease_and_pending_snapshot(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO daily_resident_turn_leases
                (context_id, resident_generation, lease_owner,
                 request_message_id, acquired_at, expires_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                1,
                1,
                "turn-owner",
                123,
                "2026-10-11 10:00:00",
                "2099-10-11 10:00:00",
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        blocked = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["code"], "FLOW_TURN_LEASE_ACTIVE")

        connection = sqlite3.connect(self.db_path)
        connection.execute("DELETE FROM daily_resident_turn_leases")
        state = {
            "schemaVersion": 1,
            "flowId": "",
            "active": False,
            "stage": "",
            "cycle": 0,
            "stageTurn": 0,
            "contextKeys": [],
            "fixedDraws": {},
            "startedAt": "",
        }
        connection.execute(
            """
            INSERT INTO hidden_flow_message_snapshots
                (assistant_message_id, user_message_id, chat_id, flow_id, status,
                 runtime_version_before, config_version, applied_guide_json,
                 control_json, state_before_json, state_after_json, next_guide_json,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                300,
                299,
                "chat-pending",
                "intimacy-v1",
                "PENDING",
                0,
                7,
                None,
                None,
                json.dumps(state),
                json.dumps(state),
                None,
                "2026-10-11 10:00:00",
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()

        pending = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(pending.status_code, 409)
        self.assertEqual(pending.get_json()["code"], "FLOW_PENDING_SNAPSHOT")

    def test_publish_blocks_expired_or_corrupt_turn_lease(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            INSERT INTO daily_resident_turn_leases
                (context_id, resident_generation, lease_owner,
                 request_message_id, acquired_at, expires_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                1,
                1,
                "turn-owner",
                123,
                "not-a-time",
                "2099-10-11 10:00:00",
                "2026-10-11 10:00:00",
            ),
        )
        connection.commit()
        connection.close()
        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        blocked = self._publish(
            saved.get_json()["document"],
            saved.get_json()["editorRevision"],
            7,
        )
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.get_json()["code"], "FLOW_ACTIVE_STATE_UNKNOWN")

    def test_publish_transaction_failure_preserves_runtime_config(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            CREATE TRIGGER fail_flow_studio_publish
            BEFORE UPDATE OF config_json ON hidden_flow_configs
            BEGIN
                SELECT RAISE(ABORT, 'publish test failure');
            END
            """
        )
        connection.commit()
        connection.close()

        first = self._get()
        document = self._publishable_document(first["document"])
        document["stages"][0]["minTurns"] = 2
        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        response = self._publish(saved.get_json()["document"], 1, 7)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "RUNTIME_PUBLISH_FAILED")

        connection = sqlite3.connect(self.db_path)
        row = connection.execute(
            "SELECT version, config_json FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        connection.close()
        self.assertEqual(row[0], 7)
        self.assertEqual(json.loads(row[1]), self.runtime_raw)

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
        self.assertIn("the final enabled stage must be marked as terminal", invalid.get_json()["errors"])

    def test_malformed_document_and_unknown_flow_are_rejected(self):
        malformed = self.client.put(
            "/api/flow-studio/editor/intimacy-v1",
            json={"expectedRevision": 0, "document": {"flowId": "wrong"}},
        )
        self.assertEqual(malformed.status_code, 422)
        missing = self.client.get("/api/flow-studio/editor/missing")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["code"], "FLOW_NOT_FOUND")

    def test_owner_cookie_session_allows_editor_get_and_missing_cookie_is_401(self):
        from moments_auth import owner_session_digest, require_owner

        app = Flask(__name__)
        app.register_blueprint(
            create_flow_studio_blueprint(
                db_path=self.db_path,
                owner_guard=require_owner,
            )
        )
        client = app.test_client()
        with patch("moments_auth._get_owner_token", lambda: "test-owner-token"):
            anonymous = client.get("/api/flow-studio/editor/intimacy-v1")
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(anonymous.get_json()["code"], "OWNER_AUTH_REQUIRED")
            self.assertEqual(anonymous.headers["WWW-Authenticate"], "Bearer")
            client.set_cookie("moments_owner", owner_session_digest("test-owner-token"))
            authenticated = client.get("/api/flow-studio/editor/intimacy-v1")
            self.assertEqual(authenticated.status_code, 200)
            body = authenticated.get_json()
            self.assertIn("document", body)
            self.assertIn("editorRevision", body)
            self.assertIn("runtime", body)

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
            client.post(
                "/api/flow-studio/editor/intimacy-v1/publish",
                json={
                    "expectedRevision": 0,
                    "expectedRuntimeVersion": 7,
                    "document": {},
                },
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["code"], "OWNER_AUTH_REQUIRED")
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            self.assertNotIn("document", response.get_json())
            self.assertNotIn("runtime", response.get_json())


    def test_explicit_migration_is_idempotent_and_preserves_runtime_tables(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        db_path = handle.name
        _seed_runtime(db_path)
        connection = sqlite3.connect(db_path)
        connection.execute("CREATE TABLE chat_fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO chat_fixture(value) VALUES (?)", ("keep",))
        connection.commit()
        before_hidden = connection.execute(
            "SELECT flow_id, schema_version, enabled, config_json, version, created_at, updated_at "
            "FROM hidden_flow_configs WHERE flow_id=?",
            ("intimacy-v1",),
        ).fetchone()
        before_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        connection.close()

        first = migrate(db_path)
        second = migrate(db_path)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

        connection = sqlite3.connect(db_path)
        self.assertEqual(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall(),
            sorted(before_tables + [("flow_studio_editor_documents",)]),
        )
        self.assertEqual(
            connection.execute(
                "SELECT flow_id, schema_version, enabled, config_json, version, created_at, updated_at "
                "FROM hidden_flow_configs WHERE flow_id=?",
                ("intimacy-v1",),
            ).fetchone(),
            before_hidden,
        )
        self.assertEqual(
            connection.execute("SELECT value FROM chat_fixture WHERE id=1").fetchone(),
            ("keep",),
        )
        self.assertEqual(
            [tuple(row[1:]) for row in connection.execute(
                "PRAGMA table_info(flow_studio_editor_documents)"
            ).fetchall()],
            [
                ("flow_id", "TEXT", 0, None, 1),
                ("schema_version", "INTEGER", 1, None, 0),
                ("document_json", "TEXT", 1, None, 0),
                ("revision", "INTEGER", 1, None, 0),
                ("created_at", "TEXT", 1, None, 0),
                ("updated_at", "TEXT", 1, None, 0),
            ],
        )
        connection.close()

    def test_requests_never_create_schema_and_migration_failure_is_explicit(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        db_path = handle.name
        _seed_runtime(db_path)

        app = Flask(__name__)

        def reject(_request):
            from moments_auth import OwnerAuthError
            raise OwnerAuthError("unauthorized", 401)

        app.register_blueprint(
            create_flow_studio_blueprint(db_path=db_path, owner_guard=reject)
        )
        client = app.test_client()
        for response in (
            client.get("/api/flow-studio/editor/intimacy-v1"),
            client.put(
                "/api/flow-studio/editor/intimacy-v1",
                json={"expectedRevision": 0, "document": {}},
            ),
            client.post(
                "/api/flow-studio/editor/intimacy-v1/validate",
                json={"document": {}},
            ),
        ):
            self.assertEqual(response.status_code, 401)
        connection = sqlite3.connect(db_path)
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("flow_studio_editor_documents",),
            ).fetchone()
        )
        connection.close()

        migrate(db_path)
        statements = []
        original_connect = flow_studio_editor._connect

        def traced_connect(path):
            connection = original_connect(path)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("flow_studio_editor._connect", traced_connect):
            authorized_app = Flask(__name__)
            authorized_app.register_blueprint(
                create_flow_studio_blueprint(
                    db_path=db_path, owner_guard=lambda _request: None
                )
            )
            self.assertEqual(
                authorized_app.test_client().get(
                    "/api/flow-studio/editor/intimacy-v1"
                ).status_code,
                200,
            )
        self.assertFalse(any("CREATE TABLE" in statement.upper() for statement in statements))

        malformed = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        malformed.close()
        self.addCleanup(lambda: os.unlink(malformed.name))
        connection = sqlite3.connect(malformed.name)
        connection.execute(
            "CREATE TABLE flow_studio_editor_documents (flow_id TEXT PRIMARY KEY)"
        )
        connection.commit()
        connection.close()
        with self.assertRaises(MigrationError):
            migrate(malformed.name)

    def test_deploy_uses_explicit_migration_after_backup(self):
        root = os.path.dirname(os.path.dirname(__file__))
        with open(
            os.path.join(root, "scripts", "deploy-frontend.sh"),
            encoding="utf-8",
        ) as handle:
            deploy_source = handle.read()
        with open(
            os.path.join(root, "flow_studio_editor.py"),
            encoding="utf-8",
        ) as handle:
            editor_source = handle.read()
        backup_marker = 'bash "$ROOT/tools/backup.sh"'
        migration_marker = '"$PYTHON" "$staging/migrations/flow_studio_editor.py"'
        self.assertIn(migration_marker, deploy_source)
        self.assertLess(deploy_source.index(backup_marker), deploy_source.index(migration_marker))
        self.assertNotIn("@blueprint.before_request", editor_source)
        self.assertNotIn("def _ensure_schema", editor_source)


    def test_disabled_cues_are_valid_filtered_and_revision_28_publishes(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["pools"].append({
            "id": "disabled-cue-pool",
            "name": "停用 Cue 专用池",
            "enabled": False,
            "mode": "perTurn",
            "count": 1,
            "entries": [{"id": "disabled-cue-entry", "text": "停用池条目", "enabled": True}],
        })
        document["cues"] = [
            {
                "id": cue_id,
                "key": key,
                "kind": "氛围",
                "enabled": False,
                "poolIds": ["disabled-cue-pool"],
            }
            for cue_id, key in (
                ("cue-cuddle", "cuddle"),
                ("cue-flirty", "flirty"),
                ("cue-spoiled", "spoiled"),
                ("cue-sticky", "sticky"),
            )
        ]

        compiled, errors, unsupported = flow_studio_editor.compile_editor_document(document)
        self.assertEqual(errors, [])
        self.assertIsNotNone(compiled)
        assert compiled is not None
        self.assertEqual(compiled["cues"], [])
        self.assertFalse(
            any(
                field.startswith("cues[") and field.endswith(".enabled")
                for field in unsupported
            )
        )
        self.assertEqual(
            [cue["poolIds"] for cue in document["cues"]],
            [["disabled-cue-pool"]] * 4,
        )

        saved = self._save(document, first["editorRevision"])
        self.assertEqual(saved.status_code, 200)
        saved_body = saved.get_json()
        self.assertTrue(saved_body["runtime"]["applyPending"])
        self.assertFalse(saved_body["runtime"]["applyBlocked"])

        published = self._publish(
            saved_body["document"],
            saved_body["editorRevision"],
            saved_body["runtime"]["version"],
        )
        self.assertEqual(published.status_code, 200)
        published_body = published.get_json()
        self.assertTrue(published_body["runtime"]["published"])
        connection = sqlite3.connect(self.db_path)
        runtime_config = json.loads(
            connection.execute(
                "SELECT config_json FROM hidden_flow_configs WHERE flow_id=?",
                ("intimacy-v1",),
            ).fetchone()[0]
        )
        connection.close()
        self.assertEqual(runtime_config["cues"], [])

        reloaded = self._get()
        self.assertEqual(
            [
                (cue["id"], cue["enabled"], cue["key"], cue["poolIds"])
                for cue in reloaded["document"]["cues"]
            ],
            [
                (cue["id"], False, cue["key"], ["disabled-cue-pool"])
                for cue in document["cues"]
            ],
        )

        reenabled = json.loads(json.dumps(reloaded["document"]))
        reenabled["cues"][0]["enabled"] = True
        recompiled, reenable_errors, _ = flow_studio_editor.compile_editor_document(reenabled)
        self.assertEqual(reenable_errors, [])
        self.assertIsNotNone(recompiled)
        assert recompiled is not None
        self.assertEqual(recompiled["cues"][0]["key"], "cuddle")
        self.assertEqual(recompiled["cues"][0]["poolIds"], ["disabled-cue-pool"])

    def test_disabled_cue_missing_pool_fails_closed_and_duplicate_only_checks_enabled(self):
        first = self._get()
        document = self._publishable_document(first["document"])
        document["cues"] = [
            {
                "id": "cue-enabled",
                "key": "shared-key",
                "kind": "氛围",
                "enabled": True,
                "poolIds": ["p1"],
            },
            {
                "id": "cue-disabled",
                "key": "shared-key",
                "kind": "氛围",
                "enabled": False,
                "poolIds": ["p1"],
            },
        ]

        _, errors, unsupported = flow_studio_editor.compile_editor_document(document)
        self.assertEqual(errors, [])
        self.assertFalse(any("duplicate enabled key" in error for error in errors))
        self.assertFalse(
            any(
                field.startswith("cues[") and field.endswith(".enabled")
                for field in unsupported
            )
        )

        reenabled = json.loads(json.dumps(document))
        reenabled["cues"][1]["enabled"] = True
        _, duplicate_errors, _ = flow_studio_editor.compile_editor_document(reenabled)
        self.assertIn("cue cue-disabled: duplicate enabled key", duplicate_errors)

        missing = json.loads(json.dumps(document))
        missing["cues"][1]["poolIds"] = ["missing-cue-pool"]
        _, missing_errors, _ = flow_studio_editor.compile_editor_document(missing)
        self.assertIn("cue cue-disabled: references a missing pool", missing_errors)

if __name__ == "__main__":
    unittest.main()
