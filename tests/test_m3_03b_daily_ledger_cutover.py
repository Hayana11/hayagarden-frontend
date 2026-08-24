"""M3-03B Daily Ledger provider cutover contracts."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
from tools import execution_fence
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import (
    INTERNAL_MCP_CAPABILITY_IDS,
    INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
    build_uh_a0_spawn_plan,
    physical_surface_fingerprint,
)
from tools.cc_tool_surface import _static_schema_registry
from tools.capability_state import RUNTIME_STATE_INHERIT, RUNTIME_STATE_OFF
from tools.lease_signer import issue_turn_lease
from wake.cc_tools import WAKE_TO_CC_MCP


BASE_FINGERPRINT = "4e5e630e8266874f8f5c99bda243647a300793d24ead0af1fe0a97fa22e11df0"
TARGET_FINGERPRINT = "7344a43bbb3163e8a7b4b46e568f05fd8a4f1b973a367b5e46c030d52df4a397"
LEDGER_INTERNAL = (
    "mcp__internal__get_ledger",
    "mcp__internal__get_ledger_budget",
)
LEDGER_PROXY = ("mcp__capability__ledger_write",)
LEDGER_HOME = (
    "mcp__home__get_ledger",
    "mcp__home__get_ledger_budget",
    "mcp__home__add_ledger",
)


def _plan(state=RUNTIME_STATE_INHERIT):
    with tempfile.TemporaryDirectory() as root:
        with mock.patch(
            "tools.cc_capability_adapter.read_capability_state",
            return_value=state,
        ):
            return build_uh_a0_spawn_plan(
                cwd=root,
                write_mcp_config=False,
                env={},
            )


class DailyLedgerCutoverTests(unittest.TestCase):
    def lease(self, *, source="default_policy", requested=(), approvals=(), turn_id="m3-03b"):
        return issue_turn_lease(
            turn_id=turn_id,
            turn_mode="chat",
            issued_from=source,
            requested_capabilities=requested,
            approval_ids=approvals,
            issued_at="2026-08-22T00:00:00Z",
        )

    def test_manifest_provider_bindings_and_grouping(self):
        self.assertEqual(
            INTERNAL_MCP_CAPABILITY_IDS,
            ("todo.read", "ledger.read", "ledger.budget.read"),
        )
        self.assertEqual(
            get_capability("ledger.read")["provider_bindings"],
            {
                "claude_code": "mcp__internal__get_ledger",
                "internal_mcp": "mcp__internal__get_ledger",
                "home_mcp": "mcp__home__get_ledger",
            },
        )
        self.assertEqual(
            get_capability("ledger.budget.read")["provider_bindings"],
            {
                "claude_code": "mcp__internal__get_ledger_budget",
                "internal_mcp": "mcp__internal__get_ledger_budget",
                "home_mcp": "mcp__home__get_ledger_budget",
            },
        )
        self.assertEqual(
            get_capability("ledger.write")["provider_bindings"],
            {
                "claude_code": "mcp__capability__ledger_write",
                "internal_mcp": "mcp__internal__add_ledger",
                "home_mcp": "mcp__home__add_ledger",
            },
        )

    def test_daily_allow_disallow_and_fingerprint(self):
        plan = _plan()
        allowed = set(plan["surface_allowlist"])
        disallowed = set(plan["disallowed_tools"])
        self.assertTrue(set(LEDGER_INTERNAL) <= allowed)
        self.assertTrue(set(LEDGER_PROXY) <= allowed)
        self.assertTrue(set(LEDGER_HOME) <= disallowed)
        self.assertIn("mcp__capability__memory_search", allowed)
        self.assertNotIn("mcp__home__search_memories", allowed)
        self.assertIn("mcp__home__search_memories", disallowed)
        self.assertIn("mcp__internal__search_memories", disallowed)
        self.assertTrue(set(LEDGER_HOME).isdisjoint(allowed))
        self.assertTrue(set(LEDGER_INTERNAL).isdisjoint(disallowed))
        self.assertIn("mcp__internal__get_todos", allowed)
        self.assertIn("mcp__internal__get_todos", allowed)
        self.assertNotIn("mcp__home__get_todos", allowed)
        self.assertNotIn("mcp__home__add_todo", allowed)
        self.assertEqual(plan["physical_surface_fingerprint"], TARGET_FINGERPRINT)
        self.assertIn("mcp__internal__search_memories", allowed)
        self.assertNotIn("mcp__home__search_memories", allowed)
        self.assertIn("mcp__home__search_memories", disallowed)
        self.assertNotIn("mcp__internal__search_memories", disallowed)
        self.assertEqual(plan["physical_surface_fingerprint"], physical_surface_fingerprint())
        self.assertNotEqual(BASE_FINGERPRINT, TARGET_FINGERPRINT)
        self.assertEqual(
            INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS,
            (),
        )

    def test_runtime_off_hides_internal_and_keeps_home_denied(self):
        plan = _plan(RUNTIME_STATE_OFF)
        allowed = set(plan["surface_allowlist"])
        disallowed = set(plan["disallowed_tools"])
        self.assertTrue(set(LEDGER_INTERNAL).isdisjoint(allowed))
        self.assertTrue(set(LEDGER_PROXY).isdisjoint(allowed))
        self.assertTrue(set(LEDGER_HOME).isdisjoint(allowed))
        self.assertTrue(set(LEDGER_INTERNAL) <= disallowed)
        self.assertTrue(set(LEDGER_PROXY) <= disallowed)
        self.assertTrue(set(LEDGER_HOME) <= disallowed)
        self.assertTrue({
            "mcp__home__search_memories",
            "mcp__internal__search_memories",
            "mcp__capability__memory_search",
            "mcp__capability__memory_write",
        } <= disallowed)

    def test_internal_ledger_schema_matches_home_exactly(self):
        registry = _static_schema_registry()
        for internal, home in zip(LEDGER_INTERNAL, LEDGER_HOME):
            self.assertEqual(registry[internal], registry[home])
        self.assertEqual(registry[LEDGER_PROXY[0]], registry["mcp__home__add_ledger"])

    def test_execution_fence_and_approval_identity(self):
        for tool_name, capability_id in (
            *zip(LEDGER_INTERNAL, ("ledger.read", "ledger.budget.read")),
            *zip(LEDGER_PROXY, ("ledger.write",)),
            *zip(LEDGER_HOME, ("ledger.read", "ledger.budget.read", "ledger.write")),
        ):
            self.assertEqual(execution_fence.capability_for_tool(tool_name), capability_id)
        action = {"amount": -12}
        home_id = execution_fence.build_approval_id("ledger.write", "mcp__home__add_ledger", action)
        internal_id = execution_fence.build_approval_id("ledger.write", "mcp__capability__ledger_write", action)
        self.assertNotEqual(home_id, internal_id)
        self.assertEqual(
            execution_fence.approval_prompt("mcp__capability__ledger_write", action),
            "这笔 12 元要我一起记账吗？",
        )

    def test_stale_home_pending_is_cleared_before_spawn(self):
        action = {"amount": -12, "category": "餐饮"}
        with tempfile.TemporaryDirectory() as tmp:
            session = cc_resident.ResidentSession(
                cwd=tmp,
                allowed_tools="",
                mcp_config_path=str(Path(tmp) / "cc-tools.json"),
            )
            session._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
            session._system_text = "UH-A0 test system"
            pending = session._capture_authoritative_deferred({
                "session_id": "old-ledger-session",
                "deferred_tool_use": {
                    "id": "old-ledger-toolu-1",
                    "name": "mcp__home__add_ledger",
                    "input": action,
                },
            })
            confirmation = self.lease(
                source="user_confirmation",
                requested=("ledger.write",),
                approvals=(pending["approval_id"],),
                turn_id="stale-home-ledger",
            )
            with mock.patch.object(
                session,
                "spawn_resumable",
                side_effect=AssertionError("stale Home pending spawned"),
            ) as spawn, mock.patch.object(
                session,
                "send_turn",
                side_effect=AssertionError("stale Home execution"),
            ):
                with self.assertRaisesRegex(
                    cc_resident.ResidentError,
                    r"deferred_resume:LEASE_MISMATCH",
                ):
                    list(session.resume_pending_deferred_turn(
                        "好，记上吧",
                        dict(os.environ),
                        confirmation,
                    ))
            self.assertIsNone(session.pending_deferred)
            spawn.assert_not_called()

            fresh = execution_fence.evaluate_tool_call(
                "mcp__capability__ledger_write",
                action,
                self.lease(),
            )
            self.assertEqual(fresh["lease_decision"], "CAPABILITY_ASK_REQUIRED")
            self.assertNotEqual(fresh["approval_id"], pending["approval_id"])

    def test_internal_down_has_no_home_fallback(self):
        plan = _plan()
        self.assertEqual(set(LEDGER_INTERNAL) & set(plan["surface_allowlist"]), set(LEDGER_INTERNAL))
        self.assertEqual(set(LEDGER_PROXY) & set(plan["surface_allowlist"]), set(LEDGER_PROXY))
        self.assertEqual(set(LEDGER_HOME) & set(plan["surface_allowlist"]), set())
        self.assertTrue(set(LEDGER_HOME) <= set(plan["disallowed_tools"]))

    def test_resident_surface_change_is_lazy(self):
        resident = cc_resident.ResidentSession(
            cwd=tempfile.mkdtemp(),
            allowed_tools="",
            mcp_config_path="",
        )
        class Alive:
            def poll(self):
                return None
        resident._proc = Alive()
        resident._tool_profile = cc_resident.TOOL_PROFILE_UH_A0
        resident._system_text = "same-system"
        resident._history_rewrite_epoch = "test-epoch"
        resident._bound_tool_surface_fingerprint = BASE_FINGERPRINT
        before = resident.generation
        with mock.patch(
            "chat.cc_history_rewrite.current_history_rewrite_epoch",
            return_value="test-epoch",
        ), mock.patch(
            "chat.cc_history_rewrite.is_unreadable_epoch",
            return_value=False,
        ), mock.patch(
            "tools.cc_capability_adapter.physical_surface_fingerprint",
            return_value=TARGET_FINGERPRINT,
        ):
            self.assertEqual(
                resident.peek_respawn_reason(
                    "same-system",
                    tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
                ),
                "tool_surface_changed",
            )
        self.assertEqual(resident.generation, before)

    def test_wake_home_zero_diff_contract(self):
        self.assertEqual(WAKE_TO_CC_MCP["get_ledger"], "mcp__home__get_ledger")
        self.assertEqual(WAKE_TO_CC_MCP["get_ledger_budget"], "mcp__home__get_ledger_budget")
        self.assertEqual(WAKE_TO_CC_MCP["add_ledger"], "mcp__home__add_ledger")


if __name__ == "__main__":
    unittest.main()
