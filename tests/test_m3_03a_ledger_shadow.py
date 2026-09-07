"""M3-03A Ledger Internal shadow and Daily isolation contracts."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import execution_fence
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import (
    INTERNAL_MCP_CAPABILITY_IDS,
    INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
    build_uh_a0_spawn_plan,
    physical_surface_fingerprint,
)
from tools.capability_state import RUNTIME_STATE_INHERIT
from wake.cc_tools import WAKE_TO_CC_MCP


INTERNAL_MEMORY_SHADOW_TOOLS = (
    "mcp__internal__search_memories",
    "mcp__internal__write_memory",
    "mcp__internal__add_todo",
    "mcp__internal__add_ledger",
)
LEDGER_HOME_TOOLS = (
    "mcp__home__get_ledger",
    "mcp__home__get_ledger_budget",
    "mcp__home__add_ledger",
)
INTERNAL_LEDGER_TOOLS = ()
CAPABILITY_LEDGER_TOOLS = (
    "mcp__capability__ledger_read",
    "mcp__capability__ledger_budget_read",
    "mcp__capability__ledger_write",
)
INTERNAL_LEDGER_SHADOW_TOOLS = ("mcp__internal__get_ledger", "mcp__internal__get_ledger_budget")
EXPECTED_INTERNAL_MCP_SHADOW_TOOLS = (
    "mcp__internal__get_ledger",
    "mcp__internal__get_ledger_budget",
    "mcp__internal__get_todos",
    "mcp__internal__search_memories",
    "mcp__internal__write_memory",
    "mcp__internal__add_todo",
    "mcp__internal__add_ledger",
)


class LedgerInternalShadowTests(unittest.TestCase):
    def test_manifest_and_fence_bindings(self):
        self.assertEqual(
            INTERNAL_MCP_CAPABILITY_IDS,
            (),
        )
        self.assertEqual(
            get_capability("ledger.read")["provider_bindings"],
            {
                "claude_code": "mcp__capability__ledger_read",
                "internal_mcp": "mcp__internal__get_ledger",
                "home_mcp": "mcp__home__get_ledger",
            },
        )
        self.assertEqual(
            get_capability("ledger.budget.read")["provider_bindings"],
            {
                "claude_code": "mcp__capability__ledger_budget_read",
                "internal_mcp": "mcp__internal__get_ledger_budget",
                "home_mcp": "mcp__home__get_ledger_budget",
            },
        )
        self.assertEqual(
            get_capability("ledger.write")["provider_bindings"],
            {
                "claude_code": "mcp__capability__ledger_write",
                "internal_mcp": "mcp__internal__add_ledger",
                "home_mcp": "mcp__home__add_ledger",
            },
        )
        self.assertEqual(
            get_capability("memory.search")["provider_bindings"],
            {
                "claude_code": "mcp__capability__memory_search",
                "internal_mcp": "mcp__internal__search_memories",
                "home_mcp": "mcp__home__search_memories",
            },
        )
        self.assertEqual(execution_fence.capability_for_tool("mcp__internal__search_memories"), "memory.search")
        self.assertEqual(execution_fence.capability_for_tool("mcp__home__search_memories"), "memory.search")
        for tool_name, capability_id in (
            ("mcp__capability__ledger_read", "ledger.read"),
            ("mcp__internal__get_ledger", "ledger.read"),
            ("mcp__internal__get_ledger_budget", "ledger.budget.read"),
            ("mcp__internal__add_ledger", "ledger.write"),
        ):
            self.assertEqual(execution_fence.capability_for_tool(tool_name), capability_id)
        self.assertEqual(
            execution_fence.approval_prompt(
                "mcp__internal__add_ledger", {"amount": -12}
            ),
            "这笔 12 元要我一起记账吗？",
        )

    def test_daily_shadow_surface_is_deterministic_without_cutover(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch(
                "tools.cc_capability_adapter.read_capability_state",
                return_value=RUNTIME_STATE_INHERIT,
            ):
                plan = build_uh_a0_spawn_plan(
                    cwd=temp,
                    write_mcp_config=False,
                    env={},
                )
                first = physical_surface_fingerprint()
                second = physical_surface_fingerprint()

        self.assertEqual(
            tuple(INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS),
            EXPECTED_INTERNAL_MCP_SHADOW_TOOLS,
        )
        self.assertIn("mcp__internal__get_ledger", INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS)
        current = physical_surface_fingerprint()
        self.assertEqual(first, current)
        self.assertEqual(second, current)
        self.assertEqual(first, second)
        allowed = set(plan["surface_allowlist"])
        disallowed = set(plan["disallowed_tools"])
        self.assertIn("mcp__internal__get_ledger", disallowed)
        self.assertIn("mcp__internal__get_ledger_budget", disallowed)
        self.assertNotIn("mcp__internal__get_ledger", allowed)
        self.assertNotIn("mcp__internal__get_ledger_budget", allowed)
        self.assertTrue(set(CAPABILITY_LEDGER_TOOLS).issubset(allowed))
        self.assertTrue(set(INTERNAL_LEDGER_SHADOW_TOOLS).issubset(disallowed))
        self.assertTrue(set(INTERNAL_LEDGER_SHADOW_TOOLS).isdisjoint(allowed))
        self.assertTrue(set(LEDGER_HOME_TOOLS).issubset(disallowed))
        self.assertIn("mcp__capability__memory_search", allowed)
        self.assertNotIn("mcp__home__search_memories", allowed)
        self.assertIn("mcp__home__search_memories", disallowed)
        self.assertIn("mcp__internal__search_memories", disallowed)
        self.assertNotIn("mcp__internal__get_todos", allowed)
        self.assertIn("mcp__internal__get_todos", disallowed)
        self.assertIn("mcp__internal__add_ledger", disallowed)
        self.assertNotIn("mcp__home__get_todos", allowed)
        self.assertEqual(WAKE_TO_CC_MCP["get_ledger"], "mcp__home__get_ledger")
        self.assertEqual(WAKE_TO_CC_MCP["add_ledger"], "mcp__home__add_ledger")

    def test_adapter_is_handler_only_and_path_explicit(self):
        source = Path("tools/ledger_internal_adapter.py").read_text(encoding="utf-8")
        self.assertIn("from tools.product_handlers import", source)
        self.assertIn("db_path", source)
        self.assertNotIn("TODO_INTERNAL_DB_PATH", source)
        self.assertNotIn("/opt/frontend/memories.db", source)
        self.assertIsNone(re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", source))


if __name__ == "__main__":
    unittest.main()
