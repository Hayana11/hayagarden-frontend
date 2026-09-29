from __future__ import annotations

import unittest

from tools.capability_manifest import (
    AUTOMATION_CONFIRM_ONLY_CAPABILITIES,
    CAPABILITY_AUTONOMY_MODES,
    CAPABILITY_FIELDS,
    CAPABILITY_KINDS,
    CAPABILITY_LOADING_POLICIES,
    CAPABILITY_MANIFEST,
    CAPABILITY_SIDE_EFFECTS,
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
    ordinary_auto_capabilities,
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
                "health.read",
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
                "gallery.save",
                "gallery.recall",
                "gallery.screenshot",
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
            "health.read",
        ):
            item = get_capability(capability_id)
            self.assertEqual(item["kind"], "read")
            self.assertEqual(item["side_effect"], "none")
            self.assertEqual(item["autonomy_mode"], "read_auto")

        for capability_id in ("todo.write", "ledger.write"):
            item = get_capability(capability_id)
            self.assertEqual(item["kind"], "write")
            self.assertEqual(item["side_effect"], "external_state")
            self.assertEqual(item["autonomy_mode"], "self_write_auto")

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
            "health.read": "mcp__internal__get.health",
            "gallery.save": "mcp__capability__gallery_save",
            "gallery.recall": "mcp__capability__gallery_recall",
            "gallery.screenshot": "mcp__capability__gallery_screenshot",
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

    def test_health_read_is_provider_neutral_read_capability(self):
        item = get_capability("health.read")
        self.assertEqual(item["kind"], "read")
        self.assertEqual(item["side_effect"], "none")
        self.assertEqual(item["autonomy_mode"], "read_auto")
        self.assertEqual(item["purpose"], "读取本人已同步的健康摘要与指标序列。")
        for field in ("purpose", "trigger", "deny_when", "failure_behavior"):
            self.assertNotIn("Xiaomi", item[field])
            self.assertNotIn("Smart Band 11", item[field])
        self.assertEqual(
            item["provider_bindings"]["claude_code"],
            "mcp__internal__get.health",
        )
        self.assertEqual(
            item["provider_bindings"]["internal_mcp"],
            "mcp__internal__get.health",
        )

    def test_task_timer_contract(self):
        item = get_capability("task.timer.start")
        self.assertEqual(item["display_name"], "开始行动计时")
        self.assertEqual(item["kind"], "write")
        self.assertEqual(item["side_effect"], "external_state")
        self.assertEqual(item["autonomy_mode"], "self_write_auto")
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

    def test_ordinary_auto_follows_manifest_order_and_exclusions(self):
        derived = ordinary_auto_capabilities()
        self.assertEqual(
            derived,
            (
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
                "web.search",
                "web.read",
                "health.read",
                "gallery.save",
                "gallery.recall",
                "gallery.screenshot",
            ),
        )
        self.assertEqual(
            derived,
            tuple(
                item["capability_id"]
                for item in CAPABILITY_MANIFEST
                if item["capability_id"] in derived
            ),
        )
        self.assertEqual(derived, tuple(sorted(derived, key=lambda cid: [
            item["capability_id"] for item in CAPABILITY_MANIFEST
        ].index(cid))))
        self.assertNotEqual(derived, tuple(sorted(derived)))

        for capability_id in (
            "files.read",
            "files.find",
            "code.search",
            "github.read",
            "home.light.control",
            "code.write",
            "workspace.execute",
        ):
            self.assertNotIn(capability_id, derived)

        self.assertEqual(AUTOMATION_CONFIRM_ONLY_CAPABILITIES, ())
        self.assertNotIn("web.search", AUTOMATION_CONFIRM_ONLY_CAPABILITIES)
        self.assertNotIn("web.read", AUTOMATION_CONFIRM_ONLY_CAPABILITIES)
        self.assertIn("web.search", derived)
        self.assertIn("web.read", derived)

        for item in CAPABILITY_MANIFEST:
            capability_id = item["capability_id"]
            binding = (item.get("provider_bindings") or {}).get("claude_code")
            has_binding = bool(
                (isinstance(binding, str) and binding.strip())
                or (
                    isinstance(binding, (tuple, list))
                    and any(str(part).strip() for part in binding)
                )
            )
            if capability_id in derived:
                self.assertIn(capability_id, P1_ENABLED_CAPABILITY_IDS)
                self.assertNotIn(capability_id, P1_RESERVED_CAPABILITY_IDS)
                self.assertIn(item["autonomy_mode"], {"read_auto", "self_write_auto"})
                self.assertTrue(has_binding)
            else:
                excluded_by_rule = (
                    capability_id not in P1_ENABLED_CAPABILITY_IDS
                    or capability_id in P1_RESERVED_CAPABILITY_IDS
                    or item["autonomy_mode"] not in {"read_auto", "self_write_auto"}
                    or not has_binding
                )
                self.assertTrue(excluded_by_rule, capability_id)

        self.assertEqual(get_capability("github.read")["provider_bindings"], {})
        self.assertEqual(get_capability("workspace.execute")["provider_bindings"], {})
        self.assertEqual(get_capability("home.light.control")["provider_bindings"], {})


if __name__ == "__main__":
    unittest.main()

