"""Focused HTTP tests for the production Continuity Settings Authority."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from continuity.settings import ensure_authority, load_authority
from continuity.store import ensure_schema
from context_compression_routes import create_context_compression_blueprint

PERSONA = "\n".join(f"## {name}\nsection {name}" for name in "ABCDEF")
CATALOG = {"models": [{
    "id": "claude-opus-5-5", "label": "Opus 5.5", "runtime_compatible": True,
}]}


class ContinuitySettingsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "memories.db")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
        ensure_authority(
            conn, provider="claude_code", model_identity="explicit:claude-opus-5-5",
            persona_text=PERSONA, now="2026-09-26T00:00:00Z",
        )
        conn.close()
        self.app = Flask(__name__)
        self.app.register_blueprint(create_context_compression_blueprint(db_path=self.db_path))
        self.client = self.app.test_client()
        self.patches = [
            patch("chat.cc_model.get_cc_model_catalog", return_value=CATALOG),
            patch("continuity.settings._persona_from_runtime", return_value=PERSONA),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def _authority(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return load_authority(conn)
        finally:
            conn.close()

    def test_get_exposes_single_active_authority_without_writing(self):
        before = self._authority()["active_revision"]["revision_id"]
        response = self.client.get("/dash/__continuity/settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["authority"]["active_revision"]["revision_id"], before)
        self.assertIsNone(payload["authority"]["pending_revision"])
        active = self._authority()["active_revision"]
        for key in (
            "prompt_hash", "prompt_revision", "prompt_policy_version",
            "persona_hash", "persona_revision", "persona_policy_version",
            "measurement_semantics", "sealing_policy_version",
        ):
            self.assertEqual(payload["authority"]["active_revision"][key], active[key])
        self.assertEqual(payload["authority"]["active_revision"]["prompt"], active["prompt_body"])
        self.assertEqual(payload["authority"]["active_revision"]["model_identity"], active["model_identity"])
        self.assertEqual(payload["authority"]["current_block"]["revision_id"], before)
        self.assertEqual(payload["defaults"]["revision_id"], before)
        self.assertEqual(payload["defaults"]["length"], active["target_logical_size"])
        providers = payload["registry"]["providers"]
        self.assertEqual(providers[0]["id"], "claude_code")
        self.assertEqual([row["id"] for row in providers[0]["models"]], [
            "default", "claude-opus-5-5",
        ])
        after = self._authority()["active_revision"]["revision_id"]
        self.assertEqual(before, after)

    def test_provider_registry_uses_existing_runtime_catalog(self):
        from context_compression_routes import _provider_registry

        catalog = {"models": [
            {"id": "claude-live-model", "label": "Live model", "runtime_compatible": True},
            {"id": "claude-unavailable-model", "runtime_compatible": False},
        ]}
        with patch("chat.cc_model.get_cc_model_catalog", return_value=catalog):
            registry = _provider_registry()
        claude = next(row for row in registry["providers"] if row["id"] == "claude_code")
        self.assertEqual([row["id"] for row in claude["models"]], [
            "default", "claude-live-model", "claude-unavailable-model",
        ])
        self.assertTrue(claude["models"][1]["enabled"])
        self.assertFalse(claude["models"][2]["enabled"])
        self.assertNotIn("api_relay", [row["id"] for row in registry["providers"]])

    def test_save_with_open_source_tail_keeps_current_active_and_sets_pending(self):
        current = {
            "available": True, "source_count": 2, "source_refs": ["turn:1", "turn:2"],
            "source_revisions": ["rev:1", "rev:2"], "context_id": 7,
            "context_epoch": 3, "materialization_error": None,
        }
        with patch("context_compression_routes.get_current", return_value=current), \
             patch("chat.cc_model.cc_model_runtime_compatibility", return_value=(True, None)):
            response = self.client.post("/dash/__continuity/settings", json={
                "length": 16000, "turns": 30, "provider": "claude_code",
                "model": "claude-opus-5-5", "prompt": "frozen next prompt",
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        authority = self._authority()
        self.assertEqual(authority["active_revision"]["revision_id"], "continuity-settings-baseline-v1")
        self.assertEqual(authority["pending_revision"]["target_logical_size"], 16000)
        self.assertEqual(authority["pending_revision"]["model_identity"], "explicit:claude-opus-5-5")
        self.assertEqual(authority["pending_anchor"]["first_source_ref"], "turn:1")
        self.assertEqual(authority["pending_anchor"]["first_source_revision"], "rev:1")
        self.assertEqual(payload["authority"]["current_block"]["revision_id"], "continuity-settings-baseline-v1")
        self.assertIsNotNone(payload["authority"]["pending_revision"])
        pending_id = authority["pending_revision"]["revision_id"]
        self.assertEqual(authority["pending_revision"]["lifecycle"], "immutable")
        conn = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE continuity_settings_revisions SET prompt_body='rewritten' WHERE revision_id=?",
                    (pending_id,),
                )
        finally:
            conn.close()

    def test_current_uses_active_revision_while_pending_is_queued(self):
        open_tail = {
            "available": True, "source_count": 1, "source_refs": ["turn:1"],
            "source_revisions": ["rev:1"], "context_id": 7,
            "context_epoch": 3, "materialization_error": None,
        }
        with patch("context_compression_routes.get_current", return_value=open_tail), \
             patch("chat.cc_model.cc_model_runtime_compatibility", return_value=(True, None)):
            saved = self.client.post("/dash/__continuity/settings", json={
                "length": 16000, "turns": 30, "provider": "claude_code",
                "model": "claude-opus-5-5", "prompt": "pending prompt",
            })
        self.assertEqual(saved.status_code, 200, saved.get_json())
        current = {
            "ok": True, "available": True, "context_id": 7,
            "context_epoch": 3, "source_count": 1,
        }
        with patch("context_compression_routes.get_current", return_value=current) as reader:
            response = self.client.get("/dash/__continuity/current")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reader.call_args.kwargs["policy"].target_logical_size, 12000)
        self.assertEqual(response.get_json()["settings_revision_id"], "continuity-settings-baseline-v1")

    def test_save_with_no_open_source_tail_changes_active_immediately(self):
        current = {
            "available": False, "source_count": 0, "source_refs": [],
            "source_revisions": [], "context_id": 7, "context_epoch": 3,
            "materialization_error": None,
        }
        with patch("context_compression_routes.get_current", return_value=current), \
             patch("chat.cc_model.cc_model_runtime_compatibility", return_value=(True, None)):
            response = self.client.post("/dash/__continuity/settings", json={
                "length": 16000, "turns": 30, "provider": "claude_code",
                "model": "claude-opus-5-5", "prompt": "new active prompt",
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        authority = self._authority()
        self.assertEqual(authority["active_revision"]["target_logical_size"], 16000)
        self.assertIsNone(authority["pending_revision"])

    def test_invalid_provider_and_unknown_source_fail_closed(self):
        bad_provider = self.client.post("/dash/__continuity/settings", json={
            "length": 16000, "turns": 30, "provider": "deepseek",
            "model": "deepseek-chat", "prompt": "prompt",
        })
        self.assertEqual(bad_provider.status_code, 400)
        bad_model = self.client.post("/dash/__continuity/settings", json={
            "length": 16000, "turns": 30, "provider": "claude_code",
            "model": "model-not-in-current-catalog", "prompt": "prompt",
        })
        self.assertEqual(bad_model.status_code, 400)
        with patch("chat.cc_model.get_cc_model_catalog", return_value={"models": [
            {"id": "claude-opus-5-5", "runtime_compatible": False},
        ]}):
            unavailable_default = self.client.post("/dash/__continuity/settings", json={
                "length": 16000, "turns": 30, "provider": "claude_code",
                "model": "default", "prompt": "prompt",
            })
        self.assertEqual(unavailable_default.status_code, 400)
        current = {
            "available": False, "source_count": 0, "source_refs": [],
            "source_revisions": [], "context_id": None, "context_epoch": None,
            "materialization_error": "window_identity_unavailable",
        }
        with patch("context_compression_routes.get_current", return_value=current), \
             patch("chat.cc_model.cc_model_runtime_compatibility", return_value=(True, None)):
            unavailable = self.client.post("/dash/__continuity/settings", json={
                "length": 16000, "turns": 30, "provider": "claude_code",
                "model": "claude-opus-5-5", "prompt": "prompt",
            })
        self.assertEqual(unavailable.status_code, 409)
        authority = self._authority()
        self.assertEqual(authority["active_revision"]["revision_id"], "continuity-settings-baseline-v1")
        self.assertIsNone(authority["pending_revision"])


if __name__ == "__main__":
    unittest.main()
