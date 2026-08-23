from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest

from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_registry import ExternalToolCandidateRegistry
from tools.external_tool_side_effect_policy import (
    CODE_OR_PROCESS,
    EXTERNAL_STATE,
    NONE,
    UNKNOWN,
    ExternalToolSideEffectPolicy,
)
from tools.external_tool_execution_fence import (
    ACTION_NOT_CONFIRMED,
    ACTION_ID_PREFIX,
    ALLOW,
    ALLOW_CURRENT_ACTION,
    APPROVAL_NOT_EFFECTIVE,
    ASK,
    CANDIDATE_NOT_PRESENT,
    CLASSIFICATION_NOT_EFFECTIVE,
    CODE_OR_PROCESS_DENIED,
    DENY,
    ExternalExecutionFenceInitializationError,
    ExternalToolExecutionFence,
    TURN_ID_MISMATCH,
    TURN_LEASE_INVALID,
    TURN_MODE_NOT_ALLOWED,
    UNKNOWN_CONTROL_ID,
    UNKNOWN_SIDE_EFFECT,
    build_external_action_id,
)


def tool(name: str = "calendar.list", **fields):
    value = {
        "name": name,
        "description": "SDK-visible description",
        "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "outputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    value.update(fields)
    return value


class ExternalToolExecutionFenceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.server_registry = ExternalServerRegistry(self.connection)
        self.server = self.server_registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="owner-admin",
        )
        self.candidates = ExternalToolCandidateRegistry(
            self.connection, server_registry=self.server_registry
        )
        self.policy = ExternalToolSideEffectPolicy(
            self.connection,
            candidate_registry=self.candidates,
            server_registry=self.server_registry,
        )
        self.fence = ExternalToolExecutionFence(
            self.connection,
            server_registry=self.server_registry,
            candidate_registry=self.candidates,
            side_effect_policy=self.policy,
        )

    def tearDown(self):
        self.connection.close()

    def result(self, records, *, server=None, revision=None):
        server = server or self.server
        return {
            "status": "SUCCESS",
            "catalog_complete": True,
            "server_id": server.server_id,
            "registry_revision": revision if revision is not None else server.revision,
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "tools": records,
            "diagnostics": {"registry_changed_during_attempt": False},
            "model_visible": False,
            "execution_allowed": False,
        }

    def lease(
        self,
        *,
        turn_id="turn-1",
        mode="chat",
        issued_from="explicit_user_intent",
        approval_ids=(),
        **extra,
    ):
        value = {
            "lease_version": 1,
            "turn_id": turn_id,
            "turn_mode": mode,
            "issued_from": issued_from,
            "allowed_capabilities": (),
            "approval_ids": tuple(approval_ids),
            "task_contract_id": None,
            "issued_at": "2026-08-23T13:00:00Z",
        }
        value.update(extra)
        return value

    def prepare(self, side_effect_class=NONE, record=None):
        record = record or tool()
        self.candidates.ingest(self.result([record]))
        candidate = self.candidates.get_candidate(self.server.server_id, record["name"])
        self.candidates.approve_candidate(
            server_id=self.server.server_id,
            tool_name=record["name"],
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor="owner",
            provenance="settings-admin",
        )
        self.policy.classify(
            server_id=self.server.server_id,
            tool_name=record["name"],
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            side_effect_class=side_effect_class,
            actor="owner",
            provenance="settings-admin",
        )
        return self.candidates.get_candidate(self.server.server_id, record["name"])

    def control_id(self, tool_name="calendar.list"):
        return f"ext:{self.server.server_id}:{tool_name}"

    def action_id(self, candidate, side_effect_class=NONE, tool_input=None):
        return build_external_action_id(
            candidate["control_id"],
            candidate["current_fingerprint"],
            candidate["current_source_registry_revision"],
            side_effect_class,
            tool_input or {"q": "today"},
        )

    def allow_lease(self, action_id, *, turn_id="turn-1"):
        return self.lease(
            turn_id=turn_id,
            issued_from="user_confirmation",
            approval_ids=(action_id,),
        )

    def evaluate(self, lease=None, *, control_id=None, tool_input=None, expected_turn_id="turn-1"):
        return self.fence.evaluate(
            control_id or self.control_id(),
            tool_input if tool_input is not None else {"q": "today"},
            lease or self.lease(),
            expected_turn_id=expected_turn_id,
        )

    def test_valid_control_id_and_frozen_decisions(self):
        candidate = self.prepare(NONE)
        ask = self.evaluate()
        self.assertEqual(ask["decision"], ASK)
        self.assertEqual(ask["reason_code"], ACTION_NOT_CONFIRMED)
        self.assertEqual(ask["control_id"], candidate["control_id"])
        self.assertTrue(ask["external_action_id"].startswith(ACTION_ID_PREFIX))
        allow = self.evaluate(self.allow_lease(ask["external_action_id"]))
        self.assertEqual(allow["decision"], ALLOW)
        self.assertEqual(allow["reason_code"], ALLOW_CURRENT_ACTION)

    def test_invalid_control_id_and_unknown_candidate_deny(self):
        self.assertEqual(
            self.evaluate(control_id="calendar.list")["reason_code"],
            UNKNOWN_CONTROL_ID,
        )
        self.assertEqual(
            self.evaluate(control_id="ext:unknown:calendar.list")["reason_code"],
            UNKNOWN_CONTROL_ID,
        )
        self.assertEqual(
            self.evaluate(control_id="ext::calendar.list")["reason_code"],
            UNKNOWN_CONTROL_ID,
        )

    def test_missing_review_and_approval_failure_deny(self):
        self.candidates.ingest(self.result([tool()]))
        self.assertEqual(self.evaluate()["reason_code"], APPROVAL_NOT_EFFECTIVE)
        self.candidates.ingest(self.result([]))
        self.assertEqual(self.evaluate()["reason_code"], CANDIDATE_NOT_PRESENT)
        candidate = self.prepare(NONE, tool(description="reappeared"))
        self.candidates.reject_candidate(
            server_id=self.server.server_id,
            tool_name="calendar.list",
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor="owner",
            provenance="settings-admin",
        )
        self.assertEqual(self.evaluate()["reason_code"], APPROVAL_NOT_EFFECTIVE)

    def test_missing_and_explicit_unknown_classification_deny(self):
        self.candidates.ingest(self.result([tool()]))
        self.assertEqual(self.evaluate()["reason_code"], APPROVAL_NOT_EFFECTIVE)
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.candidates.approve_candidate(
            server_id=self.server.server_id,
            tool_name="calendar.list",
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor="owner",
            provenance="settings-admin",
        )
        self.assertEqual(self.evaluate()["reason_code"], CLASSIFICATION_NOT_EFFECTIVE)
        self.policy.classify(
            server_id=self.server.server_id,
            tool_name="calendar.list",
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            side_effect_class=UNKNOWN,
            actor="owner",
            provenance="settings-admin",
        )
        result = self.evaluate()
        self.assertEqual(result["decision"], DENY)
        self.assertEqual(result["reason_code"], UNKNOWN_SIDE_EFFECT)

    def test_code_or_process_is_always_deny(self):
        candidate = self.prepare(CODE_OR_PROCESS)
        action = self.action_id(candidate, CODE_OR_PROCESS)
        result = self.evaluate(self.allow_lease(action))
        self.assertEqual(result["decision"], DENY)
        self.assertEqual(result["reason_code"], CODE_OR_PROCESS_DENIED)

    def test_none_and_external_state_need_exact_current_action_grant(self):
        for side_effect_class in (NONE, EXTERNAL_STATE):
            with self.subTest(side_effect_class=side_effect_class):
                candidate = self.prepare(side_effect_class, tool(name=f"calendar.{side_effect_class}"))
                control_id = candidate["control_id"]
                ask = self.fence.evaluate(
                    control_id, {"q": "today"}, self.lease(), expected_turn_id="turn-1"
                )
                self.assertEqual(ask["decision"], ASK)
                self.assertEqual(ask["reason_code"], ACTION_NOT_CONFIRMED)
                allowed = self.fence.evaluate(
                    control_id,
                    {"q": "today"},
                    self.allow_lease(ask["external_action_id"]),
                    expected_turn_id="turn-1",
                )
                self.assertEqual(allowed["decision"], ALLOW)
                self.assertEqual(allowed["side_effect_class"], side_effect_class)

    def test_wake_and_task_are_denied_even_with_grant(self):
        candidate = self.prepare(NONE)
        action = self.action_id(candidate)
        for mode in ("wake", "task"):
            with self.subTest(mode=mode):
                result = self.evaluate(
                    self.lease(mode=mode, issued_from="user_confirmation", approval_ids=(action,)),
                )
                self.assertEqual(result["decision"], DENY)
                self.assertEqual(result["reason_code"], TURN_MODE_NOT_ALLOWED)

    def test_turn_lease_shape_and_turn_binding_fail_closed(self):
        self.prepare(NONE)
        malformed = self.lease(extra_field=True)
        self.assertEqual(self.evaluate(malformed)["reason_code"], TURN_LEASE_INVALID)
        missing = self.lease()
        del missing["approval_ids"]
        self.assertEqual(self.evaluate(missing)["reason_code"], TURN_LEASE_INVALID)
        self.assertEqual(
            self.evaluate(expected_turn_id="another-turn")["reason_code"], TURN_ID_MISMATCH
        )
        self.assertEqual(
            self.evaluate(self.lease(issued_from="user_confirmation"))["reason_code"],
            TURN_LEASE_INVALID,
        )

    def test_turn_lease_cross_field_contract_matches_canonical_signer(self):
        invalid_leases = (
            self.lease(
                issued_from="task_contract",
                mode="chat",
                task_contract_id="contract-1",
            ),
            self.lease(
                issued_from="task_contract",
                mode="task",
                task_contract_id=None,
            ),
            self.lease(
                issued_from="user_confirmation",
                mode="chat",
                task_contract_id="forged-contract",
            ),
        )
        for invalid in invalid_leases:
            with self.subTest(
                issued_from=invalid["issued_from"],
                mode=invalid["turn_mode"],
                task_contract_id=invalid["task_contract_id"],
            ):
                self.assertEqual(
                    self.evaluate(invalid)["reason_code"],
                    TURN_LEASE_INVALID,
                )

        self.prepare(EXTERNAL_STATE)
        ask = self.evaluate()
        forged_confirmation = self.allow_lease(ask["external_action_id"])
        forged_confirmation["task_contract_id"] = "forged-contract"
        self.assertEqual(
            self.evaluate(forged_confirmation)["reason_code"],
            TURN_LEASE_INVALID,
        )

        valid_task_contract = self.lease(
            issued_from="task_contract",
            mode="task",
            task_contract_id="contract-1",
        )
        self.assertEqual(
            self.evaluate(valid_task_contract)["reason_code"],
            TURN_MODE_NOT_ALLOWED,
        )

    def test_non_user_confirmation_lease_cannot_allow(self):
        candidate = self.prepare(NONE)
        action = self.action_id(candidate)
        for source in ("default_policy", "explicit_user_intent", "task_contract"):
            lease = self.lease(
                issued_from=source,
                mode="task" if source == "task_contract" else "chat",
                task_contract_id="contract-1" if source == "task_contract" else None,
            )
            result = self.evaluate(lease)
            self.assertNotEqual(result["decision"], ALLOW)
        self.assertEqual(
            self.evaluate(self.lease(issued_from="user_confirmation", approval_ids=(action,)))["decision"],
            ALLOW,
        )

    def test_action_id_is_canonical_and_binds_every_frozen_field(self):
        control = self.control_id()
        fingerprint = "a" * 64
        base = build_external_action_id(control, fingerprint, 1, NONE, {"a": 1, "b": [1, 2]})
        reordered = build_external_action_id(control, fingerprint, 1, NONE, {"b": [1, 2], "a": 1})
        self.assertEqual(base, reordered)
        self.assertNotEqual(
            base, build_external_action_id(control, fingerprint, 1, NONE, {"a": 1, "b": [2, 1]})
        )
        self.assertNotEqual(base, build_external_action_id(control, "b" * 64, 1, NONE, {"a": 1, "b": [1, 2]}))
        self.assertNotEqual(base, build_external_action_id(control, fingerprint, 2, NONE, {"a": 1, "b": [1, 2]}))
        self.assertNotEqual(base, build_external_action_id(control, fingerprint, 1, EXTERNAL_STATE, {"a": 1, "b": [1, 2]}))
        self.assertNotEqual(base, build_external_action_id("ext:server:other", fingerprint, 1, NONE, {"a": 1, "b": [1, 2]}))

    def test_tool_input_must_be_mapping_and_rejects_nonfinite_values(self):
        candidate = self.prepare(NONE)
        for value in ([], "text", None, {"value": math.nan}, {"value": math.inf}, {"value": -math.inf}):
            result = self.fence.evaluate(
                self.control_id(), value, self.lease(), expected_turn_id="turn-1"
            )
            self.assertEqual(result["decision"], DENY)
            self.assertEqual(result["reason_code"], "INVALID_TOOL_INPUT")
        with self.assertRaises(ValueError):
            build_external_action_id(candidate["control_id"], candidate["current_fingerprint"], 1, NONE, {"value": math.nan})

    def test_allow_returns_frozen_execution_context(self):
        candidate = self.prepare(EXTERNAL_STATE)
        ask = self.evaluate()
        result = self.evaluate(self.allow_lease(ask["external_action_id"]))
        self.assertEqual(
            set(result),
            {
                "decision", "reason_code", "control_id", "server_id", "tool_name",
                "fingerprint", "source_registry_revision", "side_effect_class",
                "external_action_id", "turn_id",
            },
        )
        self.assertEqual(result["fingerprint"], candidate["current_fingerprint"])
        self.assertEqual(result["source_registry_revision"], candidate["current_source_registry_revision"])
        self.assertEqual(result["turn_id"], "turn-1")

    def test_annotations_never_override_explicit_unknown(self):
        candidate = self.prepare(UNKNOWN, tool(annotations={"readOnlyHint": True, "destructiveHint": False}))
        result = self.evaluate()
        self.assertEqual(result["decision"], DENY)
        self.assertEqual(result["reason_code"], UNKNOWN_SIDE_EFFECT)
        self.assertEqual(candidate["model_visible"], 0)
        self.assertEqual(candidate["execution_allowed"], 0)

    def test_caller_cannot_inject_security_facts(self):
        self.prepare(NONE)
        with self.assertRaises(TypeError):
            self.fence.evaluate(
                self.control_id(), {"q": "today"}, self.lease(), expected_turn_id="turn-1",
                approved=True,
            )
        lease = self.lease(review_state="APPROVED", side_effect_class=NONE, fingerprint="a" * 64)
        self.assertEqual(self.evaluate(lease)["reason_code"], TURN_LEASE_INVALID)

    def test_approval_classification_server_and_fingerprint_drift_deny(self):
        candidate = self.prepare(NONE)
        action = self.action_id(candidate)
        self.candidates.reject_candidate(
            server_id=self.server.server_id,
            tool_name="calendar.list",
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor="owner",
            provenance="settings-admin",
        )
        self.assertEqual(self.evaluate(self.allow_lease(action))["reason_code"], APPROVAL_NOT_EFFECTIVE)

        candidate = self.prepare(NONE)
        self.candidates.ingest(self.result([tool(description="changed")]))
        self.assertEqual(self.evaluate(self.allow_lease(action))["reason_code"], APPROVAL_NOT_EFFECTIVE)
        self.server_registry.rename(self.server.server_id, "Calendar v2")
        self.assertEqual(self.evaluate()["reason_code"], APPROVAL_NOT_EFFECTIVE)

    def test_missing_reappear_and_old_action_id_cannot_reuse(self):
        candidate = self.prepare(NONE)
        old_action = self.action_id(candidate)
        self.candidates.ingest(self.result([]))
        self.candidates.ingest(self.result([tool(description="reappeared")]))
        current = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(
            self.evaluate(self.allow_lease(old_action))["reason_code"],
            APPROVAL_NOT_EFFECTIVE,
        )
        self.assertNotEqual(old_action, self.action_id(current))

    def test_shared_authoritative_connection_is_required(self):
        other = sqlite3.connect(":memory:")
        try:
            other_servers = ExternalServerRegistry(other)
            other_candidates = ExternalToolCandidateRegistry(other, server_registry=other_servers)
            other_policy = ExternalToolSideEffectPolicy(
                other, candidate_registry=other_candidates, server_registry=other_servers
            )
            with self.assertRaises(ExternalExecutionFenceInitializationError):
                ExternalToolExecutionFence(
                    self.connection,
                    server_registry=other_servers,
                    candidate_registry=other_candidates,
                    side_effect_policy=other_policy,
                )
        finally:
            other.close()

    def test_evaluate_is_read_only_and_preserves_frozen_flags(self):
        candidate = self.prepare(NONE)
        action = self.action_id(candidate)
        before = self.connection.iterdump().__repr__()
        self.evaluate(self.allow_lease(action))
        after = self.connection.iterdump().__repr__()
        self.assertEqual(before, after)
        self.assertEqual(self.candidates.get_candidate(self.server.server_id, "calendar.list"), candidate)

    def test_real_two_connection_invalidation_races(self):
        self.prepare(NONE)
        path_handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        path = path_handle.name
        path_handle.close()
        seed = sqlite3.connect(path)
        self.connection.backup(seed)
        seed.close()
        first = sqlite3.connect(path, timeout=5, check_same_thread=False)
        second = sqlite3.connect(path, timeout=5, check_same_thread=False)
        try:
            first_servers = ExternalServerRegistry(first)
            first_candidates = ExternalToolCandidateRegistry(first, server_registry=first_servers)
            first_policy = ExternalToolSideEffectPolicy(first, candidate_registry=first_candidates, server_registry=first_servers)
            first_fence = ExternalToolExecutionFence(first, server_registry=first_servers, candidate_registry=first_candidates, side_effect_policy=first_policy)
            second_servers = ExternalServerRegistry(second)
            second_candidates = ExternalToolCandidateRegistry(second, server_registry=second_servers)
            candidate = first_candidates.get_candidate(self.server.server_id, "calendar.list")
            old_action = build_external_action_id(candidate["control_id"], candidate["current_fingerprint"], candidate["current_source_registry_revision"], NONE, {"q": "today"})
            second_server = second_servers.get(self.server.server_id)
            changed_result = {
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": second_server.server_id, "registry_revision": second_server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [tool(description="race")],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            }
            committed = threading.Event()

            def invalidate():
                second_candidates.ingest(changed_result)
                committed.set()

            thread = threading.Thread(target=invalidate)
            thread.start()
            self.assertTrue(committed.wait(5))
            thread.join(5)
            result = first_fence.evaluate(
                candidate["control_id"], {"q": "today"}, self.allow_lease(old_action), expected_turn_id="turn-1"
            )
            self.assertEqual(result["decision"], DENY)
            self.assertIn(result["reason_code"], {APPROVAL_NOT_EFFECTIVE, CLASSIFICATION_NOT_EFFECTIVE})

            current = first_candidates.get_candidate(self.server.server_id, "calendar.list")
            first_candidates.approve_candidate(
                server_id=self.server.server_id, tool_name="calendar.list",
                expected_fingerprint=current["current_fingerprint"],
                expected_source_registry_revision=current["current_source_registry_revision"],
                actor="owner", provenance="settings-admin",
            )
            first_policy.classify(
                server_id=self.server.server_id, tool_name="calendar.list",
                expected_fingerprint=current["current_fingerprint"],
                expected_source_registry_revision=current["current_source_registry_revision"],
                side_effect_class=NONE, actor="owner", provenance="settings-admin",
            )
            ask = first_fence.evaluate(candidate["control_id"], {"q": "today"}, self.lease(), expected_turn_id="turn-1")
            self.assertEqual(ask["decision"], ASK)
            allowed = first_fence.evaluate(candidate["control_id"], {"q": "today"}, self.allow_lease(ask["external_action_id"]), expected_turn_id="turn-1")
            self.assertEqual(allowed["decision"], ALLOW)
            committed.clear()
            def reject_current():
                current_now = second_candidates.get_candidate(self.server.server_id, "calendar.list")
                second_candidates.reject_candidate(
                    server_id=self.server.server_id,
                    tool_name="calendar.list",
                    expected_fingerprint=current_now["current_fingerprint"],
                    expected_source_registry_revision=current_now["current_source_registry_revision"],
                    actor="owner",
                    provenance="settings-admin",
                )
                committed.set()

            thread = threading.Thread(target=reject_current)
            thread.start()
            self.assertTrue(committed.wait(5))
            thread.join(5)
            self.assertEqual(first_fence.evaluate(candidate["control_id"], {"q": "today"}, self.allow_lease(ask["external_action_id"]), expected_turn_id="turn-1")["decision"], DENY)
        finally:
            first.close()
            second.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
