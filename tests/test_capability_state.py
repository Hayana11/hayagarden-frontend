"""Runtime capability-state and execution-fence contract tests."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import capability_state
from tools.capability_state import (
    CAPABILITY_STATE_KEY,
    RUNTIME_STATE_DENY,
    RUNTIME_STATE_OFF,
    RUNTIME_STATE_ON,
    effective_capability_state,
    read_capability_state,
    set_capability_state,
)
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease


class CapabilityStateFenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "runtime.db"
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE runtime_config ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at DATETIME DEFAULT (datetime('now')))"
        )
        conn.commit()
        conn.close()
        self._patch = patch.object(
            capability_state, "DB_PATH", str(self._db_path)
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def lease(
        self,
        *,
        mode="chat",
        source="default_policy",
        requested=(),
        approvals=(),
        turn_id="runtime-turn",
    ):
        return issue_turn_lease(
            turn_id=turn_id,
            turn_mode=mode,
            issued_from=source,
            requested_capabilities=requested,
            approval_ids=approvals,
            issued_at="2026-08-20T00:00:00Z",
            **({"task_contract_id": "task-1"} if source == "task_contract" else {}),
        )

    def _raw_state(self, value):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO runtime_config "
            "(key, value) VALUES (?, ?)",
            (CAPABILITY_STATE_KEY, value),
        )
        conn.commit()
        conn.close()

    def test_missing_state_inherits_existing_fence_behavior(self):
        home = evaluate_tool_call(
            "mcp__home__get_todos", {}, self.lease()
        )
        native = evaluate_tool_call(
            "Read", {}, self.lease(mode="task")
        )
        self.assertEqual(home["lease_decision"], "ALLOW")
        self.assertEqual(native["lease_decision"], "ALLOW")
        self.assertEqual(read_capability_state("todo.read"), "INHERIT")

    def test_explicit_on_keeps_existing_fence_behavior(self):
        set_capability_state("todo.read", enabled=True)
        result = evaluate_tool_call(
            "mcp__home__get_todos", {}, self.lease()
        )
        self.assertEqual(read_capability_state("todo.read"), RUNTIME_STATE_ON)
        self.assertEqual(result["lease_decision"], "ALLOW")

    def test_explicit_off_denies_home_mcp(self):
        set_capability_state("todo.read", enabled=False)
        result = evaluate_tool_call(
            "mcp__home__get_todos", {}, self.lease()
        )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")
        self.assertIn("explicitly disabled", result["diagnostic"])

    def test_explicit_off_denies_native_read(self):
        set_capability_state("files.read", enabled=False)
        result = evaluate_tool_call("Read", {}, self.lease(mode="task"))
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")

    def test_explicit_off_cannot_bypass_read_auto(self):
        set_capability_state("web.search", enabled=False)
        result = evaluate_tool_call(
            "WebSearch", {"query": "x"}, self.lease()
        )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")

    def test_explicit_off_cannot_bypass_self_write_auto(self):
        set_capability_state("diary.write", enabled=False)
        result = evaluate_tool_call(
            "mcp__home__write_diary", {"content": "x"}, self.lease()
        )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")

    def test_explicit_off_cannot_bypass_existing_allowed_lease(self):
        set_capability_state("todo.write", enabled=False)
        lease = self.lease(
            source="explicit_user_intent",
            requested=("todo.write",),
        )
        result = evaluate_tool_call(
            "mcp__home__add_todo", {"content": "x"}, lease
        )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")

    def test_reserved_runtime_on_does_not_expand_static_set(self):
        with self.assertRaises(ValueError):
            set_capability_state("github.read", enabled=True)
        self._raw_state(json.dumps({
            "schema_version": 1,
            "states": {"github.read": "ON"},
        }))
        self.assertEqual(
            effective_capability_state("github.read"), RUNTIME_STATE_DENY
        )

    def test_unknown_capability_cannot_create_valid_state(self):
        with self.assertRaises(ValueError):
            set_capability_state("made.up.capability", enabled=True)
        conn = sqlite3.connect(self._db_path)
        row = conn.execute(
            "SELECT value FROM runtime_config WHERE key=?",
            (CAPABILITY_STATE_KEY,),
        ).fetchone()
        conn.close()
        self.assertIsNone(row)

    def test_malformed_state_fails_closed(self):
        self._raw_state("{not-json")
        result = evaluate_tool_call(
            "mcp__home__get_todos", {}, self.lease()
        )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")
        self.assertIn("unavailable", result["diagnostic"])

    def test_storage_read_failure_fails_closed(self):
        with patch.object(
            capability_state,
            "_connect",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            result = evaluate_tool_call(
                "mcp__home__get_todos", {}, self.lease()
            )
        self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")
        self.assertIn("unavailable", result["diagnostic"])

    def test_independent_read_instance_sees_persisted_state(self):
        set_capability_state("todo.read", enabled=False)
        self.assertEqual(read_capability_state("todo.read"), RUNTIME_STATE_OFF)
        conn = sqlite3.connect(self._db_path)
        raw = conn.execute(
            "SELECT value FROM runtime_config WHERE key=?",
            (CAPABILITY_STATE_KEY,),
        ).fetchone()[0]
        conn.close()
        self.assertEqual(json.loads(raw)["states"]["todo.read"], "OFF")


if __name__ == "__main__":
    unittest.main()
