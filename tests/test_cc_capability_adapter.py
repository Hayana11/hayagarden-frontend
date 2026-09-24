from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
import chat.cc_history_rewrite
import chat.daily_runtime as daily_runtime
from tools.cc_tool_surface import _CAPABILITY_PROXY_TOOL_SCHEMAS, _HOME_TOOL_SCHEMAS, _INTERNAL_TOOL_SCHEMAS
from tools.capability_manifest import (
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)
from tools import capability_state
from tools.capability_state import (
    CAPABILITY_STATE_KEY,
    set_capability_state,
)
from tools.cc_capability_adapter import (
    FORBIDDEN_BUILTIN_TOOLS,
    HOME_MCP_CAPABILITY_IDS,
    CAPABILITY_PROXY_CAPABILITY_IDS,
    INTERNAL_MCP_CAPABILITY_IDS,
    XIAOMI_HEALTH_CAPABILITY_IDS,
    INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
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
    uh_a0_internal_mcp_tools,
    uh_a0_xiaomi_health_tools,
    uh_a0_capability_proxy_tools,
    uh_a0_home_legacy_tools,
    uh_a0_home_compatibility_tools,
    uh_a0_external_read_tools,
    uh_a0_native_bindings,
)
from tools.lease_signer import issue_turn_lease


class CcCapabilityAdapterContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "runtime.db"
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE runtime_config ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at DATETIME DEFAULT (datetime('now')))"
        )
        conn.commit()
        conn.close()
        self._state_patch = mock.patch.object(
            capability_state, "DB_PATH", str(self._db_path)
        )
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _plan(self):
        return build_uh_a0_spawn_plan(
            cwd=self._tmp.name,
            write_mcp_config=False,
            env={},
        )

    def _write_raw_state(self, value):
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "INSERT OR REPLACE INTO runtime_config (key, value) VALUES (?, ?)",
            (CAPABILITY_STATE_KEY, value),
        )
        conn.commit()
        conn.close()

    def test_a_bindings_come_from_capability_manifest(self):
        self.assertEqual(HOME_MCP_CAPABILITY_IDS, ("countdown.read",))
        self.assertEqual(INTERNAL_MCP_CAPABILITY_IDS, ())
        self.assertEqual(XIAOMI_HEALTH_CAPABILITY_IDS, ("get.health",))
        self.assertTrue(set(XIAOMI_HEALTH_CAPABILITY_IDS).isdisjoint(P1_ENABLED_CAPABILITY_IDS))
        self.assertEqual(
            CAPABILITY_PROXY_CAPABILITY_IDS,
            ("memory.search", "memory.write", "diary.write", "task.timer.start", "home.light.status", "todo.read", "todo.write", "ledger.read", "ledger.budget.read", "ledger.write"),
        )
        self.assertEqual(
            uh_a0_xiaomi_health_tools(),
            ("mcp__internal__get_health",),
        )
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
        home = uh_a0_home_mcp_tools()
        native = uh_a0_native_bindings()
        for cid in HOME_MCP_CAPABILITY_IDS:
            expected = get_capability(cid)["provider_bindings"]["claude_code"]
            self.assertIn(expected, home)
        self.assertEqual(uh_a0_internal_mcp_tools(), ())
        for cid, tool_name in zip(
            XIAOMI_HEALTH_CAPABILITY_IDS,
            uh_a0_xiaomi_health_tools(),
            strict=True,
        ):
            bindings = get_capability(cid)["provider_bindings"]
            self.assertEqual(bindings["claude_code"], tool_name)
            self.assertEqual(bindings["internal_mcp"], "get.health")
            self.assertNotIn(cid, P1_ENABLED_CAPABILITY_IDS)
        self.assertEqual(native["files.read"], "Read")
        self.assertEqual(native["files.find"], "Glob")
        self.assertEqual(native["code.search"], "Grep")
        self.assertEqual(uh_a0_external_read_tools(), ("WebSearch", "WebFetch"))
        # No second product dictionary: every surface MCP name resolves via manifest.
        for name in home:
            matches = [
                cid
                for cid in P1_ENABLED_CAPABILITY_IDS
                if get_capability(cid)["provider_bindings"].get("claude_code") == name
            ]
            self.assertEqual(len(matches), 1, name)

    def test_health_read_capabilities_are_internal_mcp_only(self):
        plan = self._plan()
        expected = set(uh_a0_xiaomi_health_tools())
        self.assertEqual(len(expected), 1)
        self.assertTrue(expected.issubset(set(plan["internal_mcp_tools"])))
        self.assertTrue(expected.issubset(set(plan["surface_allowlist"])))
        self.assertTrue(expected.isdisjoint(set(plan["disallowed_tools"])))

    def test_health_days_schema_is_bounded(self):
        schema = _INTERNAL_TOOL_SCHEMAS["mcp__internal__get_health"]
        self.assertEqual(
            schema["properties"]["metric"]["enum"],
            ["all", "status", "steps", "sleep", "heart_rate"],
        )
        self.assertEqual(schema["properties"]["metric"]["default"], "all")
        self.assertEqual(schema["properties"]["days"]["minimum"], 1)
        self.assertEqual(schema["properties"]["days"]["maximum"], 30)
        self.assertEqual(schema["properties"]["days"]["default"], 7)

    def test_b_home_legacy_tools_keep_todo_and_ledger_order(self):
        self.assertEqual(
            uh_a0_home_legacy_tools(),
            (
                "mcp__home__get_todos",
                "mcp__home__add_todo",
                "mcp__home__get_ledger",
                "mcp__home__get_ledger_budget",
                "mcp__home__add_ledger",
                "mcp__home__search_memories",
                "mcp__home__get_light_status",
            ),
        )

    def test_b_exact_builtin_surface(self):
        self.assertEqual(
            UH_A0_BUILTIN_TOOLS,
            ("Read", "Glob", "Grep", "WebSearch", "WebFetch"),
        )
        plan = build_uh_a0_spawn_plan(write_mcp_config=False, env={})
        self.assertEqual(plan["built_in_tools"], UH_A0_BUILTIN_TOOLS)
        self.assertEqual(plan["built_in_tools_csv"], "Read,Glob,Grep,WebSearch,WebFetch")
        for bad in FORBIDDEN_BUILTIN_TOOLS:
            self.assertNotIn(bad, plan["built_in_tools"])
            self.assertIn(bad, plan["disallowed_tools"])

    def test_c_home_surface_is_p1_enabled_only(self):
        home = uh_a0_home_mcp_tools()
        self.assertEqual(len(home), 1)
        self.assertEqual(
            set(home),
            {
                "mcp__home__get_countdowns",
            },
        )
        for name in NON_P3_HOME_MCP_TOOLS:
            self.assertNotIn(name, home)
        cfg = build_uh_a0_mcp_config(env={})
        self.assertEqual(set(cfg["mcpServers"]), {"home", "internal", "capability", "external"})
        capability_cfg = cfg["mcpServers"]["capability"]
        self.assertEqual(capability_cfg["type"], "stdio")
        self.assertTrue(capability_cfg["env"]["TODO_INTERNAL_DB_PATH"])
        self.assertTrue(capability_cfg["env"]["TODO_INTERNAL_DB_PATH"].endswith("memories.db"))
        self.assertEqual(
            cfg["mcpServers"]["home"]["headers"],
            {"X-UH-A0-Profile": "uh_a0"},
        )
        self.assertNotIn("brain", cfg["mcpServers"])
        self.assertNotIn("codebase", cfg["mcpServers"])
        self.assertEqual(cfg["mcpServers"]["internal"]["url"], "http://127.0.0.1:3101/mcp")
        self.assertEqual(
            cfg["mcpServers"]["internal"]["headers"],
            {"X-UH-A0-Profile": "uh_a0"},
        )
        external_cfg = cfg["mcpServers"]["external"]
        self.assertEqual(external_cfg["type"], "stdio")
        self.assertEqual(set(external_cfg["env"]), {"UH_A0_REPO_ROOT", "UH_A0_TURN_LEASE_PATH"})
        self.assertTrue(external_cfg["env"]["UH_A0_REPO_ROOT"])
        self.assertTrue(external_cfg["env"]["UH_A0_TURN_LEASE_PATH"])
        self.assertTrue(external_cfg["args"][0].endswith("external-mcp-surface-server.js"))
        self.assertNotIn("workspace", cfg["mcpServers"])

    def test_c_capability_proxy_servers_share_resident_lease_path(self):
        lease_path = str(Path(self._tmp.name) / ".uh-a0-current-turn-lease.json")
        cfg = build_uh_a0_mcp_config(env={"UH_A0_TURN_LEASE_PATH": lease_path})

        capability_env = cfg["mcpServers"]["capability"]["env"]
        self.assertEqual(
            capability_env["UH_A0_TURN_LEASE_PATH"],
            lease_path,
        )
        self.assertEqual(
            cfg["mcpServers"]["external"]["env"]["UH_A0_TURN_LEASE_PATH"],
            lease_path,
        )

        proxy_tools = set(uh_a0_capability_proxy_tools())
        self.assertTrue(
            {
                "mcp__capability__memory_write",
                "mcp__capability__home_light_status",
                "mcp__capability__todo_read",
                "mcp__capability__ledger_read",
            }.issubset(proxy_tools)
        )

    def test_c_home_compatibility_diary_is_hidden_but_registered(self):
        self.assertEqual(
            uh_a0_home_compatibility_tools(),
            ("mcp__home__write_diary",),
        )
        plan = self._plan()
        self.assertNotIn("mcp__home__write_diary", plan["surface_allowlist"])
        self.assertIn("mcp__home__write_diary", plan["disallowed_tools"])
        self.assertIn("mcp__capability__diary_write", plan["surface_allowlist"])
        self.assertIn("mcp__capability__task_timer_start", plan["surface_allowlist"])

    def test_c_diary_surface_schema_is_content_only(self):
        old_schema = _HOME_TOOL_SCHEMAS["mcp__home__write_diary"]
        new_schema = _CAPABILITY_PROXY_TOOL_SCHEMAS["mcp__capability__diary_write"]
        for schema in (old_schema, new_schema):
            self.assertEqual(set(schema["properties"]), {"content"})
            self.assertEqual(schema["required"], ["content"])

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
        for bad in ("Bash", "Edit", "Write", "Agent"):
            self.assertNotIn(bad, surface)
        for good in ("WebSearch", "WebFetch"):
            self.assertIn(good, surface)

    def test_e_exact_inherit_fingerprint_is_stable(self):
        first = physical_surface_fingerprint()
        second = physical_surface_fingerprint()
        self.assertEqual(first, second)
        print("S4_PHYSICAL_SURFACE_FINGERPRINT=" + first)

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
        self.assertEqual(loading["diary.write"], "deferred")
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

    def test_i_daily_live_profile_is_uh_a0(self):
        self.assertEqual(cc_resident.TOOL_PROFILE_UH_A0, "uh_a0")
        self.assertEqual(TOOL_PROFILE_UH_A0, "uh_a0")
        self.assertEqual(
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
        self.assertEqual(diag["minimum_claude_code_version"], "2.1.280")
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
            self.assertEqual(flags["tools"], "Read,Glob,Grep,WebSearch,WebFetch")
            self.assertIn("--strict-mcp-config", flags["extra"])
            self.assertIn("--disallowedTools", flags["extra"])
            allowed_idx = flags["extra"].index("--allowedTools") + 1
            allowed = flags["extra"][allowed_idx]
            self.assertIn("Read", allowed)
            self.assertNotIn("mcp__home__search_memories", allowed)
            self.assertIn("mcp__capability__memory_search", allowed)
            self.assertNotIn("mcp__internal__get_todos", allowed)
            deny_idx = flags["extra"].index("--disallowedTools") + 1
            disallowed = flags["extra"][deny_idx]
            self.assertIn("mcp__internal__get_todos", disallowed)
            self.assertIn("mcp__capability__todo_read", allowed)
            self.assertIn("mcp__capability__todo_write", allowed)
            self.assertNotIn("mcp__brain__", allowed)
            self.assertNotIn("mcp__home__get_todos", allowed)
            self.assertNotIn("mcp__home__add_todo", allowed)
            self.assertNotIn("mcp__home__light_on", allowed)
            mcp_path = Path(flags["mcp_path"])
            self.assertTrue(mcp_path.is_file())
            cfg = json.loads(mcp_path.read_text(encoding="utf-8"))
            self.assertEqual(set(cfg["mcpServers"]), {"home", "internal", "capability", "external"})

            # tool_profile mismatch is detected by the existing decision helper
            # once a live generation exists (process_dead otherwise wins).
            session._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
            session._system_text = "SYS"
            session._history_rewrite_epoch = "test-epoch"
            session._proc = mock.Mock(poll=mock.Mock(return_value=None))
            with mock.patch.object(
                chat.cc_history_rewrite,
                "current_history_rewrite_epoch",
                return_value="test-epoch",
            ):
                reason = session._decide_respawn_reason(
                    "SYS",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                )
            self.assertEqual(reason, "tool_profile_changed")
            session._proc = None


    def test_l_runtime_off_hides_native_external_and_home_surface(self):
        set_capability_state("files.read", enabled=False)
        set_capability_state("web.search", enabled=False)
        set_capability_state("todo.read", enabled=False)
        set_capability_state("memory.search", enabled=False)
        set_capability_state("memory.write", enabled=False)
        plan = self._plan()

        self.assertNotIn("Read", plan["built_in_tools"])
        self.assertNotIn("Read", plan["built_in_tools_csv"])
        self.assertNotIn("Read", plan["surface_allowlist"])
        self.assertNotIn("Read", plan["claude_visible_built_ins"])
        self.assertNotIn("WebSearch", plan["built_in_tools"])
        self.assertNotIn("WebSearch", plan["built_in_tools_csv"])
        self.assertNotIn("WebSearch", plan["claude_visible_built_ins"])

        self.assertNotIn("mcp__home__get_todos", plan["home_mcp_tools"])
        self.assertNotIn(
            "mcp__home__get_todos", plan["surface_allowlist"]
        )
        self.assertIn("mcp__home__get_todos", plan["disallowed_tools"])
        for ledger_tool in (
            "mcp__home__get_ledger",
            "mcp__home__get_ledger_budget",
            "mcp__home__add_ledger",
        ):
            self.assertNotIn(ledger_tool, plan["home_mcp_tools"])
            self.assertIn(ledger_tool, plan["disallowed_tools"])
        self.assertNotIn("mcp__home__search_memories", plan["home_mcp_tools"])
        self.assertNotIn("mcp__internal__search_memories", plan["internal_mcp_tools"])
        self.assertNotIn("mcp__internal__write_memory", plan["internal_mcp_tools"])
        self.assertIn("mcp__home__search_memories", plan["disallowed_tools"])
        self.assertIn("mcp__internal__search_memories", plan["disallowed_tools"])
        self.assertIn("mcp__internal__write_memory", plan["disallowed_tools"])
        self.assertIn("mcp__capability__memory_search", plan["disallowed_tools"])
        self.assertIn("mcp__capability__memory_write", plan["disallowed_tools"])
        self.assertIn("mcp__home__light_on", plan["disallowed_tools"])
        self.assertIn("mcp__home__exec_vps", plan["disallowed_tools"])

        tools_idx = plan["spawn_extra_args"].index("--allowedTools") + 1
        deny_idx = plan["spawn_extra_args"].index("--disallowedTools") + 1
        self.assertNotIn("Read", plan["spawn_extra_args"][tools_idx])
        self.assertIn(
            "mcp__home__get_todos", plan["spawn_extra_args"][deny_idx]
        )

    def test_m2_runtime_on_restores_surface_and_fingerprint(self):
        baseline = self._plan()
        set_capability_state("todo.read", enabled=False)
        hidden = self._plan()
        set_capability_state("todo.read", enabled=True)
        restored = self._plan()

        self.assertNotEqual(
            baseline["physical_surface_fingerprint"],
            hidden["physical_surface_fingerprint"],
        )
        for field in (
            "built_in_tools",
            "home_mcp_tools",
            "surface_allowlist",
            "disallowed_tools",
            "claude_visible_built_ins",
            "claude_visible_mcp_tools",
            "physical_surface_fingerprint",
        ):
            self.assertEqual(restored[field], baseline[field], field)

    def test_m3_reserved_and_unknown_runtime_on_cannot_enter_surface(self):
        with self.assertRaises(ValueError):
            set_capability_state("github.read", enabled=True)
        with self.assertRaises(ValueError):
            set_capability_state("unknown.capability", enabled=True)
        plan = self._plan()
        assert_reserved_absent_from_surface(plan["surface_allowlist"])
        self.assertEqual(plan["runtime_state_status"], "OK")

    def test_m4_malformed_runtime_state_fails_closed(self):
        self._write_raw_state("{not-json")
        plan = self._plan()
        self.assertEqual(plan["runtime_state_status"], "FAIL_CLOSED")
        self.assertEqual(plan["built_in_tools"], ())
        self.assertEqual(plan["home_mcp_tools"], ())
        self.assertEqual(plan["surface_allowlist"], ())
        for tool in uh_a0_home_mcp_tools():
            self.assertIn(tool, plan["disallowed_tools"])

    def test_m5_storage_unavailable_fails_closed(self):
        with mock.patch.object(
            capability_state,
            "_connect",
            side_effect=sqlite3.OperationalError("locked"),
        ):
            plan = self._plan()
        self.assertEqual(plan["runtime_state_status"], "FAIL_CLOSED")
        self.assertEqual(plan["built_in_tools"], ())
        self.assertEqual(plan["home_mcp_tools"], ())
        self.assertEqual(plan["surface_allowlist"], ())


if __name__ == "__main__":
    unittest.main()
