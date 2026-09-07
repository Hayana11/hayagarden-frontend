"""M3-04C Daily Memory Home -> Internal provider cutover contracts."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from tools import execution_fence
from tools.capability_manifest import get_capability
from tools.capability_state import RUNTIME_STATE_INHERIT, RUNTIME_STATE_OFF
from tools.cc_capability_adapter import (
    HOME_MCP_CAPABILITY_IDS,
    INTERNAL_MCP_CAPABILITY_IDS,
    INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
    build_uh_a0_spawn_plan,
    physical_surface_fingerprint,
    uh_a0_home_legacy_tools,
    uh_a0_home_mcp_tools,
    uh_a0_internal_mcp_tools,
    uh_a0_capability_proxy_tools,
)
from tools.cc_tool_surface import _static_schema_registry
import cc_resident
from wake.cc_tools import WAKE_TO_CC_MCP


HOME_MEMORY = "mcp__home__search_memories"
INTERNAL_MEMORY = "mcp__capability__memory_search"
LEGACY_INTERNAL_MEMORY = "mcp__internal__search_memories"
OLD_SURFACE = "fixture-old-surface"
NEW_SURFACE = "fixture-new-surface"


class DailyMemoryCutoverTests(unittest.TestCase):
    def plan(self, state=RUNTIME_STATE_INHERIT):
        with tempfile.TemporaryDirectory() as root:
            with patch("tools.cc_capability_adapter.read_capability_state", return_value=state):
                return build_uh_a0_spawn_plan(cwd=root, write_mcp_config=False, env={})

    def test_manifest_grouping_and_loading_are_exact(self):
        self.assertEqual(
            get_capability("memory.search")["provider_bindings"],
            {
                "claude_code": INTERNAL_MEMORY,
                "internal_mcp": LEGACY_INTERNAL_MEMORY,
                "home_mcp": HOME_MEMORY,
            },
        )
        self.assertEqual(HOME_MCP_CAPABILITY_IDS, ("countdown.read",))
        self.assertEqual(INTERNAL_MCP_CAPABILITY_IDS, ())
        self.assertEqual(
            INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
            (
                "mcp__internal__get_ledger",
                "mcp__internal__get_ledger_budget",
                "mcp__internal__get_todos",
                "mcp__internal__search_memories",
                "mcp__internal__write_memory",
                "mcp__internal__add_todo",
                "mcp__internal__add_ledger",
            ),
        )
        item = get_capability("memory.search")
        self.assertEqual(item["kind"], "read")
        self.assertEqual(item["side_effect"], "none")
        self.assertEqual(item["autonomy_mode"], "read_auto")
        self.assertEqual(item["loading_policy"], "always_load")

    def test_normal_surface_has_only_internal_memory(self):
        plan = self.plan()
        self.assertEqual(set(uh_a0_home_mcp_tools()), {"mcp__home__get_countdowns"})
        self.assertIn(INTERNAL_MEMORY, uh_a0_capability_proxy_tools())
        self.assertIn(INTERNAL_MEMORY, plan["surface_allowlist"])
        self.assertNotIn(HOME_MEMORY, plan["surface_allowlist"])
        self.assertIn(HOME_MEMORY, plan["disallowed_tools"])
        self.assertNotIn(INTERNAL_MEMORY, plan["disallowed_tools"])
        self.assertIn(LEGACY_INTERNAL_MEMORY, plan["disallowed_tools"])
        current = physical_surface_fingerprint()
        self.assertEqual(plan["physical_surface_fingerprint"], current)
        self.assertEqual(physical_surface_fingerprint(), current)

    def test_home_legacy_and_fence_lookup_remain_explicit(self):
        self.assertEqual(uh_a0_home_legacy_tools()[-1], "mcp__home__get_light_status")
        self.assertEqual(execution_fence.capability_for_tool(INTERNAL_MEMORY), "memory.search")
        self.assertEqual(execution_fence.capability_for_tool(LEGACY_INTERNAL_MEMORY), "memory.search")
        self.assertEqual(execution_fence.capability_for_tool(HOME_MEMORY), "memory.search")
        self.assertEqual(WAKE_TO_CC_MCP["search_memories"], HOME_MEMORY)
        registry = _static_schema_registry()
        self.assertEqual(registry[INTERNAL_MEMORY], registry[HOME_MEMORY])
        self.assertEqual(registry[INTERNAL_MEMORY], registry[LEGACY_INTERNAL_MEMORY])

    def test_runtime_off_denies_both_memory_providers(self):
        plan = self.plan(RUNTIME_STATE_OFF)
        self.assertNotIn(INTERNAL_MEMORY, plan["surface_allowlist"])
        self.assertNotIn(HOME_MEMORY, plan["surface_allowlist"])
        self.assertIn(INTERNAL_MEMORY, plan["disallowed_tools"])
        self.assertIn(LEGACY_INTERNAL_MEMORY, plan["disallowed_tools"])
        self.assertIn(HOME_MEMORY, plan["disallowed_tools"])

    def test_generation_change_is_lazy(self):
        resident = cc_resident.ResidentSession(
            cwd=tempfile.mkdtemp(),
            allowed_tools="",
            mcp_config_path="",
        )

        class Alive:
            def poll(self):
                return None

        resident._proc = Alive()
        resident._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
        resident._system_text = "same-system"
        resident._history_rewrite_epoch = "test-epoch"
        resident._bound_tool_surface_fingerprint = OLD_SURFACE
        before = resident.generation
        with patch("chat.cc_history_rewrite.current_history_rewrite_epoch", return_value="test-epoch"), patch(
            "chat.cc_history_rewrite.is_unreadable_epoch", return_value=False
        ), patch(
            "tools.cc_capability_adapter.physical_surface_fingerprint",
            return_value=NEW_SURFACE,
        ):
            self.assertEqual(
                resident.peek_respawn_reason(
                    "same-system",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "tool_surface_changed",
            )
        self.assertEqual(resident.generation, before)


if __name__ == "__main__":
    unittest.main()
