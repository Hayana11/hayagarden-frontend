from __future__ import annotations

import builtins
import importlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config_store
import moments_auth
from tools import capability_state


app_module = None


class CapabilityStateHttpContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "runtime.db")
        conn = sqlite3.connect(self.db_path)
        # app.py's import-time migration expects the canonical chat table to
        # exist, but the columns owned by that migration must remain absent.
        conn.execute(
            "CREATE TABLE chat_messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "author TEXT NOT NULL DEFAULT 'user', "
            "content TEXT NOT NULL, "
            "thinking TEXT DEFAULT '', "
            "image_url TEXT DEFAULT '', "
            "session_id INTEGER DEFAULT 1, "
            "created_at DATETIME DEFAULT (datetime('now', '+8 hours'))"
            ")"
        )
        conn.execute(
            "CREATE TABLE runtime_config ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?)",
            ("TOOL_DISABLED", json.dumps(["legacy_tool"])),
        )
        conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?)",
            ("TOOL_DRAWER_DISABLED", json.dumps(["legacy_drawer"])),
        )
        conn.commit()
        conn.close()

        global app_module
        if app_module is None:
            real_connect = sqlite3.connect

            def isolated_connect(database, *args, **kwargs):
                if os.fspath(database) == "/opt/frontend/memories.db":
                    database = self.db_path
                    conn = real_connect(database, *args, **kwargs)
                    conn.row_factory = sqlite3.Row
                    return conn
                return real_connect(database, *args, **kwargs)

            def isolated_open(file, *args, **kwargs):
                if os.fspath(file) == "/opt/frontend/.env":
                    return io.StringIO("")
                return real_open(file, *args, **kwargs)

            real_open = builtins.open
            with mock.patch.object(
                sqlite3,
                "connect",
                side_effect=isolated_connect,
            ), mock.patch.object(
                builtins,
                "open",
                side_effect=isolated_open,
            ):
                app_module = importlib.import_module("app")

        self.app_db_patch = mock.patch.object(app_module, "DB_PATH", self.db_path)
        self.app_db_patch.start()
        self.db_patch = mock.patch.object(capability_state, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.config_db_patch = mock.patch.object(config_store, "DB_PATH", self.db_path)
        self.config_db_patch.start()
        self.env_patch = mock.patch.dict(
            os.environ,
            {"MOMENTS_OWNER_TOKEN": "test-owner-token"},
            clear=False,
        )
        self.env_patch.start()
        self.auth_patch = mock.patch.object(
            moments_auth,
            "_get_owner_token",
            moments_auth.owner_token_getter(),
        )
        self.auth_patch.start()
        self.client = app_module.app.test_client()
        self.owner_headers = {"Authorization": "Bearer test-owner-token"}

    def tearDown(self):
        self.auth_patch.stop()
        self.env_patch.stop()
        self.config_db_patch.stop()
        self.db_patch.stop()
        self.app_db_patch.stop()
        self.temp_dir.cleanup()

    def _read_rows(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT key, value FROM runtime_config ORDER BY key"
            ).fetchall()
        finally:
            conn.close()

    def _state(self, payload, capability_id="todo.read"):
        return next(
            row
            for row in payload["states"]
            if row["capability_id"] == capability_id
        )

    def test_get_missing_state_returns_inherit_without_writing(self):
        before = self._read_rows()
        response = self.client.get("/api/capabilities/states")
        after = self._read_rows()

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], 1)
        self.assertEqual(
            self._state(payload),
            {
                "capability_id": "todo.read",
                "static_enabled": True,
                "runtime_state": "INHERIT",
                "effective_enabled": True,
                "writable": True,
            },
        )
        self.assertEqual(after, before)

    def test_patch_off_then_on_preserves_explicit_states(self):
        off = self.client.patch(
            "/api/capabilities/todo.read/state",
            json={"enabled": False},
            headers=self.owner_headers,
        )
        self.assertEqual(off.status_code, 200)
        self.assertEqual(
            off.get_json()["state"]["runtime_state"],
            "OFF",
        )
        self.assertEqual(
            capability_state.read_capability_state("todo.read"),
            capability_state.RUNTIME_STATE_OFF,
        )

        on = self.client.patch(
            "/api/capabilities/todo.read/state",
            json={"enabled": True},
            headers=self.owner_headers,
        )
        self.assertEqual(on.status_code, 200)
        self.assertEqual(
            on.get_json()["state"]["runtime_state"],
            "ON",
        )
        self.assertEqual(
            capability_state.read_capability_state("todo.read"),
            capability_state.RUNTIME_STATE_ON,
        )

        reread = self.client.get("/api/capabilities/states")
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(self._state(reread.get_json())["runtime_state"], "ON")

    def test_unknown_reserved_and_static_not_enabled_are_rejected(self):
        for capability_id in ("unknown.capability", "code.write"):
            with self.subTest(capability_id=capability_id):
                response = self.client.patch(
                    f"/api/capabilities/{capability_id}/state",
                    json={"enabled": True},
                    headers=self.owner_headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
                self.assertEqual(
                    response.get_json()["error"],
                    "capability_not_writable",
                )

        enabled_without_todo = (
            set(capability_state.P1_ENABLED_CAPABILITY_IDS) - {"todo.read"}
        )
        with mock.patch.object(
            capability_state,
            "P1_ENABLED_CAPABILITY_IDS",
            frozenset(enabled_without_todo),
        ):
            response = self.client.patch(
                "/api/capabilities/todo.read/state",
                json={"enabled": True},
                headers=self.owner_headers,
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])

    def test_patch_accepts_only_strict_boolean_enabled(self):
        invalid_bodies = (
            {},
            {"enabled": 0},
            {"enabled": 1},
            {"enabled": "true"},
            {"enabled": "false"},
            {"enabled": True, "state": "ON"},
            {"enabled": False, "tool": "todo.read"},
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                response = self.client.patch(
                    "/api/capabilities/todo.read/state",
                    json=body,
                    headers=self.owner_headers,
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])

    def test_patch_requires_owner_and_reports_unconfigured_auth(self):
        missing = self.client.patch(
            "/api/capabilities/todo.read/state",
            json={"enabled": False},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertFalse(missing.get_json()["ok"])

        with mock.patch.object(moments_auth, "_get_owner_token", lambda: ""):
            unavailable = self.client.patch(
                "/api/capabilities/todo.read/state",
                json={"enabled": False},
                headers=self.owner_headers,
            )
        self.assertEqual(unavailable.status_code, 503)
        self.assertFalse(unavailable.get_json()["ok"])

    def test_malformed_runtime_state_fails_closed(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO runtime_config (key, value) VALUES (?, ?)",
            (capability_state.CAPABILITY_STATE_KEY, "{malformed"),
        )
        conn.commit()
        conn.close()

        response = self.client.get("/api/capabilities/states")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])
        self.assertNotIn("states", response.get_json())

    def test_storage_unavailable_does_not_fake_get_or_patch_success(self):
        with mock.patch.object(
            capability_state,
            "_connect",
            side_effect=sqlite3.OperationalError("database unavailable"),
        ):
            get_response = self.client.get("/api/capabilities/states")
            patch_response = self.client.patch(
                "/api/capabilities/todo.read/state",
                json={"enabled": False},
                headers=self.owner_headers,
            )
        self.assertEqual(get_response.status_code, 503)
        self.assertEqual(patch_response.status_code, 503)
        self.assertFalse(get_response.get_json()["ok"])
        self.assertFalse(patch_response.get_json()["ok"])

    def test_write_transaction_failure_does_not_return_success(self):
        with mock.patch.object(
            capability_state,
            "_connect",
            side_effect=sqlite3.OperationalError("write unavailable"),
        ):
            response = self.client.patch(
                "/api/capabilities/todo.read/state",
                json={"enabled": False},
                headers=self.owner_headers,
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])

    def test_legacy_drawer_keys_are_unchanged(self):
        before = self._read_rows()
        response = self.client.patch(
            "/api/capabilities/todo.read/state",
            json={"enabled": False},
            headers=self.owner_headers,
        )
        after = self._read_rows()

        self.assertEqual(response.status_code, 200)
        before_legacy = {
            key: value
            for key, value in before
            if key in {"TOOL_DISABLED", "TOOL_DRAWER_DISABLED"}
        }
        after_legacy = {
            key: value
            for key, value in after
            if key in {"TOOL_DISABLED", "TOOL_DRAWER_DISABLED"}
        }
        self.assertEqual(after_legacy, before_legacy)

    def test_companion_hints_patch_rejects_capability_state_fields(self):
        for field in ("enabled", "state"):
            with self.subTest(field=field):
                response = self.client.patch(
                    "/api/tools/companion-hints",
                    json={"capability_id": "todo.read", field: False},
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
