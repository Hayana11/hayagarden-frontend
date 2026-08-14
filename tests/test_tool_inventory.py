import unittest
from pathlib import Path
from tools import tool_inventory
class ToolInventoryTest(unittest.TestCase):
    def setUp(self):
        p=tool_inventory.payload()
        self.p=p; self.t={x["tool_name"]:x for g in p["groups"] for x in g["tools"]}
    def test_total_unique_and_group_sum(self):
        names=tool_inventory.inventory_names()
        self.assertEqual(self.p["total"],83); self.assertEqual(len(names),83); self.assertEqual(len(names),len(set(names)))
        self.assertEqual(sum(g["total"] for g in self.p["groups"]),83)
        self.assertEqual(sum(g["available"] for g in self.p["groups"]),self.p["available_count"])
    def test_workspace_not_duplicated(self):
        gs={g["id"]:g for g in self.p["groups"]}
        self.assertEqual(gs["workspace"]["total"],12); self.assertEqual(gs["code_files"]["total"],15)
        self.assertFalse({x["tool_name"] for x in gs["workspace"]["tools"]}&{x["tool_name"] for x in gs["code_files"]["tools"]})
    def test_frozen_rules(self):
        self.assertTrue(self.t["get_light_status"]["available"])
        for n in ("light_on","light_off","light_warm","light_neutral"):
            self.assertFalse(self.t[n]["available"]); self.assertEqual(self.t[n]["reason_code"],"contract_disabled")
        for n in ("set_brightness","set_color_temp"):
            self.assertFalse(self.t[n]["available"]); self.assertEqual(self.t[n]["reason_code"],"retired")
        for n in ("codebase_patch","codebase_create_file","add_todo","add_ledger"):
            self.assertFalse(self.t[n]["available"]); self.assertEqual(self.t[n]["reason_code"],"safety_gap")
        self.assertEqual(self.t["collect_chat_moment"]["reason_code"],"prerequisite_unproven")
    def test_codebase_read_green(self):
        for n in ("codebase_describe_project","codebase_read_file","codebase_list_directory","codebase_search_code","codebase_find_references","codebase_git_view","codebase_explain_history"):
            self.assertTrue(self.t[n]["available"]); self.assertEqual(self.t[n]["provider"],"mcp__codebase")
    def test_endpoint_get_only(self):
        s=(Path(__file__).resolve().parents[1]/"app.py").read_text(encoding="utf-8")
        self.assertIn("@app.route('/api/tools/inventory', methods=['GET'])",s); self.assertIn("from tools.tool_inventory import payload",s)
if __name__=="__main__": unittest.main()
