import unittest
from pathlib import Path
from unittest import mock

from tools import tool_companion_hints
from tools import tool_inventory
from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS


class ToolInventoryTest(unittest.TestCase):
    def setUp(self):
        p = tool_inventory.payload()
        self.p = p
        self.t = {x["tool_name"]: x for g in p["groups"] for x in g["tools"]}

    def test_total_unique_and_group_sum(self):
        names = tool_inventory.inventory_names()
        self.assertEqual(self.p["total"], 84)
        self.assertEqual(len(names), 84)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(sum(g["total"] for g in self.p["groups"]), 84)
        self.assertEqual(sum(g["available"] for g in self.p["groups"]), self.p["available_count"])
        self.assertEqual(self.p["available_count"], 18)
        self.assertEqual(self.p["unavailable_count"], 66)

    def test_workspace_not_duplicated(self):
        gs = {g["id"]: g for g in self.p["groups"]}
        self.assertEqual(gs["workspace"]["total"], 12)
        self.assertEqual(gs["code_files"]["total"], 15)
        self.assertFalse(
            {x["tool_name"] for x in gs["workspace"]["tools"]}
            & {x["tool_name"] for x in gs["code_files"]["tools"]}
        )

    def test_frozen_rules(self):
        self.assertTrue(self.t["get_light_status"]["available"])
        for n in ("light_on", "light_off", "light_warm", "light_neutral"):
            self.assertFalse(self.t[n]["available"])
            self.assertEqual(self.t[n]["reason_code"], "contract_disabled")
        for n in ("set_brightness", "set_color_temp"):
            self.assertFalse(self.t[n]["available"])
            self.assertEqual(self.t[n]["reason_code"], "retired")
        for n in ("codebase_patch", "codebase_create_file"):
            self.assertFalse(self.t[n]["available"])
            self.assertEqual(self.t[n]["reason_code"], "safety_gap")
        self.assertEqual(self.t["collect_chat_moment"]["reason_code"], "prerequisite_unproven")

    def test_write_graduation_inventory_is_green(self):
        for name, provider, label in (
            ("add_todo", "mcp__home__add_todo", "记录待办"),
            ("add_ledger", "mcp__home__add_ledger", "记一笔账"),
            ("write_diary", "mcp__home__write_diary", "记日记"),
        ):
            self.assertTrue(self.t[name]["available"])
            self.assertEqual(self.t[name]["reason_code"], "active")
            self.assertNotIn(name, tool_inventory._GRAY)
            self.assertEqual(self.t[name]["provider"], provider)
            self.assertEqual(self.t[name]["display_label"], label)

    def test_codebase_read_green(self):
        for n in (
            "codebase_describe_project", "codebase_read_file", "codebase_list_directory",
            "codebase_search_code", "codebase_find_references", "codebase_git_view",
            "codebase_explain_history",
        ):
            self.assertTrue(self.t[n]["available"])
            self.assertEqual(self.t[n]["provider"], "mcp__codebase")

    def test_external_read_green_and_github_stays_gray(self):
        self.assertTrue(self.t["web_search"]["available"])
        self.assertEqual(self.t["web_search"]["provider"], "Claude Code WebSearch")
        self.assertTrue(self.t["read_webpage"]["available"])
        self.assertEqual(self.t["read_webpage"]["provider"], "Claude Code WebFetch")
        self.assertFalse(self.t["browse_github"]["available"])
        self.assertEqual(self.t["browse_github"]["reason_code"], "provider_blocked")

    def test_all_display_labels_are_readable(self):
        names = tool_inventory.inventory_names()
        labels = {row["tool_name"]: row["display_label"] for group in self.p["groups"] for row in group["tools"]}
        self.assertEqual(len(tool_inventory.DISPLAY_LABELS), 84)
        self.assertEqual(set(labels), set(names))
        self.assertTrue(all(labels[name].strip() for name in names))
        self.assertTrue(all(labels[name] != name for name in names))

    def test_companion_catalog_and_model_preview_are_frozen(self):
        capability_ids = [
            capability_id
            for _, _, ids in tool_companion_hints._EXPECTED_GROUPS
            for capability_id in ids
        ]
        self.assertEqual(len(capability_ids), 14)
        self.assertEqual(len(set(capability_ids)), 14)
        self.assertEqual(set(capability_ids), set(P1_ENABLED_CAPABILITY_IDS))
        with mock.patch.object(tool_companion_hints.config_store, "get", return_value=""):
            preview = tool_companion_hints.payload()["prompt_preview"]
        self.assertIn("网络搜索", preview)
        self.assertIn("读取网页", preview)
        for inventory_only_name in ("pocket_status", "shop_browse", "codebase_patch"):
            self.assertNotIn(inventory_only_name, preview)

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

