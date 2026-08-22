"""Focused M4-01A API Relay get_todos lease/fence contracts."""
from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import capability_state, execution_fence
from tools.capability_manifest import get_capability
from tools.execution_fence import capability_for_tool, evaluate_tool_call
from tools.lease_signer import issue_turn_lease


ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PATH = ROOT / "gateway.py"


def _calls(node, name):
    return [
        item
        for item in ast.walk(node)
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == name
    ]


def _route_function(tree, route):
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "route"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == route
            ):
                return node
    raise AssertionError(f"route function not found: {route}")


def _gateway_functions(*names):
    tree = ast.parse(GATEWAY_PATH.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in selected} != set(names):
        raise AssertionError(f"gateway functions missing: {names}")
    namespace = {"run_tool": lambda name, args: None}
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(GATEWAY_PATH), "exec"), namespace)
    return namespace


class M401AApiTodoReadTests(unittest.TestCase):
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

    @staticmethod
    def _lease(turn_id="m4-01a-turn"):
        return issue_turn_lease(
            turn_id=turn_id,
            turn_mode="chat",
            issued_from="default_policy",
            issued_at="2026-08-22T00:00:00Z",
        )

    def test_binding_default_policy_and_todo_write_scope(self):
        self.assertEqual(capability_for_tool("get_todos"), "todo.read")
        self.assertIsNone(capability_for_tool("add_todo"))
        self.assertEqual(
            capability_for_tool("mcp__internal__add_todo"), "todo.write"
        )
        self.assertEqual(
            get_capability("todo.read")["provider_bindings"]["api_relay"],
            "get_todos",
        )
        self.assertNotIn(
            "api_relay",
            get_capability("todo.write")["provider_bindings"],
        )
        decision = evaluate_tool_call("get_todos", {}, self._lease())
        self.assertEqual(decision["capability_id"], "todo.read")
        self.assertEqual(decision["turn_mode"], "chat")
        self.assertEqual(decision["lease_decision"], "ALLOW")

    def test_api_turn_lease_helper_uses_one_default_chat_lease(self):
        helpers = _gateway_functions("_issue_api_chat_turn_lease")
        lease = object()
        with patch(
            "tools.lease_signer.issue_turn_lease", return_value=lease
        ) as issue:
            self.assertIs(
                helpers["_issue_api_chat_turn_lease"]("  turn-42  "),
                lease,
            )
        issue.assert_called_once_with(
            turn_id="turn-42",
            turn_mode="chat",
            issued_from="default_policy",
        )

    def test_dispatch_allows_frontend_only_after_allow(self):
        frontend_calls = []
        helpers = _gateway_functions("_dispatch_api_chat_tool")
        helpers["run_tool"] = lambda name, args: frontend_calls.append((name, args)) or "ok"

        result = helpers["_dispatch_api_chat_tool"](
            "get_todos", {"status": "open"}, self._lease()
        )
        self.assertEqual(result, "ok")
        self.assertEqual(frontend_calls, [("get_todos", {"status": "open"})])

    def test_missing_malformed_off_and_storage_failure_never_call_frontend(self):
        frontend_calls = []
        helpers = _gateway_functions("_dispatch_api_chat_tool")
        helpers["run_tool"] = lambda name, args: frontend_calls.append((name, args)) or "unexpected"

        malformed = dict(self._lease())
        malformed.pop("turn_id")
        for lease in (None, malformed):
            result = helpers["_dispatch_api_chat_tool"]("get_todos", {}, lease)
            self.assertIn("工具执行失败：LEASE_MISMATCH", result)
        self.assertEqual(frontend_calls, [])

        capability_state.set_capability_state("todo.read", enabled=False)
        result = helpers["_dispatch_api_chat_tool"]("get_todos", {}, self._lease())
        self.assertIn("工具执行失败：DENIED_CAPABILITY", result)
        self.assertEqual(frontend_calls, [])

        with patch.object(
            execution_fence,
            "read_capability_state",
            side_effect=capability_state.CapabilityStateError("unavailable"),
        ):
            result = helpers["_dispatch_api_chat_tool"]("get_todos", {}, self._lease())
        self.assertIn("工具执行失败：DENIED_CAPABILITY", result)
        self.assertEqual(frontend_calls, [])

    def test_non_get_todos_legacy_tool_bypasses_new_fence(self):
        frontend_calls = []
        helpers = _gateway_functions("_dispatch_api_chat_tool")
        helpers["run_tool"] = lambda name, args: frontend_calls.append((name, args)) or "legacy-ok"
        with patch.object(
            execution_fence,
            "evaluate_tool_call",
            side_effect=AssertionError("legacy tool entered API fence"),
        ):
            result = helpers["_dispatch_api_chat_tool"](
                "legacy_tool", {"value": 1}, self._lease()
            )
        self.assertEqual(result, "legacy-ok")
        self.assertEqual(frontend_calls, [("legacy_tool", {"value": 1})])

    def test_normal_agent_loop_reuses_one_lease_across_tool_rounds(self):
        helpers = _gateway_functions("generate_reply", "agent_loop")
        responses = iter(
            [
                {
                    "content": [
                        {"type": "text", "text": "round one"},
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "get_todos",
                            "input": {},
                        },
                    ],
                    "stop_reason": "tool_use",
                },
                {
                    "content": [
                        {"type": "text", "text": "round two"},
                        {
                            "type": "tool_use",
                            "id": "tool-2",
                            "name": "legacy_tool",
                            "input": {"x": 1},
                        },
                    ],
                    "stop_reason": "tool_use",
                },
                {
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                },
            ]
        )
        observed_leases = []

        def api_call(system, messages):
            return next(responses)

        def dispatch(name, args, lease):
            observed_leases.append(lease)
            return f"result:{name}"

        helpers.update(
            {
                "_get_provider": lambda: "api_relay",
                "api_call": api_call,
                "_dispatch_api_chat_tool": dispatch,
                "NL": "\n",
                "re": re,
            }
        )
        lease = object()
        text, thinking = helpers["generate_reply"](
            "system", [], api_turn_lease=lease
        )
        self.assertIn("done", text)
        self.assertEqual(len(observed_leases), 2)
        self.assertTrue(all(item is lease for item in observed_leases))
        self.assertEqual(thinking, "")

    def test_normal_and_streaming_api_wiring_is_narrow_and_single_issue(self):
        tree = ast.parse(GATEWAY_PATH.read_text(encoding="utf-8"))
        normal_chat = _route_function(tree, "/chat")
        stream_route = _route_function(tree, "/chat/stream")
        normal_agent_loop = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "agent_loop"
        )
        normal_generate_reply = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "generate_reply"
        )
        stream_generates = [
            node
            for node in ast.walk(stream_route)
            if isinstance(node, ast.FunctionDef) and node.name == "generate"
        ]
        self.assertEqual(len(stream_generates), 1)
        stream_generate = stream_generates[0]

        self.assertEqual(len(_calls(normal_chat, "_issue_api_chat_turn_lease")), 1)
        generate_calls = _calls(normal_chat, "generate_reply")
        self.assertEqual(len(generate_calls), 1)
        self.assertIn(
            "api_turn_lease",
            {keyword.arg for keyword in generate_calls[0].keywords},
        )
        self.assertEqual(
            len(_calls(normal_agent_loop, "_dispatch_api_chat_tool")), 1
        )
        self.assertEqual(len(_calls(normal_agent_loop, "run_tool")), 0)

        stream_lease_calls = _calls(stream_generate, "_issue_api_chat_turn_lease")
        stream_dispatch_calls = _calls(stream_generate, "_dispatch_api_chat_tool")
        activate_calls = _calls(stream_generate, "activate_turn")
        self.assertEqual(len(stream_lease_calls), 1)
        self.assertEqual(len(stream_dispatch_calls), 1)
        self.assertEqual(len(activate_calls), 1)
        self.assertGreater(stream_lease_calls[0].lineno, activate_calls[0].lineno)
        self.assertEqual(len(_calls(stream_generate, "run_tool")), 0)


if __name__ == "__main__":
    unittest.main()
