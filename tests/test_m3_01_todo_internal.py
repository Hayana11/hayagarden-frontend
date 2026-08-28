"""M3-01 Todo Internal Adapter and provider-neutral fence contracts."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import execution_fence
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import build_uh_a0_spawn_plan
from tools.lease_signer import issue_turn_lease
from tools.product_handlers import create_todo, list_todos
from tools.todo_internal_adapter import add_todo, read_todos
from wake.cc_tools import WAKE_TO_CC_MCP, cc_wake_allowed_tools


def make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE todos ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "content TEXT NOT NULL, done INTEGER DEFAULT 0, due_date TEXT, "
        "author TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO todos (content, done, due_date, author, created_at) "
        "VALUES (?,?,?,?,?)",
        [
            ("无日期未完成", 0, None, "alice", "2026-08-01"),
            ("明天完成", 0, "2026-08-22", "fyodor_api", "2026-08-02"),
            ("早先完成", 1, "2026-08-01", "alice", "2026-08-03"),
            ("最近完成", 1, "2026-08-02", "fyodor_api", "2026-08-04"),
            ("应被限制掉", 1, "2026-08-03", "alice", "2026-08-05"),
            ("另一个应被限制掉", 1, "2026-08-04", "alice", "2026-08-06"),
        ],
    )
    conn.commit()
    conn.close()


class TodoInternalAdapterTests(unittest.TestCase):
    def lease(self, *, source="default_policy", requested=(), approvals=(), mode="chat"):
        return issue_turn_lease(
            turn_id="m3-01-turn",
            turn_mode=mode,
            issued_from=source,
            requested_capabilities=requested,
            approval_ids=approvals,
            issued_at="2026-08-21T00:00:00Z",
        )

    def test_manifest_shadow_binding_preserves_live_binding(self):
        self.assertEqual(
            get_capability("todo.read")["provider_bindings"],
            {
                "claude_code": "mcp__capability__todo_read",
                "internal_mcp": "mcp__internal__get_todos",
                "home_mcp": "mcp__home__get_todos",
                "api_relay": "get_todos",
            },
        )
        self.assertEqual(
            get_capability("todo.write")["provider_bindings"],
            {
                "claude_code": "mcp__capability__todo_write",
                "internal_mcp": "mcp__internal__add_todo",
                "home_mcp": "mcp__home__add_todo",
                "api_relay": "add_todo",
            },
        )

    def test_provider_neutral_fence_maps_home_and_internal(self):
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__home__get_todos"),
            "todo.read",
        )
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__internal__get_todos"),
            "todo.read",
        )
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__capability__todo_read"),
            "todo.read",
        )
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__capability__todo_write"),
            "todo.write",
        )
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__home__add_todo"),
            "todo.write",
        )
        self.assertEqual(
            execution_fence.capability_for_tool("mcp__internal__add_todo"),
            "todo.write",
        )

    def test_binding_collision_is_rejected(self):
        duplicate = {
            **get_capability("todo.read"),
            "capability_id": "fake.read",
            "provider_bindings": {"internal_mcp": "mcp__internal__get_todos"},
        }
        with patch.object(
            execution_fence,
            "CAPABILITY_MANIFEST",
            execution_fence.CAPABILITY_MANIFEST + (duplicate,),
        ):
            with self.assertRaises(ValueError):
                execution_fence.tool_capability_index()

    def test_approval_prompt_is_provider_neutral_but_ids_are_not(self):
        action = {"content": "明天寄快递", "due_date": "2026-08-22"}
        home_id = execution_fence.build_approval_id(
            "todo.write", "mcp__home__add_todo", action
        )
        internal_id = execution_fence.build_approval_id(
            "todo.write", "mcp__internal__add_todo", action
        )
        self.assertNotEqual(home_id, internal_id)
        self.assertEqual(
            execution_fence.approval_prompt("mcp__home__add_todo", action),
            "我顺手给你记进待办里？",
        )
        self.assertEqual(
            execution_fence.approval_prompt("mcp__internal__add_todo", action),
            "我顺手给你记进待办里？",
        )
        direct = execution_fence.evaluate_tool_call(
            "mcp__internal__add_todo",
            action,
            self.lease(
                source="explicit_user_intent",
                requested=("todo.write",),
            ),
        )
        self.assertEqual(direct["lease_decision"], "ALLOW")
        self.assertNotIn("approval_id", direct)
        confirmed = self.lease(
            source="user_confirmation",
            requested=("todo.write",),
            approvals=(internal_id,),
        )
        self.assertEqual(
            execution_fence.evaluate_tool_call(
                "mcp__internal__add_todo", action, confirmed
            )["lease_decision"],
            "ALLOW",
        )
        wrong = self.lease(
            source="user_confirmation",
            requested=("todo.write",),
            approvals=(home_id,),
        )
        self.assertEqual(
            execution_fence.evaluate_tool_call(
                "mcp__internal__add_todo", action, wrong
            )["lease_decision"],
            "LEASE_MISMATCH",
        )

    def test_runtime_off_denies_both_todo_providers(self):
        read_lease = self.lease()
        write_lease = self.lease(
            source="explicit_user_intent", requested=("todo.write",)
        )
        with patch.object(execution_fence, "read_capability_state", return_value="OFF"):
            for name, lease in (
                ("mcp__home__get_todos", read_lease),
                ("mcp__internal__get_todos", read_lease),
                ("mcp__home__add_todo", write_lease),
                ("mcp__internal__add_todo", write_lease),
            ):
                self.assertEqual(
                    execution_fence.evaluate_tool_call(name, {}, lease)["lease_decision"],
                    "DENIED_CAPABILITY",
                    name,
                )

    def test_read_equivalence_uses_shared_handler_and_recent_done_limit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "todos.db"
            make_db(path)
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                expected = {"todos": list_todos(conn)}
            self.assertEqual(read_todos(path), expected)

    def test_write_equivalence_isolated_and_single_insert(self):
        with tempfile.TemporaryDirectory() as root:
            home_path = Path(root) / "home.db"
            internal_path = Path(root) / "internal.db"
            make_db(home_path)
            make_db(internal_path)
            with sqlite3.connect(home_path) as conn:
                home_result = create_todo(
                    conn,
                    content="新待办",
                    due_date="2026-08-23",
                    author="fyodor_api",
                )
            internal_result = add_todo(
                internal_path,
                content="新待办",
                due_date="2026-08-23",
            )
            self.assertEqual(internal_result, {"ok": True})
            self.assertEqual(
                home_result["ok"],
                internal_result["ok"],
            )
            with sqlite3.connect(home_path) as home_conn, sqlite3.connect(internal_path) as internal_conn:
                home_conn.row_factory = sqlite3.Row
                internal_conn.row_factory = sqlite3.Row
                self.assertEqual(
                    dict(home_conn.execute(
                        "SELECT content, due_date, done, author FROM todos ORDER BY id DESC LIMIT 1"
                    ).fetchone()),
                    dict(internal_conn.execute(
                        "SELECT content, due_date, done, author FROM todos ORDER BY id DESC LIMIT 1"
                    ).fetchone()),
                )
                self.assertEqual(
                    home_conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0],
                    7,
                )
                self.assertEqual(
                    internal_conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0],
                    7,
                )

    def test_chat_and_wake_leases_allow_todo_read_without_wake_surface_change(self):
        chat_lease = self.lease(mode="chat")
        wake_lease = self.lease(mode="wake")
        for lease in (chat_lease, wake_lease):
            self.assertEqual(
                execution_fence.evaluate_tool_call(
                    "mcp__capability__todo_read", {}, lease
                )["lease_decision"],
                "ALLOW",
            )

        chat_plan = build_uh_a0_spawn_plan(
            write_mcp_config=False, turn_lease=chat_lease, env={}
        )
        wake_plan = build_uh_a0_spawn_plan(
            write_mcp_config=False, turn_lease=wake_lease, env={}
        )
        self.assertEqual(chat_plan["surface_allowlist"], wake_plan["surface_allowlist"])
        self.assertEqual(
            chat_plan["physical_surface_fingerprint"],
            wake_plan["physical_surface_fingerprint"],
        )
        self.assertIn("mcp__capability__todo_read", chat_plan["surface_allowlist"])
        self.assertNotIn("mcp__capability__todo_read", cc_wake_allowed_tools())
        self.assertEqual(WAKE_TO_CC_MCP["get_todos"], "mcp__home__get_todos")

    def test_capability_proxy_todo_read_is_typed_and_reuses_read_adapter(self):
        root = Path(__file__).resolve().parents[1]
        proxy_source = (root / "capability-proxy-mcp-server.js").read_text(encoding="utf-8")
        adapter_source = (root / "tools" / "todo_internal_adapter.py").read_text(encoding="utf-8")
        self.assertIn("todo_read: 'tools.todo_internal_adapter'", proxy_source)
        self.assertIn("server.tool(\n    'todo_read',\n    {},", proxy_source)
        self.assertIn("? 'get_todos'", proxy_source)
        self.assertNotIn("SELECT", proxy_source)
        self.assertIn('operation == "get_todos"', adapter_source)

    def test_live_surface_uses_capability_todo_read_and_forbids_home_todo(self):
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertEqual(set(plan["mcp_config"]["mcpServers"]), {"home", "internal", "capability"})
        self.assertIn("mcp__capability__todo_read", plan["surface_allowlist"])
        self.assertNotIn("mcp__internal__get_todos", plan["surface_allowlist"])
        self.assertIn("mcp__internal__get_todos", plan["disallowed_tools"])
        self.assertIn("mcp__capability__todo_write", plan["surface_allowlist"])
        self.assertNotIn("mcp__home__get_todos", plan["surface_allowlist"])
        self.assertNotIn("mcp__home__add_todo", plan["surface_allowlist"])
        self.assertIn("mcp__home__get_todos", plan["disallowed_tools"])
        self.assertIn("mcp__home__add_todo", plan["disallowed_tools"])


if __name__ == "__main__":
    unittest.main()
