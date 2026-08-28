"""Regression tests for the production-equivalent PreToolUse hook command.

The child hook must import the production repository modules when launched from
Claude Code's non-repository working directory, without inheriting a test
runner PYTHONPATH.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.cc_capability_adapter import build_uh_a0_settings
from tools.execution_fence import write_current_turn_lease
from tools.lease_signer import issue_turn_lease

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_STATE_KEY = "CAPABILITY_RUNTIME_STATE_V1"


class PreToolUseCwdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name)
        self._lease_path = self._root / "turn-lease.json"
        self._runtime_db = self._root / "runtime.db"
        self._make_runtime_db()
        lease = issue_turn_lease(
            turn_id="pretooluse-cwd",
            turn_mode="chat",
            issued_from="default_policy",
            issued_at="2026-08-28T00:00:00Z",
        )
        write_current_turn_lease(self._lease_path, lease)
        self.addCleanup(self._tmp.cleanup)

    def _make_runtime_db(self, state=None):
        if self._runtime_db.exists():
            self._runtime_db.unlink()
        conn = sqlite3.connect(self._runtime_db)
        conn.execute(
            "CREATE TABLE runtime_config ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at DATETIME DEFAULT (datetime('now')))"
        )
        if state is not None:
            conn.execute(
                "INSERT INTO runtime_config(key, value) VALUES (?, ?)",
                (
                    RUNTIME_STATE_KEY,
                    json.dumps(
                        {"schema_version": 1, "states": state},
                        separators=(",", ":"),
                    ),
                ),
            )
        conn.commit()
        conn.close()

    def _hook_command(self):
        settings = build_uh_a0_settings(repo_root=ROOT)
        return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def _run_hook(self, tool_name, *, runtime_state=None):
        if runtime_state is not None:
            self._make_runtime_db(runtime_state)
        env = os.environ.copy()
        # Do not let the test runner's import path make this pass accidentally.
        env["PYTHONPATH"] = ""
        env["UH_A0_TURN_LEASE_PATH"] = str(self._lease_path)
        env["HAYAGARDEN_CONFIG_DB_PATH"] = str(self._runtime_db)
        payload = {
            "tool_name": tool_name,
            "tool_input": {"title": "cwd regression"},
        }
        return subprocess.run(
            ["sh", "-c", self._hook_command()],
            input=json.dumps(payload),
            cwd=str(self._root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

    def test_generated_hook_imports_from_non_repo_cwd_and_enforces_fence(self):
        command = self._hook_command()
        self.assertTrue(command.startswith("PYTHONPATH="))
        self.assertIn("python3 -m tools.execution_fence pretooluse", command)

        allowed = self._run_hook("mcp__capability__task_timer_start")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertNotIn("ModuleNotFoundError", allowed.stderr)
        self.assertNotIn("ModuleNotFoundError", allowed.stdout)
        self.assertEqual(
            json.loads(allowed.stdout)["hookSpecificOutput"]["permissionDecision"],
            "allow",
        )

        runtime_off = self._run_hook(
            "mcp__capability__task_timer_start",
            runtime_state={"task.timer.start": "OFF"},
        )
        self.assertEqual(runtime_off.returncode, 0, runtime_off.stderr)
        self.assertEqual(
            json.loads(runtime_off.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        unknown = self._run_hook("mcp__capability__unknown")
        self.assertEqual(unknown.returncode, 0, unknown.stderr)
        self.assertEqual(
            json.loads(unknown.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )

        task_only = self._run_hook("Read")
        self.assertEqual(task_only.returncode, 0, task_only.stderr)
        self.assertEqual(
            json.loads(task_only.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )


if __name__ == "__main__":
    unittest.main()
