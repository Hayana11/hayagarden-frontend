from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import capability_state
from tools.cc_capability_adapter import (
    build_uh_a0_mcp_config,
    build_uh_a0_settings,
    build_uh_a0_spawn_plan,
)
from tools.cc_tool_surface import capture_tool_surface_snapshot
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.ombre_native_surface import (
    OFFICIAL_HOOK_PATH,
    UPSTREAM_BRAIN_ALLOW_RULE,
    UPSTREAM_BRAIN_TOOL_BINDINGS,
)
from chat import system_builder


class OmbreUhA0NativeTests(unittest.TestCase):
    def _legacy_config(self, root: Path) -> Path:
        path = root / "cc-tools.json"
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "brain": {
                            "type": "http",
                            "url": "http://127.0.0.1:18002/mcp",
                            "headers": {
                                "Authorization": "Bearer ${OMBRE_MCP_TOKEN}",
                            },
                            "timeout": 12,
                        },
                        "home": {
                            "type": "http",
                            "url": "http://127.0.0.1:3100/mcp",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_brain_entry_is_copied_without_secret_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._legacy_config(root)
            config = build_uh_a0_mcp_config(legacy_mcp_config_path=source, env={})
            brain = config["mcpServers"]["brain"]
            self.assertEqual(brain["url"], "http://127.0.0.1:18002/mcp")
            self.assertEqual(brain["headers"]["Authorization"], "Bearer ${OMBRE_MCP_TOKEN}")
            self.assertEqual(brain["timeout"], 12)
            self.assertNotIn("mcp_token", json.dumps(config))
            self.assertNotIn("Bearer ey", json.dumps(config))

    def test_invalid_or_missing_brain_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "missing.json"
            with self.assertRaises(ValueError):
                build_uh_a0_mcp_config(legacy_mcp_config_path=missing, env={})
            invalid = root / "invalid.json"
            invalid.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                build_uh_a0_mcp_config(legacy_mcp_config_path=invalid, env={})

    def test_spawn_plan_uses_native_wildcard_and_keeps_other_servers_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._legacy_config(root)
            db_path = root / "runtime.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE runtime_config ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_at DATETIME DEFAULT (datetime('now')))"
            )
            conn.commit()
            conn.close()
            with mock.patch.object(capability_state, "DB_PATH", str(db_path)):
                plan = build_uh_a0_spawn_plan(
                    cwd=root,
                    legacy_mcp_config_path=source,
                    write_mcp_config=False,
                    write_settings=False,
                    env={},
                )
            self.assertEqual(plan["brain_mcp_tools"], (UPSTREAM_BRAIN_ALLOW_RULE,))
            self.assertIn(UPSTREAM_BRAIN_ALLOW_RULE, plan["surface_allowlist"])
            self.assertNotIn("brain", plan["claude_absent_servers"])
            self.assertNotIn("codebase", plan["mcp_config"]["mcpServers"])
            self.assertEqual(
                plan["mcp_config"]["mcpServers"]["brain"],
                json.loads(source.read_text(encoding="utf-8"))["mcpServers"]["brain"],
            )

    def test_native_fence_allows_observed_tools_only(self):
        lease = issue_turn_lease(
            turn_id="ombre-native",
            turn_mode="chat",
            issued_from="default_policy",
        )
        allowed = evaluate_tool_call("mcp__brain__hold", {}, lease)
        unknown = evaluate_tool_call("mcp__brain__not_from_tools_list", {}, lease)
        self.assertEqual(allowed["lease_decision"], "ALLOW")
        self.assertEqual(allowed["capability_id"], "ombre_native:brain")
        self.assertEqual(unknown["lease_decision"], "DENIED_CAPABILITY")

    def test_wildcard_surface_expands_to_observed_upstream_names(self):
        snapshot = capture_tool_surface_snapshot(
            UPSTREAM_BRAIN_ALLOW_RULE,
            prefer_live_mcp=False,
        )
        self.assertEqual(snapshot["tool_schema_measurement_status"], "available")
        self.assertEqual(snapshot["missing_tool_schemas"], [])
        self.assertEqual(
            {item["name"] for item in json.loads(snapshot["tool_schema_text"])},
            set(UPSTREAM_BRAIN_TOOL_BINDINGS),
        )
        self.assertEqual(snapshot["tool_count"], len(UPSTREAM_BRAIN_TOOL_BINDINGS))

    def test_session_start_reuses_official_hook_path_and_interpreter(self):
        settings = build_uh_a0_settings(repo_root="/opt/frontend")
        hook = settings["hooks"]["SessionStart"][0]
        self.assertEqual(hook["matcher"], "startup|resume")
        self.assertEqual(
            hook["hooks"][0]["command"],
            f"/usr/bin/python3.11 {OFFICIAL_HOOK_PATH}",
        )

    def test_official_prompt_is_injected_byte_for_byte_and_observed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE_PROMPT.md"
            raw = "官方\nPrompt\n".encode("utf-8")
            path.write_bytes(raw)
            system_builder.load_official_ombre_prompt.cache_clear()
            with mock.patch.object(system_builder, "OFFICIAL_PROMPT_PATH", str(path)):
                prompt, observation = system_builder.load_official_ombre_prompt()
            system_builder.load_official_ombre_prompt.cache_clear()
        self.assertEqual(prompt.encode("utf-8"), raw)
        self.assertTrue(observation["ombre_prompt_loaded"])
        self.assertEqual(
            observation["ombre_prompt_sha256"],
            hashlib.sha256(raw).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
