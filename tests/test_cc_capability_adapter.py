from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
import chat.daily_runtime as daily_runtime
from tools.capability_manifest import (
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)
from tools.cc_capability_adapter import (
    FORBIDDEN_BUILTIN_TOOLS,
    HOME_MCP_CAPABILITY_IDS,
    NON_P3_HOME_MCP_TOOLS,
    TOOL_PROFILE_UH_A0,
    UH_A0_BUILTIN_TOOLS,
    assert_reserved_absent_from_surface,
    build_uh_a0_mcp_config,
    build_uh_a0_spawn_plan,
    diagnose_tool_search,
    loading_plan_from_manifest,
    physical_surface_fingerprint,
    physical_surface_names,
    uh_a0_home_mcp_tools,
    uh_a0_native_bindings,
)
from tools.lease_signer import issue_turn_lease


class CcCapabilityAdapterContractTests(unittest.TestCase):
    def test_a_bindings_come_from_capability_manifest(self):
        home = uh_a0_home_mcp_tools()
        native = uh_a0_native_bindings()
        for cid in HOME_MCP_CAPABILITY_IDS:
            expected = get_capability(cid)["provider_bindings"]["claude_code"]
            self.assertIn(expected, home)
        self.assertEqual(native["files.read"], "Read")
        self.assertEqual(native["files.find"], "Glob")
        self.assertEqual(native["code.search"], "Grep")
        # No second product dictionary: every surface MCP name resolves via manifest.
        for name in home:
            matches = [
                cid
                for cid in P1_ENABLED_CAPABILITY_IDS
                if get_capability(cid)["provider_bindings"].get("claude_code") == name
            ]
            self.assertEqual(len(matches), 1, name)

    def test_b_exact_builtin_surface(self):
        self.assertEqual(UH_A0_BUILTIN_TOOLS, ("Read", "Glob", "Grep"))
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertEqual(plan["built_in_tools"], UH_A0_BUILTIN_TOOLS)
        self.assertEqual(plan["built_in_tools_csv"], "Read,Glob,Grep")
        for bad in FORBIDDEN_BUILTIN_TOOLS:
            self.assertNotIn(bad, plan["built_in_tools"])
            self.assertIn(bad, plan["disallowed_tools"])

    def test_c_home_surface_is_p1_enabled_only(self):
        home = uh_a0_home_mcp_tools()
        self.assertEqual(len(home), 8)
        self.assertEqual(
            set(home),
            {
                "mcp__home__search_memories",
                "mcp__home__get_light_status",
                "mcp__home__get_todos",
                "mcp__home__add_todo",
                "mcp__home__get_countdowns",
                "mcp__home__get_ledger",
                "mcp__home__get_ledger_budget",
                "mcp__home__add_ledger",
            },
        )
        for name in NON_P3_HOME_MCP_TOOLS:
            self.assertNotIn(name, home)
        cfg = build_uh_a0_mcp_config()
        self.assertEqual(set(cfg["mcpServers"]), {"home"})
        self.assertEqual(
            cfg["mcpServers"]["home"]["headers"],
            {"X-UH-A0-Profile": "uh_a0"},
        )
        self.assertNotIn("brain", cfg["mcpServers"])
        self.assertNotIn("codebase", cfg["mcpServers"])
        self.assertNotIn("workspace", cfg["mcpServers"])

    def test_d_reserved_fail_closed(self):
        surface = physical_surface_names()
        assert_reserved_absent_from_surface(surface)
        for cid in P1_RESERVED_CAPABILITY_IDS:
            binding = (get_capability(cid)["provider_bindings"] or {}).get("claude_code")
            if isinstance(binding, str) and binding:
                self.assertNotIn(binding, surface)
            if isinstance(binding, (list, tuple)):
                for item in binding:
                    self.assertNotIn(item, surface)
        for bad in ("WebSearch", "WebFetch", "Bash", "Edit", "Write", "Agent"):
            self.assertNotIn(bad, surface)

    def test_e_physical_surface_stable_across_leases(self):
        chat_lease = issue_turn_lease(
            turn_id="chat-1",
            turn_mode="chat",
            issued_from="default_policy",
        )
        task_lease = issue_turn_lease(
            turn_id="task-1",
            turn_mode="task",
            issued_from="default_policy",
        )
        self.assertNotEqual(
            chat_lease["allowed_capabilities"],
            task_lease["allowed_capabilities"],
        )
        plan_chat = build_uh_a0_spawn_plan(
            write_mcp_config=False,
            turn_lease=chat_lease,
            env={},
        )
        plan_task = build_uh_a0_spawn_plan(
            write_mcp_config=False,
            turn_lease=task_lease,
            env={},
        )
        self.assertEqual(
            plan_chat["physical_surface_fingerprint"],
            plan_task["physical_surface_fingerprint"],
        )
        self.assertEqual(plan_chat["built_in_tools"], plan_task["built_in_tools"])
        self.assertEqual(plan_chat["home_mcp_tools"], plan_task["home_mcp_tools"])
        self.assertEqual(
            plan_chat["physical_surface_fingerprint"],
            physical_surface_fingerprint(),
        )

    def test_f_memory_loading_matches_manifest(self):
        loading = loading_plan_from_manifest()
        self.assertEqual(loading["memory.search"], "always_load")
        for cid in HOME_MCP_CAPABILITY_IDS:
            if cid == "memory.search":
                continue
            self.assertEqual(loading[cid], "deferred", cid)
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertEqual(plan["memory_search_loading"], "always_load")
        self.assertEqual(plan["loading_plan"]["memory.search"], "always_load")

    def test_g_native_bindings_not_codebase(self):
        native = uh_a0_native_bindings()
        self.assertEqual(native, {
            "files.read": "Read",
            "files.find": "Glob",
            "code.search": "Grep",
        })
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        blob = json.dumps(plan["mcp_config"])
        self.assertNotIn("codebase", blob)
        self.assertNotIn("mcp__codebase", ",".join(plan["surface_allowlist"]))

    def test_h_no_raw_bash(self):
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertNotIn("Bash", plan["built_in_tools"])
        self.assertNotIn("Bash", plan["surface_allowlist"])
        self.assertIn("Bash", plan["disallowed_tools"])

    def test_i_daily_live_gate_remains_flag_off(self):
        self.assertEqual(cc_resident.TOOL_PROFILE_UH_A0, "uh_a0")
        self.assertEqual(TOOL_PROFILE_UH_A0, "uh_a0")
        self.assertEqual(
            daily_runtime.DAILY_TOOL_PROFILE,
            cc_resident.TOOL_PROFILE_TEXT_ONLY,
        )
        self.assertNotEqual(
            daily_runtime.DAILY_TOOL_PROFILE,
            cc_resident.TOOL_PROFILE_UH_A0,
        )

    def test_j_legacy_and_text_only_profiles_unchanged(self):
        self.assertEqual(cc_resident.TOOL_PROFILE_LEGACY, "legacy")
        self.assertEqual(cc_resident.TOOL_PROFILE_TEXT_ONLY, "text_only")
        session = cc_resident.ResidentSession(
            cwd="/tmp",
            allowed_tools="mcp__home__get_todos",
            mcp_config_path="/tmp/cc-tools.json",
        )
        session._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
        text_flags = session._build_spawn_tool_flags(env={})
        self.assertEqual(text_flags["tools"], "")
        self.assertEqual(text_flags["extra"], ["--allowedTools", ""])

        session._tool_profile = cc_resident.TOOL_PROFILE_LEGACY
        legacy_flags = session._build_spawn_tool_flags(env={})
        self.assertEqual(legacy_flags["tools"], "")
        self.assertEqual(
            legacy_flags["extra"],
            [
                "--mcp-config",
                "/tmp/cc-tools.json",
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__home__get_todos",
            ],
        )

    def test_k_tool_search_status_recorded(self):
        diag = diagnose_tool_search(env={}, actual_version="2.1.220")
        self.assertIn(
            diag["tool_search_status"],
            {"AVAILABLE", "PRELOAD_FALLBACK", "ENVIRONMENT_BLOCKED"},
        )
        self.assertEqual(diag["expected_claude_code_version"], "2.1.220")
        self.assertEqual(diag["anthropic_base_url_class"], "absent")
        self.assertFalse(diag["enable_tool_search_set"])
        # Current production-shaped env (no ENABLE_TOOL_SEARCH) => preload fallback.
        self.assertEqual(diag["tool_search_status"], "PRELOAD_FALLBACK")

        blocked = diagnose_tool_search(
            env={"ANTHROPIC_BASE_URL": "https://proxy.example/v1", "ENABLE_TOOL_SEARCH": "true"},
        )
        self.assertEqual(blocked["tool_search_status"], "ENVIRONMENT_BLOCKED")

        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertEqual(plan["tool_search"]["tool_search_status"], "PRELOAD_FALLBACK")
        self.assertEqual(plan["home_loading_mode"], "preload_fallback")

    def test_uh_a0_resident_argv_uses_builtin_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "cc-tools.json"
            legacy.write_text(
                json.dumps({
                    "mcpServers": {
                        "home": {"type": "http", "url": "http://127.0.0.1:3100/mcp"},
                        "brain": {"type": "http", "url": "http://127.0.0.1:8000/mcp"},
                        "codebase": {"type": "http", "url": "http://127.0.0.1:5056/mcp"},
                    }
                }),
                encoding="utf-8",
            )
            session = cc_resident.ResidentSession(
                cwd=tmp,
                allowed_tools="mcp__brain__breath,mcp__home__light_on",
                mcp_config_path=str(legacy),
            )
            session._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            flags = session._build_spawn_tool_flags(env={})
            self.assertEqual(flags["tools"], "Read,Glob,Grep")
            self.assertIn("--strict-mcp-config", flags["extra"])
            self.assertIn("--disallowedTools", flags["extra"])
            allowed_idx = flags["extra"].index("--allowedTools") + 1
            allowed = flags["extra"][allowed_idx]
            self.assertIn("Read", allowed)
            self.assertIn("mcp__home__search_memories", allowed)
            self.assertNotIn("mcp__brain__", allowed)
            self.assertNotIn("mcp__home__light_on", allowed)
            mcp_path = Path(flags["mcp_path"])
            self.assertTrue(mcp_path.is_file())
            cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
            self.assertEqual(set(cfg["mcpServers"]), {"home"})

            # tool_profile mismatch is detected by the existing decision helper
            # once a live generation exists (process_dead otherwise wins).
            session._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
            session._system_text = "SYS"
            session._proc = mock.Mock(poll=mock.Mock(return_value=None))
            reason = session._decide_respawn_reason(
                "SYS",
                tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
            )
            self.assertEqual(reason, "tool_profile_changed")
            session._proc = None


if __name__ == "__main__":
    unittest.main()
