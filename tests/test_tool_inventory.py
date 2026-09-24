import unittest
from pathlib import Path
from unittest import mock

from tools import tool_companion_hints
from tools import tool_inventory
from tools.cc_capability_adapter import physical_surface_names as current_physical_surface_names
from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS, get_capability


class ToolInventoryTest(unittest.TestCase):
    def setUp(self):
        bindings = {
            (get_capability(capability_id) or {}).get("provider_bindings", {}).get("claude_code")
            for capability_id in P1_ENABLED_CAPABILITY_IDS
        }
        bindings.discard(None)
        # B1 registers health.read, while its physical Claude tool is still unpublished.
        bindings.discard("mcp__internal__get.health")
        self.surface_patch = mock.patch(
            "tools.tool_inventory.physical_surface_names",
            return_value=tuple(sorted(bindings)),
        )
        self.surface_patch.start()
        self.addCleanup(self.surface_patch.stop)
        self.p = tool_inventory.payload()
        self.groups = {group["id"]: group for group in self.p["groups"]}
        self.t = {
            tool["tool_name"]: tool
            for group in self.p["groups"]
            for tool in group["tools"]
        }

    def test_current_groups_derive_from_companion_catalog(self):
        expected_ids = [group_id for group_id, _, _ in tool_companion_hints._EXPECTED_GROUPS]
        self.assertEqual(expected_ids, ["memory", "home", "plans", "health", "ledger", "files", "external_read"])
        self.assertEqual([group["id"] for group in self.p["groups"][:len(tool_companion_hints._EXPECTED_GROUPS)]], expected_ids)
        self.assertEqual(self.groups["plans"]["label"], "计划")
        self.assertEqual(
            {
                tool["tool_name"]
                for group in self.p["groups"][:len(tool_companion_hints._EXPECTED_GROUPS)]
                for tool in group["tools"]
            },
            set(P1_ENABLED_CAPABILITY_IDS),
        )
        self.assertIn("health.read", self.t)
        self.assertFalse(self.t["health.read"]["available"])
        self.assertEqual(self.t["health.read"]["reason_code"], "runtime_disabled")
        self.assertIn("task.timer.start", self.t)
        self.assertTrue(self.t["task.timer.start"]["available"])
        self.assertEqual(self.t["task.timer.start"]["display_label"], "开始行动计时")
        self.assertFalse(hasattr(tool_inventory, "_ACTIVE"))

    def test_health_inventory_tracks_only_the_physical_surface(self):
        actual_surface = tuple(current_physical_surface_names())
        binding = "mcp__internal__get.health"

        with mock.patch(
            "tools.tool_inventory.physical_surface_names",
            return_value=actual_surface,
        ):
            groups = tool_inventory._current_capability_groups()
        health = next(
            tool
            for group in groups
            for tool in group["tools"]
            if tool["tool_name"] == "health.read"
        )
        published = binding in actual_surface
        self.assertEqual(health["provider"], binding)
        self.assertEqual(health["available"], published)
        self.assertEqual(
            health["reason_code"],
            "active" if published else "runtime_disabled",
        )

        absent_surface = tuple(name for name in actual_surface if name != binding)
        with mock.patch(
            "tools.tool_inventory.physical_surface_names",
            return_value=absent_surface,
        ):
            disabled_groups = tool_inventory._current_capability_groups()
        disabled_health = next(
            tool
            for group in disabled_groups
            for tool in group["tools"]
            if tool["tool_name"] == "health.read"
        )
        self.assertFalse(disabled_health["available"])
        self.assertEqual(disabled_health["reason_code"], "runtime_disabled")

    def test_inventory_has_no_duplicate_tool_names(self):
        names = tool_inventory.inventory_names()
        self.assertEqual(self.p["total"], len(names))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(sum(group["total"] for group in self.p["groups"]), self.p["total"])
        self.assertEqual(
            sum(group["available"] for group in self.p["groups"]),
            self.p["available_count"],
        )

    def test_historical_groups_are_explicit_and_do_not_duplicate_current_rows(self):
        current_names = {
            tool["tool_name"]
            for group in self.p["groups"][:len(tool_companion_hints._EXPECTED_GROUPS)]
            for tool in group["tools"]
        }
        self.assertIn("browse_github", {tool["tool_name"] for tool in self.groups["legacy:web"]["tools"]})
        github_tool = next(tool for tool in self.groups["legacy:web"]["tools"] if tool["tool_name"] == "browse_github")
        self.assertFalse(github_tool["available"])
        self.assertEqual(github_tool["reason_code"], "provider_blocked")
        patch_tool = next(
            tool for tool in self.groups["legacy:code_files"]["tools"]
            if tool["tool_name"] == "codebase_patch"
        )
        self.assertFalse(patch_tool["available"])
        self.assertEqual(patch_tool["reason_code"], "safety_gap")
        legacy_names = {
            tool["tool_name"]
            for group in self.p["groups"] if group["id"].startswith("legacy:")
            for tool in group["tools"]
        }
        self.assertTrue(current_names.isdisjoint(legacy_names))

    def test_external_groups_use_the_same_catalog_shape(self):
        catalog = [{
            "server_id": "calendar",
            "display_name": "Calendar",
            "lifecycle_state": "CONNECTED",
            "transport": "STREAMABLE_HTTP",
            "revision": 4,
            "tools": [{
                "control_id": "ext:calendar:roll",
                "remote_tool_name": "roll",
                "description": "Roll a calendar entry",
                "input_schema": {"type": "object"},
                "fingerprint": "a" * 64,
                "source_registry_revision": 4,
                "surface_tool_name": "calendar__roll__1234567890",
                "available": True,
            }],
        }]
        with mock.patch("tools.external_mcp_surface.list_external_surface", return_value=catalog):
            groups = tool_inventory._external_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["id"], "external_mcp:calendar")
        self.assertEqual(groups[0]["label"], "Calendar")
        self.assertEqual(groups[0]["transport"], "STREAMABLE_HTTP")
        self.assertEqual(groups[0]["tools"][0]["tool_name"], "roll")
        self.assertTrue(groups[0]["tools"][0]["available"])
        self.assertEqual(groups[0]["tools"][0]["provider"], "External MCP · Streamable HTTP")

        catalog[0]["lifecycle_state"] = "DISCONNECTED"
        catalog[0]["tools"][0]["available"] = False
        with mock.patch("tools.external_mcp_surface.list_external_surface", return_value=catalog):
            disconnected = tool_inventory._external_groups()[0]
        self.assertEqual(disconnected["tools"][0]["status_label"], "未连接")
        self.assertFalse(disconnected["tools"][0]["available"])

    def test_endpoint_contracts_and_read_only_inventory(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn("@app.route('/api/tools/companion-hints', methods=['GET', 'PATCH'])", source)
        self.assertIn("allowed = {'capability_id', 'display_label', 'companion_hint', 'reset'}", source)
        self.assertIn("companion_hints.update_hint(", source)
        self.assertIn("@app.route('/api/tools/inventory', methods=['GET'])", source)
        self.assertIn("from tools.tool_inventory import payload", source)
        self.assertNotIn("@app.route('/api/tools/inventory', methods=['GET', 'PATCH'])", source)
        self.assertNotIn("@app.route('/api/tools/inventory', methods=['POST'])", source)


if __name__ == "__main__":
    unittest.main()
