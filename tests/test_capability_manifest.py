from __future__ import annotations

import unittest

from tools.capability_manifest import (
    CAPABILITY_AUTONOMY_MODES,
    CAPABILITY_FIELDS,
    CAPABILITY_KINDS,
    CAPABILITY_LOADING_POLICIES,
    CAPABILITY_MANIFEST,
    CAPABILITY_SIDE_EFFECTS,
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
    p1_enabled_capabilities,
)


class CapabilityManifestContractTests(unittest.TestCase):
    def test_manifest_uses_exact_frozen_fields(self):
        expected = set(CAPABILITY_FIELDS)
        self.assertEqual(len(CAPABILITY_FIELDS), 11)
        for item in CAPABILITY_MANIFEST:
            self.assertEqual(set(item), expected, item.get("capability_id"))

    def test_capability_ids_are_unique_and_match_frozen_p1_scope(self):
        ids = [item["capability_id"] for item in CAPABILITY_MANIFEST]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            set(ids),
            P1_ENABLED_CAPABILITY_IDS | P1_RESERVED_CAPABILITY_IDS,
        )
        self.assertTrue(P1_ENABLED_CAPABILITY_IDS.isdisjoint(P1_RESERVED_CAPABILITY_IDS))
        self.assertEqual(
            P1_ENABLED_CAPABILITY_IDS,
            {
                "memory.search",
                "memory.write",
                "diary.write",
                "home.light.status",
                "todo.read",
                "todo.write",
                "task.timer.start",
                "countdown.read",
                "ledger.read",
                "ledger.budget.read",
                "ledger.write",
                "files.read",
                "files.find",
                "code.search",
                "web.search",
                "web.read",
            },
        )
        self.assertEqual(
            P1_RESERVED_CAPABILITY_IDS,
            {
                "github.read",
                "home.light.control",
                "code.write",
                "workspace.execute",
            },
        )

    def test_frozen_enums_and_provider_neutral_ids(self):
        for item in CAPABILITY_MANIFEST:
            capability_id = item["capability_id"]
            self.assertIn(item["kind"], CAPABILITY_KINDS, capability_id)
            self.assertIn(item["side_effect"], CAPABILITY_SIDE_EFFECTS, capability_id)
            self.assertIn(item["autonomy_mode"], CAPABILITY_AUTONOMY_MODES, capability_id)
            self.assertIn(item["loading_policy"], CAPABILITY_LOADING_POLICIES, capability_id)
            self.assertNotIn("mcp__", capability_id)
            self.assertNotIn("claude", capability_id.lower())
            self.assertNotIn("openai", capability_id.lower())
            self.assertIsInstance(item["provider_bindings"], dict)
            for text_field in (
                "display_name",
                "trigger",
                "purpose",
                "deny_when",
                "failure_behavior",
            ):
                self.assertTrue(str(item[text_field]).strip(), (capability_id, text_field))

    def test_diary_write_is_self_authored_chat_capability(self):
        item = get_capability("diary.write")
        self.assertEqual(item["display_name"], "记日记")
        self.assertEqual(item["kind"], "write")
        self.assertEqual(item["side_effect"], "external_state")
        self.assertEqual(item["autonomy_mode"], "self_write_auto")
        self.assertEqual(item["loading_policy"], "deferred")
        self.assertEqual(
            item["provider_bindings"]["claude_code"],
            "mcp__capability__diary_write",
        )
        self.assertEqual(
            item["provider_bindings"]["home_mcp"],
            "mcp__home__write_diary",
        )

    def test_p1_loading_policy_matches_frozen_visibility_contract(self):
        self.assertEqual(get_capability("memory.search")["loading_policy"], "always_load")
        for capability_id in (
            "home.light.status",
            "todo.read",
            "todo.write",
            "countdown.read",
            "ledger.read",
            "ledger.budget.read",
            "ledger.write",
        ):
            self.assertEqual(get_capability(capability_id)["loading_policy"], "deferred")
        memory_write = get_capability("memory.write")
        self.assertEqual(memory_write["kind"], "write")
        self.assertEqual(memory_write["side_effect"], "external_state")
        self.assertEqual(memory_write["autonomy_mode"], "self_write_auto")
        self.assertEqual(memory_write["loading_policy"], "deferred")

        for capability_id in ("files.read", "files.find", "code.search"):
            self.assertEqual(get_capability(capability_id)["loading_policy"], "task_scoped")

    def test_p1_autonomy_and_side_effects_match_contract(self):
        for capability_id in (
            "memory.search",
            "home.light.status",
            "todo.read",
            "countdown.read",
            "ledger.read",
            "ledger.budget.read",
        ):
            item = get_capability(capability_id)
            self.assertEqual(item["kind"], "read")
            self.assertEqual(item["side_effect"], "none")
            self.assertEqual(item["autonomy_mode"], "read_auto")

        for capability_id in ("todo.write", "ledger.write"):
            item = get_capability(capability_id)
            self.assertEqual(item["kind"], "write")
            self.assertEqual(item["side_effect"], "external_state")
            self.assertEqual(item["autonomy_mode"], "explicit_or_ask")

        for capability_id in ("files.read", "files.find", "code.search"):
            item = get_capability(capability_id)
            self.assertEqual(item["kind"], "read")
            self.assertEqual(item["side_effect"], "none")
            self.assertEqual(item["autonomy_mode"], "task_only")

    def test_enabled_bindings_are_only_provider_specific_metadata(self):
        expected_cc_bindings = {
            "memory.search": "mcp__capability__memory_search",
            "memory.write": "mcp__capability__memory_write",
            "diary.write": "mcp__capability__diary_write",
            "home.light.status": "mcp__capability__home_light_status",
            "todo.read": "mcp__capability__todo_read",
            "todo.write": "mcp__capability__todo_write",
            "task.timer.start": "mcp__capability__task_timer_start",
            "countdown.read": "mcp__home__get_countdowns",
            "ledger.read": "mcp__capability__ledger_read",
            "ledger.budget.read": "mcp__capability__ledger_budget_read",
            "ledger.write": "mcp__capability__ledger_write",
            "files.read": "Read",
            "files.find": "Glob",
            "code.search": "Grep",
            "web.search": "WebSearch",
            "web.read": "WebFetch",
        }
        for capability_id, binding in expected_cc_bindings.items():
            self.assertEqual(
                get_capability(capability_id)["provider_bindings"].get("claude_code"),
                binding,
            )
        self.assertEqual(
            get_capability("home.light.status")["provider_bindings"].get("home_mcp"),
            "mcp__home__get_light_status",
        )

    def test_task_timer_contract(self):
        item = get_capability("task.timer.start")
        self.assertEqual(item["display_name"], "开始行动计时")
        self.assertEqual(item["kind"], "write")
        self.assertEqual(item["side_effect"], "external_state")
        self.assertEqual(item["autonomy_mode"], "explicit_or_ask")
        self.assertEqual(item["loading_policy"], "deferred")
        self.assertEqual(
            item["provider_bindings"]["claude_code"],
            "mcp__capability__task_timer_start",
        )

    def test_lookup_and_p1_enabled_order_do_not_expand_scope(self):
        self.assertIsNone(get_capability("unknown.capability"))
        enabled = p1_enabled_capabilities()
        self.assertEqual(
            {item["capability_id"] for item in enabled},
            P1_ENABLED_CAPABILITY_IDS,
        )
        self.assertTrue(
            all(item["capability_id"] not in P1_RESERVED_CAPABILITY_IDS for item in enabled)
        )

    def test_github_read_stays_reserved_without_provider_proof(self):
        self.assertIn("github.read", P1_RESERVED_CAPABILITY_IDS)
        self.assertNotIn("github.read", P1_ENABLED_CAPABILITY_IDS)
        self.assertEqual(get_capability("github.read")["provider_bindings"], {})


if __name__ == "__main__":
    unittest.main()

