"""M3-02B Daily UH-A0 Todo provider cutover contracts."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tools import execution_fence
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import build_uh_a0_spawn_plan, physical_surface_fingerprint, physical_surface_names
from tools.cc_tool_surface import _static_schema_registry
from tools.capability_state import RUNTIME_STATE_INHERIT, RUNTIME_STATE_OFF

BASE_FINGERPRINT = "4e5e630e8266874f8f5c99bda243647a300793d24ead0af1fe0a97fa22e11df0"

class M302BTodoCutoverTests(unittest.TestCase):
    def plan(self, state=RUNTIME_STATE_INHERIT):
        with tempfile.TemporaryDirectory() as root:
            with patch("tools.cc_capability_adapter.read_capability_state", return_value=state):
                return build_uh_a0_spawn_plan(cwd=root, write_mcp_config=False, env={})

    def test_manifest_bindings(self):
        self.assertEqual(get_capability("todo.read")["provider_bindings"], {"claude_code": "mcp__internal__get_todos", "internal_mcp": "mcp__internal__get_todos", "home_mcp": "mcp__home__get_todos"})
        self.assertEqual(get_capability("todo.write")["provider_bindings"], {"claude_code": "mcp__internal__add_todo", "internal_mcp": "mcp__internal__add_todo", "home_mcp": "mcp__home__add_todo"})

    def test_config_and_surface(self):
        plan = self.plan()
        servers = plan["mcp_config"]["mcpServers"]
        self.assertEqual(set(servers), {"home", "internal"})
        self.assertEqual(servers["home"]["url"], "http://127.0.0.1:3100/mcp")
        self.assertEqual(servers["internal"]["url"], "http://127.0.0.1:3101/mcp")
        self.assertEqual(servers["internal"]["headers"], {"X-UH-A0-Profile": "uh_a0"})
        allowed = set(plan["surface_allowlist"])
        disallowed = set(plan["disallowed_tools"])
        self.assertIn("mcp__internal__get_todos", allowed)
        self.assertIn("mcp__internal__add_todo", allowed)
        self.assertNotIn("mcp__home__get_todos", allowed)
        self.assertNotIn("mcp__home__add_todo", allowed)
        self.assertIn("mcp__home__get_todos", disallowed)
        self.assertIn("mcp__home__add_todo", disallowed)
        self.assertEqual(set(plan["claude_visible_mcp_tools"]), {"mcp__internal__get_todos", "mcp__internal__add_todo", "mcp__home__search_memories", "mcp__home__write_diary", "mcp__home__get_light_status", "mcp__home__get_countdowns", "mcp__internal__get_ledger", "mcp__internal__get_ledger_budget", "mcp__internal__add_ledger"})

    def test_runtime_off_hides_both_provider_surfaces(self):
        plan = self.plan(RUNTIME_STATE_OFF)
        allowed = set(plan["surface_allowlist"])
        disallowed = set(plan["disallowed_tools"])
        self.assertNotIn("mcp__internal__get_todos", allowed)
        self.assertNotIn("mcp__internal__add_todo", allowed)
        self.assertNotIn("mcp__home__get_todos", allowed)
        self.assertNotIn("mcp__home__add_todo", allowed)
        self.assertIn("mcp__internal__get_todos", disallowed)
        self.assertIn("mcp__internal__add_todo", disallowed)
        self.assertIn("mcp__home__get_todos", disallowed)
        self.assertIn("mcp__home__add_todo", disallowed)

    def test_fingerprint_and_names(self):
        with patch("tools.cc_capability_adapter.read_capability_state", return_value=RUNTIME_STATE_INHERIT):
            first = physical_surface_fingerprint()
            self.assertEqual(first, physical_surface_fingerprint())
            self.assertNotEqual(first, BASE_FINGERPRINT)
            self.assertIn("mcp__internal__get_todos", physical_surface_names())
            self.assertNotIn("mcp__home__get_todos", physical_surface_names())

    def test_fence_and_approval_identity(self):
        self.assertEqual(execution_fence.capability_for_tool("mcp__home__add_todo"), "todo.write")
        self.assertEqual(execution_fence.capability_for_tool("mcp__internal__add_todo"), "todo.write")
        action = {"content": "cutover"}
        home_id = execution_fence.build_approval_id("todo.write", "mcp__home__add_todo", action)
        internal_id = execution_fence.build_approval_id("todo.write", "mcp__internal__add_todo", action)
        self.assertNotEqual(home_id, internal_id)
        self.assertEqual(execution_fence.approval_prompt("mcp__internal__add_todo", action), "我顺手给你记进待办里？")

    def test_resident_drift_is_lazy(self):
        import cc_resident
        class AliveProcess:
            def poll(self): return None
        with tempfile.TemporaryDirectory() as root:
            resident = cc_resident.ResidentSession(cwd=root, allowed_tools="", mcp_config_path=str(Path(root) / "legacy.json"))
            resident._proc = AliveProcess()
            resident._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            resident._system_text = "same-system"
            resident._history_rewrite_epoch = "test-epoch"
            resident._bound_tool_surface_fingerprint = "base"
            resident._model_identity = None
            before = resident.generation
            with patch("chat.cc_history_rewrite.current_history_rewrite_epoch", return_value="test-epoch"), patch("chat.cc_history_rewrite.is_unreadable_epoch", return_value=False), patch("tools.cc_capability_adapter.physical_surface_fingerprint", return_value="head"):
                self.assertEqual(resident.peek_respawn_reason("same-system", tool_profile=cc_resident.TOOL_PROFILE_UH_A0), "tool_surface_changed")
            self.assertEqual(resident.generation, before)

    def test_internal_schema_matches_home(self):
        registry = _static_schema_registry()
        self.assertEqual(registry["mcp__internal__get_todos"], registry["mcp__home__get_todos"])
        self.assertEqual(registry["mcp__internal__add_todo"], registry["mcp__home__add_todo"])

if __name__ == "__main__":
    unittest.main()
