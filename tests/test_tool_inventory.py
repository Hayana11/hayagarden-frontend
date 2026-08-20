import unittest

from tools import tool_inventory


class ToolInventoryTest(unittest.TestCase):
    def setUp(self):
        self.payload = tool_inventory.payload()
        self.groups = {
            group["id"]: group for group in self.payload["groups"]
        }
        self.tools = {
            tool["tool_name"]: tool
            for group in self.payload["groups"]
            for tool in group["tools"]
        }

    def test_inventory_totals_and_availability_are_unchanged(self):
        self.assertEqual(self.payload["total"], 83)
        self.assertEqual(self.payload["available_count"], 15)
        self.assertEqual(self.payload["unavailable_count"], 68)
        self.assertEqual(
            sum(group["available"] for group in self.payload["groups"]),
            self.payload["available_count"],
        )

    def test_countdown_wake_action_taxonomy_matches_inventory_payload(self):
        expected = {
            "get_countdowns": (
                "查看日期倒计时",
                "plans_ledger",
                True,
                "当前可用",
                "active",
                "mcp__home__get_countdowns",
            ),
            "set_self_trigger": (
                "设置延时主动联系",
                "triggers",
                False,
                "当前不可用",
                "legacy_only",
                None,
            ),
            "cancel_self_trigger": (
                "取消延时主动联系",
                "triggers",
                False,
                "当前不可用",
                "legacy_only",
                None,
            ),
            "issue_command": (
                "设置行动倒计时",
                "phone",
                False,
                "当前不可用",
                "legacy_only",
                None,
            ),
        }
        for name, (label, group_id, available, status_label, reason_code, provider) in expected.items():
            tool = self.tools[name]
            self.assertEqual(
                (
                    tool["tool_name"],
                    tool["display_label"],
                    tool["available"],
                    tool["status_label"],
                    tool["reason_code"],
                    tool["provider"],
                ),
                (name, label, available, status_label, reason_code, provider),
            )
            self.assertIn(
                name,
                {tool["tool_name"] for tool in self.groups[group_id]["tools"]},
            )

        self.assertEqual(self.groups["plans_ledger"]["label"], "生活 / 日程")
        self.assertEqual(self.groups["triggers"]["label"], "Wake")
        self.assertEqual(self.groups["phone"]["label"], "行动")
        self.assertNotIn(
            "issue_command",
            {tool["tool_name"] for tool in self.groups["triggers"]["tools"]},
        )


if __name__ == "__main__":
    unittest.main()
