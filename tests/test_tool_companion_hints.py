import importlib
import os
import tempfile
import unittest
from unittest import mock


_CONFIG_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_CONFIG_DB.close()
os.environ["HAYAGARDEN_CONFIG_DB_PATH"] = _CONFIG_DB.name

from tools import tool_companion_hints as hints


class ToolCompanionHintsTest(unittest.TestCase):
    def setUp(self):
        hints.config_store.DB_PATH = _CONFIG_DB.name
        hints.config_store.set(hints.CONFIG_KEY, "")
        importlib.reload(hints)

    def test_catalog_matches_p1_and_has_eleven_unique_capabilities(self):
        from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS

        ids = [item["capability_id"] for group in hints.catalog() for item in group["items"]]
        self.assertEqual(set(ids), set(P1_ENABLED_CAPABILITY_IDS))
        self.assertEqual(len(ids), 11)
        self.assertEqual(len(ids), len(set(ids)))

    def test_light_boundary_is_power_only_and_explicit(self):
        light = next(
            item
            for group in hints.catalog()
            for item in group["items"]
            if item["capability_id"] == "home.light.status"
        )
        boundary = light["physical_boundary"]
        for text in ("主灯", "床头灯", "power", "不读取亮度", "Kelvin", "color_temp"):
            self.assertIn(text, boundary)
        self.assertIn("不能开灯", boundary)

    def test_model_visible_block_excludes_internal_contract_metadata(self):
        block = hints.prompt_preview()
        for forbidden in (
            "capability_id",
            "mcp__",
            "trigger=",
            "deny_when=",
            "provider_bindings",
            "lease",
            "approval",
        ):
            self.assertNotIn(forbidden, block)

    def test_copy_preserves_newlines_and_outer_spaces_and_reset(self):
        raw = "  第一行\n第二行  "
        hints.update_copy("todo.read", companion_hint=raw)
        item = next(
            item
            for group in hints.catalog()
            for item in group["items"]
            if item["capability_id"] == "todo.read"
        )
        self.assertEqual(item["companion_hint"], raw)
        hints.update_copy("todo.read", reset=True)
        reset_item = next(
            item
            for group in hints.catalog()
            for item in group["items"]
            if item["capability_id"] == "todo.read"
        )
        self.assertNotEqual(reset_item["companion_hint"], raw)

    def test_static_and_daily_use_the_same_tool_intuition(self):
        from chat import system_builder

        with mock.patch.object(system_builder, "read_persona", return_value="persona"), \
             mock.patch.object(system_builder, "build_stable_note", return_value="stable"):
            static = system_builder.build_cc_static_parts()
            daily = system_builder.build_cc_daily_static_parts()
        self.assertEqual(static["tool_intuition"], daily["tool_intuition"])
        self.assertIn(static["tool_intuition"], static["full_system"])
        self.assertIn(daily["tool_intuition"], daily["full_system"])

    def test_public_catalog_has_only_copy_and_readonly_boundary_fields(self):
        allowed = {
            "capability_id",
            "display_label",
            "companion_hint",
            "default_display_label",
            "default_companion_hint",
            "physical_boundary",
            "status_label",
            "kind",
        }
        for group in hints.catalog():
            for item in group["items"]:
                self.assertEqual(set(item), allowed)


if __name__ == "__main__":
    unittest.main()
