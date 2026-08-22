"""Focused UH-A0 P4 execution-enforcement tests."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import capability_state
from tools.capability_manifest import P1_RESERVED_CAPABILITY_IDS
from tools.cc_capability_adapter import build_uh_a0_spawn_plan, physical_surface_names
from tools.execution_fence import (
    build_approval_id,
    evaluate_tool_call,
    pretooluse_payload,
    read_current_turn_lease,
    UH_A0TurnRuntime,
    approval_prompt,
    write_current_turn_lease,
)
from tools.lease_signer import issue_turn_lease


class ExecutionFenceTests(unittest.TestCase):
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
        self._state_db_patch = patch.object(
            capability_state, "DB_PATH", str(self._db_path)
        )
        self._state_db_patch.start()
        self.addCleanup(self._state_db_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def lease(self, *, mode="chat", source="default_policy", requested=(),
              approvals=(), turn_id="turn-1"):
        args = dict(
            turn_id=turn_id, turn_mode=mode, issued_from=source,
            requested_capabilities=requested, approval_ids=approvals,
            issued_at="2026-08-12T00:00:00Z",
        )
        if source == "task_contract":
            args["task_contract_id"] = "task-1"
        return issue_turn_lease(**args)

    def test_a_chat_read_allowed(self):
        result = evaluate_tool_call(
            "mcp__home__search_memories", {"keyword": "昨天"}, self.lease()
        )
        self.assertEqual(result["capability_id"], "memory.search")
        self.assertEqual(result["lease_decision"], "ALLOW")

    def test_b_task_native_reads_allowed(self):
        lease = self.lease(mode="task")
        for tool, capability in (
            ("Read", "files.read"), ("Glob", "files.find"), ("Grep", "code.search")
        ):
            result = evaluate_tool_call(tool, {"path": "x"}, lease)
            self.assertEqual(result["capability_id"], capability)
            self.assertEqual(result["lease_decision"], "ALLOW")

    def test_b_external_read_is_chat_auto_allowed_without_approval(self):
        for tool, capability in (("WebSearch", "web.search"), ("WebFetch", "web.read")):
            result = evaluate_tool_call(tool, {"query": "x"}, self.lease())
            self.assertEqual(result["capability_id"], capability)
            self.assertEqual(result["lease_decision"], "ALLOW")
            self.assertNotIn("approval_id", result)

    def test_b_external_read_is_not_auto_enabled_for_wake(self):
        for tool in ("WebSearch", "WebFetch"):
            result = evaluate_tool_call(tool, {"query": "x"}, self.lease(mode="wake"))
            self.assertEqual(result["lease_decision"], "DENIED_CAPABILITY")

    def test_c_diary_chat_auto_allows_without_approval(self):
        result = evaluate_tool_call(
            "mcp__home__write_diary", {"content": "今天值得留下的一页"}, self.lease()
        )
        self.assertEqual(result["capability_id"], "diary.write")
        self.assertEqual(result["lease_decision"], "ALLOW")
        self.assertNotIn("approval_id", result)
        self.assertEqual(
            evaluate_tool_call(
                "mcp__home__write_diary",
                {"content": "Wake 不应写入"},
                self.lease(mode="wake"),
            )["lease_decision"],
            "DENIED_CAPABILITY",
        )

    def test_c_chat_native_read_denied(self):
        self.assertEqual(
            evaluate_tool_call("Read", {"file_path": "x"}, self.lease())["lease_decision"],
            "DENIED_CAPABILITY",
        )

    def test_d_ungranted_write_asks_with_action_id(self):
        action = {"content": "明天寄快递", "due_date": "2026-08-13"}
        result = evaluate_tool_call("mcp__home__add_todo", action, self.lease())
        self.assertEqual(result["capability_id"], "todo.write")
        self.assertEqual(result["lease_decision"], "CAPABILITY_ASK_REQUIRED")
        self.assertEqual(
            result["approval_id"],
            build_approval_id("todo.write", "mcp__home__add_todo", action),
        )

    def test_e_explicit_write_allows(self):
        lease = self.lease(source="explicit_user_intent", requested=("todo.write",))
        self.assertEqual(
            evaluate_tool_call(
                "mcp__home__add_todo", {"content": "明天寄快递"}, lease
            )["lease_decision"],
            "ALLOW",
        )

    def test_f_confirmation_exact_match_allows(self):
        action = {"content": "明天寄快递", "due_date": "2026-08-13"}
        asked = evaluate_tool_call("mcp__home__add_todo", action, self.lease())
        lease = self.lease(
            source="user_confirmation", requested=("todo.write",),
            approvals=(asked["approval_id"],), turn_id="turn-2",
        )
        self.assertEqual(
            evaluate_tool_call("mcp__home__add_todo", action, lease)["lease_decision"],
            "ALLOW",
        )

    def test_g_confirmation_changed_input_mismatches(self):
        asked = evaluate_tool_call(
            "mcp__home__add_todo",
            {"content": "明天寄快递", "due_date": "2026-08-13"},
            self.lease(),
        )
        lease = self.lease(
            source="user_confirmation", requested=("todo.write",),
            approvals=(asked["approval_id"],), turn_id="turn-2",
        )
        result = evaluate_tool_call(
            "mcp__home__add_todo",
            {"content": "明天买牛奶", "due_date": "2026-08-13"},
            lease,
        )
        self.assertEqual(result["lease_decision"], "LEASE_MISMATCH")

    def test_h_previous_turn_write_does_not_inherit(self):
        action = {"content": "明天寄快递"}
        old = self.lease(
            source="explicit_user_intent", requested=("todo.write",), turn_id="100"
        )
        self.assertEqual(
            evaluate_tool_call("mcp__home__add_todo", action, old)["lease_decision"],
            "ALLOW",
        )
        self.assertEqual(
            evaluate_tool_call(
                "mcp__home__add_todo", action, self.lease(turn_id="101")
            )["lease_decision"],
            "CAPABILITY_ASK_REQUIRED",
        )

    def test_i_missing_lease_fails_closed(self):
        self.assertEqual(
            evaluate_tool_call("Read", {}, None)["lease_decision"], "LEASE_MISMATCH"
        )
        broken = dict(self.lease())
        broken.pop("turn_id")
        self.assertEqual(
            evaluate_tool_call("Read", {}, broken)["lease_decision"], "LEASE_MISMATCH"
        )

    def test_j_unknown_and_reserved_denied(self):
        lease = self.lease()
        self.assertEqual(
            evaluate_tool_call("mcp__unknown__read", {}, lease)["lease_decision"],
            "DENIED_CAPABILITY",
        )
        self.assertEqual(
            evaluate_tool_call("Edit", {}, lease)["lease_decision"],
            "DENIED_CAPABILITY",
        )
        self.assertEqual(
            evaluate_tool_call("mcp__workspace__execute", {}, lease)["lease_decision"],
            "DENIED_CAPABILITY",
        )
        self.assertTrue(P1_RESERVED_CAPABILITY_IDS)

    def test_k_l_server_side_gate_has_zero_bypass_posts(self):
        action = {"content": "不应写入"}
        post_calls = []
        denied = evaluate_tool_call("mcp__home__add_todo", action, self.lease())
        if denied["lease_decision"] == "ALLOW":
            post_calls.append(action)
        self.assertEqual(denied["lease_decision"], "CAPABILITY_ASK_REQUIRED")
        self.assertEqual(post_calls, [])
        allowed = evaluate_tool_call(
            "mcp__home__add_todo", action,
            self.lease(source="explicit_user_intent", requested=("todo.write",)),
        )
        if allowed["lease_decision"] == "ALLOW":
            post_calls.append(action)
        self.assertEqual(post_calls, [action])

    def test_m_atomic_lease_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease.json"
            write_current_turn_lease(path, self.lease(turn_id="file"), session_id="cc")
            loaded, record = read_current_turn_lease(path)
            self.assertEqual(loaded["turn_id"], "file")
            self.assertEqual(record["session_id"], "cc")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_n_runtime_turn_100_rotates_and_clears_before_turn_101(self):
        action = {"content": "runtime-only todo"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease.json"
            runtime = UH_A0TurnRuntime(path, session_id="cc-runtime")

            turn100 = self.lease(
                source="explicit_user_intent",
                requested=("todo.write",),
                turn_id="100",
            )
            runtime.start_turn(turn100)
            self.assertEqual(
                runtime.evaluate("mcp__home__add_todo", action)["lease_decision"],
                "ALLOW",
            )
            self.assertTrue(runtime.end_turn(turn_id="100"))
            self.assertIsNone(read_current_turn_lease(path)[0])

            turn101 = self.lease(turn_id="101")
            runtime.start_turn(turn101)
            denied = runtime.evaluate("mcp__home__add_todo", action)
            self.assertEqual(denied["lease_decision"], "CAPABILITY_ASK_REQUIRED")
            deferred = runtime.deferred_tool_use("mcp__home__add_todo", action)
            self.assertEqual(deferred["lease_decision"], "CAPABILITY_ASK_REQUIRED")
            self.assertEqual(deferred["event"], "deferred_tool_use")
            self.assertEqual(deferred["tool_name"], "mcp__home__add_todo")
            self.assertEqual(deferred["tool_input"], action)
            self.assertEqual(
                deferred["approval_id"], denied["approval_id"],
            )
            self.assertEqual(
                deferred["approval_prompt"], "我顺手给你记进待办里？",
            )
            self.assertNotIn("是否授权", deferred["approval_prompt"])
            self.assertTrue(runtime.abort_turn(turn_id="101"))
            self.assertIsNone(read_current_turn_lease(path)[0])

    def test_p_ledger_confirmation_copy_is_concrete(self):
        self.assertEqual(
            approval_prompt("mcp__home__add_ledger", {"amount": -68}),
            "这笔 68 元要我一起记账吗？",
        )
        for forbidden in ("可以关心你吗？", "是否允许我帮助你？", "是否授权 todo.write？"):
            self.assertNotIn(
                forbidden,
                approval_prompt("mcp__home__add_todo", {"content": "x"}),
            )

    def test_o_confirmation_is_new_runtime_lease_and_exact_action_only(self):
        action = {"content": "68元晚饭", "amount": -68}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lease.json"
            runtime = UH_A0TurnRuntime(path, session_id="cc-runtime")
            turn100 = self.lease(turn_id="100")
            runtime.start_turn(turn100)
            asked = runtime.evaluate("mcp__home__add_ledger", action)
            self.assertEqual(asked["lease_decision"], "CAPABILITY_ASK_REQUIRED")
            runtime.end_turn(turn_id="100")

            confirmed = self.lease(
                source="user_confirmation",
                requested=("ledger.write",),
                approvals=(asked["approval_id"],),
                turn_id="101",
            )
            runtime.start_turn(confirmed)
            self.assertEqual(
                runtime.evaluate("mcp__home__add_ledger", action)["lease_decision"],
                "ALLOW",
            )
            changed = dict(action)
            changed["amount"] = -69
            self.assertEqual(
                runtime.evaluate("mcp__home__add_ledger", changed)["lease_decision"],
                "LEASE_MISMATCH",
            )
            runtime.end_turn(turn_id="101")

    def test_n_provider_mapping(self):
        for status, expected in (
            ("ALLOW", "allow"),
            ("DENIED_CAPABILITY", "deny"),
            ("LEASE_MISMATCH", "deny"),
            ("CAPABILITY_ASK_REQUIRED", "defer"),
        ):
            payload = pretooluse_payload(
                {"capability_id": "todo.write", "lease_decision": status}
            )
            self.assertEqual(
                payload["hookSpecificOutput"]["permissionDecision"], expected
            )

    def test_o_surface_is_generation_stable(self):
        first = self.lease(turn_id="a")
        second = self.lease(
            source="explicit_user_intent", requested=("todo.write",), turn_id="b"
        )
        with tempfile.TemporaryDirectory() as tmp:
            a = build_uh_a0_spawn_plan(cwd=tmp, turn_lease=first)
            b = build_uh_a0_spawn_plan(cwd=tmp, turn_lease=second)
            self.assertEqual(a["physical_surface_fingerprint"], b["physical_surface_fingerprint"])
            self.assertEqual(a["spawn_extra_args"], b["spawn_extra_args"])
        self.assertEqual(
            set(physical_surface_names()),
            {
                "Read", "Glob", "Grep", "WebSearch", "WebFetch",
                "mcp__home__search_memories", "mcp__home__write_diary",
                "mcp__home__get_light_status",
                "mcp__home__get_countdowns",
                "mcp__internal__get_ledger",
                "mcp__internal__get_ledger_budget", "mcp__internal__add_ledger",
                "mcp__internal__get_todos", "mcp__internal__add_todo",
            },
        )


if __name__ == "__main__":
    unittest.main()

