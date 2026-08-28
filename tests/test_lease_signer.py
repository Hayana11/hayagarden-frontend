from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from tools.capability_manifest import (
    CAPABILITY_MANIFEST,
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)
from tools.lease_signer import (
    DEFAULT_ALLOWED_CAPABILITIES,
    ISSUED_FROM_VALUES,
    LEASE_VERSION,
    TURN_LEASE_FIELDS,
    LeaseSignError,
    default_allowed_capabilities,
    issue_turn_lease,
)


CHAT_DEFAULTS = (
    "memory.search",
    "memory.write",
    "diary.write",
    "home.light.status",
    "todo.read",
    "todo.write",
    "countdown.read",
    "task.timer.start",
    "ledger.read",
    "ledger.budget.read",
    "ledger.write",
)
WAKE_DEFAULTS = (
    "memory.search",
    "home.light.status",
    "todo.read",
    "countdown.read",
    "ledger.read",
    "ledger.budget.read",
)
TASK_DEFAULTS = (
    "files.read",
    "files.find",
    "code.search",
)


class LeaseSignerContractTests(unittest.TestCase):
    def test_a_frozen_turn_lease_shape(self):
        lease = issue_turn_lease(
            turn_id="t-shape",
            turn_mode="chat",
            issued_from="default_policy",
            issued_at="2026-08-12T00:00:00Z",
        )
        self.assertEqual(tuple(lease.keys()), TURN_LEASE_FIELDS)
        self.assertEqual(
            set(TURN_LEASE_FIELDS),
            {
                "lease_version",
                "turn_id",
                "turn_mode",
                "issued_from",
                "allowed_capabilities",
                "approval_ids",
                "task_contract_id",
                "issued_at",
            },
        )
        self.assertEqual(lease["lease_version"], 1)
        self.assertIsInstance(LEASE_VERSION, int)
        self.assertEqual(lease["lease_version"], LEASE_VERSION)
        self.assertEqual(lease["turn_id"], "t-shape")
        self.assertEqual(lease["turn_mode"], "chat")
        self.assertEqual(lease["issued_from"], "default_policy")
        self.assertIsInstance(lease["allowed_capabilities"], tuple)
        self.assertEqual(lease["approval_ids"], ())
        self.assertIsNone(lease["task_contract_id"])
        self.assertEqual(lease["issued_at"], "2026-08-12T00:00:00Z")

    def test_b_issued_from_whitelist_fail_closed(self):
        self.assertEqual(
            ISSUED_FROM_VALUES,
            {
                "default_policy",
                "explicit_user_intent",
                "user_confirmation",
                "task_contract",
            },
        )
        for bad in (
            "model_request",
            "assistant_decision",
            "auto",
            "inherited",
            "previous_turn",
            "system_guess",
            "wake_memory",
            "fallback",
        ):
            with self.assertRaises(LeaseSignError) as ctx:
                issue_turn_lease(
                    turn_id="t-bad-source",
                    turn_mode="chat",
                    issued_from=bad,
                )
            self.assertEqual(ctx.exception.code, "DENIED_CAPABILITY")

    def test_c_default_policy_matches_section_5_3(self):
        self.assertEqual(DEFAULT_ALLOWED_CAPABILITIES["chat"], CHAT_DEFAULTS)
        self.assertEqual(DEFAULT_ALLOWED_CAPABILITIES["wake"], WAKE_DEFAULTS)
        self.assertEqual(DEFAULT_ALLOWED_CAPABILITIES["task"], TASK_DEFAULTS)

        for mode, expected in (
            ("chat", CHAT_DEFAULTS),
            ("wake", WAKE_DEFAULTS),
            ("task", TASK_DEFAULTS),
        ):
            lease = issue_turn_lease(
                turn_id=f"t-default-{mode}",
                turn_mode=mode,
                issued_from="default_policy",
            )
            self.assertEqual(lease["allowed_capabilities"], expected)
            self.assertEqual(default_allowed_capabilities(mode), expected)
            if mode == "chat":
                self.assertIn("todo.write", lease["allowed_capabilities"])
                self.assertIn("ledger.write", lease["allowed_capabilities"])
            else:
                self.assertNotIn("todo.write", lease["allowed_capabilities"])
                self.assertNotIn("ledger.write", lease["allowed_capabilities"])
            self.assertNotIn("code.write", lease["allowed_capabilities"])
            self.assertNotIn("workspace.execute", lease["allowed_capabilities"])

        with self.assertRaises(LeaseSignError):
            issue_turn_lease(
                turn_id="t-default-extra",
                turn_mode="chat",
                issued_from="default_policy",
                requested_capabilities=("todo.write",),
            )

    def test_d_no_inheritance_across_turns(self):
        turn_n = issue_turn_lease(
            turn_id="turn-100",
            turn_mode="chat",
            issued_from="explicit_user_intent",
            requested_capabilities=("todo.write",),
        )
        self.assertIn("todo.write", turn_n["allowed_capabilities"])

        turn_n1 = issue_turn_lease(
            turn_id="turn-101",
            turn_mode="chat",
            issued_from="default_policy",
        )
        self.assertIn("todo.write", turn_n1["allowed_capabilities"])
        self.assertEqual(turn_n1["allowed_capabilities"], CHAT_WAKE_DEFAULTS)

        with self.assertRaises(LeaseSignError) as ctx:
            issue_turn_lease(
                turn_id="turn-102",
                turn_mode="chat",
                issued_from="default_policy",
                previous_lease=turn_n,
            )
        self.assertEqual(ctx.exception.code, "LEASE_MISMATCH")

        wake = issue_turn_lease(
            turn_id="wake-after-chat",
            turn_mode="wake",
            issued_from="default_policy",
        )
        self.assertNotIn("todo.write", wake["allowed_capabilities"])
        self.assertEqual(wake["allowed_capabilities"], CHAT_WAKE_DEFAULTS)

    def test_e_explicit_user_intent_signs_write_without_ask(self):
        lease = issue_turn_lease(
            turn_id="t-explicit-todo",
            turn_mode="chat",
            issued_from="explicit_user_intent",
            requested_capabilities=("todo.write",),
        )
        self.assertEqual(lease["issued_from"], "explicit_user_intent")
        self.assertIn("todo.write", lease["allowed_capabilities"])
        for capability_id in CHAT_DEFAULTS:
            self.assertIn(capability_id, lease["allowed_capabilities"])
        self.assertEqual(lease["approval_ids"], ())
        self.assertIsNone(lease["task_contract_id"])

        ledger = issue_turn_lease(
            turn_id="t-explicit-ledger",
            turn_mode="chat",
            issued_from="explicit_user_intent",
            requested_capabilities=("ledger.write",),
        )
        self.assertIn("ledger.write", ledger["allowed_capabilities"])

        with self.assertRaises(LeaseSignError) as ctx:
            issue_turn_lease(
                turn_id="t-explicit-task-only",
                turn_mode="chat",
                issued_from="explicit_user_intent",
                requested_capabilities=("files.read",),
            )
        self.assertEqual(ctx.exception.code, "DENIED_CAPABILITY")

    def test_f_user_confirmation_creates_new_lease_with_approval_ids(self):
        old = issue_turn_lease(
            turn_id="t-ask-old",
            turn_mode="chat",
            issued_from="default_policy",
            issued_at="2026-08-12T01:00:00Z",
        )
        old_snapshot = dict(old)

        confirmed = issue_turn_lease(
            turn_id="t-ask-new",
            turn_mode="chat",
            issued_from="user_confirmation",
            requested_capabilities=("todo.write",),
            approval_ids=("approval-42",),
            issued_at="2026-08-12T01:01:00Z",
        )
        self.assertEqual(confirmed["turn_id"], "t-ask-new")
        self.assertNotEqual(confirmed["turn_id"], old["turn_id"])
        self.assertEqual(confirmed["approval_ids"], ("approval-42",))
        self.assertIn("todo.write", confirmed["allowed_capabilities"])
        self.assertEqual(old, old_snapshot)
        self.assertNotIn("todo.write", old["allowed_capabilities"])

        with self.assertRaises(LeaseSignError) as ctx:
            issue_turn_lease(
                turn_id="t-ask-missing",
                turn_mode="chat",
                issued_from="user_confirmation",
                requested_capabilities=("todo.write",),
            )
        self.assertEqual(ctx.exception.code, "LEASE_MISMATCH")

    def test_g_task_contract_required_for_task_only(self):
        with self.assertRaises(LeaseSignError) as ctx:
            issue_turn_lease(
                turn_id="t-task-missing-id",
                turn_mode="task",
                issued_from="task_contract",
                requested_capabilities=("files.read",),
            )
        self.assertEqual(ctx.exception.code, "LEASE_MISMATCH")

        with self.assertRaises(LeaseSignError):
            issue_turn_lease(
                turn_id="t-task-wrong-mode",
                turn_mode="chat",
                issued_from="task_contract",
                task_contract_id="task-1",
                requested_capabilities=("files.read",),
            )

        lease = issue_turn_lease(
            turn_id="t-task-ok",
            turn_mode="task",
            issued_from="task_contract",
            task_contract_id="task-77",
            requested_capabilities=("todo.read",),
        )
        self.assertEqual(lease["task_contract_id"], "task-77")
        self.assertEqual(lease["issued_from"], "task_contract")
        for capability_id in TASK_DEFAULTS:
            self.assertIn(capability_id, lease["allowed_capabilities"])
        self.assertIn("todo.read", lease["allowed_capabilities"])

        plain_task = issue_turn_lease(
            turn_id="t-task-default",
            turn_mode="task",
            issued_from="default_policy",
        )
        self.assertEqual(plain_task["allowed_capabilities"], TASK_DEFAULTS)
        self.assertNotIn("code.write", plain_task["allowed_capabilities"])
        self.assertNotIn("workspace.execute", plain_task["allowed_capabilities"])

    def test_h_reserved_capabilities_fail_closed(self):
        for capability_id in sorted(P1_RESERVED_CAPABILITY_IDS):
            with self.assertRaises(LeaseSignError) as ctx:
                issue_turn_lease(
                    turn_id=f"t-reserved-{capability_id}",
                    turn_mode="chat",
                    issued_from="explicit_user_intent",
                    requested_capabilities=(capability_id,),
                )
            self.assertEqual(ctx.exception.code, "DENIED_CAPABILITY")

            with self.assertRaises(LeaseSignError) as ctx2:
                issue_turn_lease(
                    turn_id=f"t-reserved-task-{capability_id}",
                    turn_mode="task",
                    issued_from="task_contract",
                    task_contract_id="task-reserved",
                    requested_capabilities=(capability_id,),
                )
            self.assertEqual(ctx2.exception.code, "DENIED_CAPABILITY")

        for mode in ("chat", "wake", "task"):
            lease = issue_turn_lease(
                turn_id=f"t-default-reserved-{mode}",
                turn_mode=mode,
                issued_from="default_policy",
            )
            self.assertTrue(
                P1_RESERVED_CAPABILITY_IDS.isdisjoint(lease["allowed_capabilities"])
            )

    def test_i_model_cannot_self_sign_lease(self):
        source = Path(__file__).resolve().parents[1] / "tools" / "lease_signer.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        forbidden_names = {
            "mcp",
            "FastMCP",
            "tool",
            "register_tool",
            "claude",
            "openai",
            "provider_bindings",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, "module", None) or ""
                names = [alias.name for alias in node.names]
                joined = " ".join([module, *names]).lower()
                self.assertNotIn("mcp", joined)
                self.assertNotIn("fastapi", joined)
            if isinstance(node, ast.FunctionDef):
                self.assertFalse(node.name.startswith("mcp"))
                for deco in node.decorator_list:
                    deco_src = ast.unparse(deco).lower()
                    for bad in ("mcp", "tool", "app.route", "fastapi"):
                        self.assertNotIn(bad, deco_src)

        sig = inspect.signature(issue_turn_lease)
        self.assertNotIn("model_request", sig.parameters)
        self.assertNotIn("assistant_decision", sig.parameters)

        # Manifest must not bind any provider surface to lease_signer.
        for item in CAPABILITY_MANIFEST:
            bindings = item.get("provider_bindings") or {}
            blob = str(bindings).lower()
            self.assertNotIn("lease_signer", blob)
            self.assertNotIn("issue_turn_lease", blob)

        # Signer only consumes the shared capability dictionary.
        for capability_id in CHAT_DEFAULTS + WAKE_DEFAULTS + TASK_DEFAULTS + ("todo.write",):
            self.assertIsNotNone(get_capability(capability_id))
            self.assertIn(capability_id, P1_ENABLED_CAPABILITY_IDS)


if __name__ == "__main__":
    unittest.main()
