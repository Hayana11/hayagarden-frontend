from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from cryptography.fernet import Fernet

from tools.external_mcp_invocation import (
    FAILED_PRE_CALL,
    OUTCOME_UNKNOWN,
    STARTED,
    SUCCEEDED,
    TOOL_ERROR,
    ExternalInvocationInitializationError,
    ExternalMcpInvocation,
    MAX_TOOL_INPUT_BYTES,
)
from tools.external_mcp_auth_binding import AUTH_NONE, ExternalMcpAuthBindingRegistry
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_execution_fence import ExternalToolExecutionFence, build_external_action_id
from tools.external_tool_registry import ExternalToolCandidateRegistry
from tools.external_tool_side_effect_policy import EXTERNAL_STATE, NONE, ExternalToolSideEffectPolicy


def tool(name="calendar.list", **extra):
    value = {"name": name, "description": "Calendar", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}}
    value.update(extra)
    return value


class ExternalMcpInvocationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.server_registry = ExternalServerRegistry(self.connection, id_factory=lambda: "server-1")
        self.server = self.server_registry.register(display_name="Calendar", endpoint="https://calendar.example/mcp", provenance="owner-admin")
        self.tempdir = tempfile.TemporaryDirectory()
        self.key_path = os.path.join(self.tempdir.name, "key")
        with open(self.key_path, "wb") as key_file:
            key_file.write(Fernet.generate_key())
        if os.name == "posix":
            os.chmod(self.key_path, 0o600)
        self.secret_store = ExternalSecretStore(self.connection, key_file=self.key_path, registry=self.server_registry)
        self.auth_bindings = ExternalMcpAuthBindingRegistry(self.connection, server_registry=self.server_registry, secret_store=self.secret_store)
        self.auth_bindings.set_binding(self.server.server_id, AUTH_NONE)
        self.server = self.server_registry.get(self.server.server_id)
        self.candidates = ExternalToolCandidateRegistry(self.connection, server_registry=self.server_registry)
        self.policy = ExternalToolSideEffectPolicy(self.connection, candidate_registry=self.candidates, server_registry=self.server_registry)
        self.fence = ExternalToolExecutionFence(self.connection, server_registry=self.server_registry, candidate_registry=self.candidates, side_effect_policy=self.policy)
        self.ids = iter(f"id-{number}" for number in range(1, 1000))
        self.invocation = ExternalMcpInvocation(self.connection, server_registry=self.server_registry, candidate_registry=self.candidates, side_effect_policy=self.policy, execution_fence=self.fence, auth_binding_registry=self.auth_bindings, id_factory=lambda: next(self.ids))
        self.calls = 0

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def discovery(self, records, revision=None):
        return {"status": "SUCCESS", "catalog_complete": True, "server_id": self.server.server_id, "registry_revision": self.server.revision if revision is None else revision, "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": records, "diagnostics": {"registry_changed_during_attempt": False}, "model_visible": False, "execution_allowed": False}

    def prepare(self, side_effect_class=NONE):
        self.candidates.ingest(self.discovery([tool()]))
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.candidates.approve_candidate(server_id=self.server.server_id, tool_name="calendar.list", expected_fingerprint=candidate["current_fingerprint"], expected_source_registry_revision=candidate["current_source_registry_revision"], actor="owner", provenance="settings-admin")
        self.policy.classify(server_id=self.server.server_id, tool_name="calendar.list", expected_fingerprint=candidate["current_fingerprint"], expected_source_registry_revision=candidate["current_source_registry_revision"], side_effect_class=side_effect_class, actor="owner", provenance="settings-admin")
        return self.candidates.get_candidate(self.server.server_id, "calendar.list")

    def lease(self, approval_ids=(), turn_id="turn-1"):
        return {"lease_version": 1, "turn_id": turn_id, "turn_mode": "chat", "issued_from": "user_confirmation", "allowed_capabilities": (), "approval_ids": tuple(approval_ids), "task_contract_id": None, "issued_at": "2026-08-29T00:00:00Z"}

    def allowed_lease(self, candidate, value=None, turn_id="turn-1", side_effect_class=NONE):
        action = build_external_action_id(candidate["control_id"], candidate["current_fingerprint"], candidate["current_source_registry_revision"], side_effect_class, value or {"q": "today"})
        return None, action

    def runner(self, status="SUCCESS"):
        def run(envelope):
            self.calls += 1
            return {"status": status}
        return run

    def invoke(self, lease, runner=None, value=None, expected_turn_id="turn-1"):
        return self.invocation.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"} if value is None else value, lease, expected_turn_id=expected_turn_id, runner=runner or self.runner())

    def test_shared_authority_and_no_default_db(self):
        self.assertIs(self.invocation._connection, self.connection)
        other = sqlite3.connect(":memory:")
        try:
            servers = ExternalServerRegistry(other)
            candidates = ExternalToolCandidateRegistry(other, server_registry=servers)
            policy = ExternalToolSideEffectPolicy(other, candidate_registry=candidates, server_registry=servers)
            fence = ExternalToolExecutionFence(other, server_registry=servers, candidate_registry=candidates, side_effect_policy=policy)
            other_secret = ExternalSecretStore(other, key_file=tempfile.NamedTemporaryFile(delete=False).name, registry=servers)
            other_auth = ExternalMcpAuthBindingRegistry(other, server_registry=servers, secret_store=other_secret)
            with self.assertRaises(ExternalInvocationInitializationError):
                ExternalMcpInvocation(self.connection, server_registry=servers, candidate_registry=candidates, side_effect_policy=policy, execution_fence=fence, auth_binding_registry=other_auth)
        finally:
            other.close()

    def test_autonomous_path_never_reads_confirmation_store(self):
        import tools.confirmation_store as confirmation_store

        class Bomb:
            def __init__(self, *args, **kwargs):
                raise AssertionError("confirmation store touched")

        candidate = self.prepare()
        action_lease, _ = self.allowed_lease(candidate)
        previous = confirmation_store.PendingActionStore
        confirmation_store.PendingActionStore = Bomb
        try:
            result = self.invoke(action_lease)
        finally:
            confirmation_store.PendingActionStore = previous
        self.assertEqual(result["status"], SUCCEEDED)
        self.assertEqual(self.calls, 1)

    def test_ask_deny_and_invalid_turn_never_run(self):
        candidate = self.prepare()
        self.assertEqual(self.invoke(self.lease())["status"], FAILED_PRE_CALL)
        self.policy.classify(server_id=self.server.server_id, tool_name="calendar.list", expected_fingerprint=candidate["current_fingerprint"], expected_source_registry_revision=candidate["current_source_registry_revision"], side_effect_class="unknown", actor="owner", provenance="settings-admin")
        self.assertEqual(self.invoke(self.lease())["status"], FAILED_PRE_CALL)
        self.assertEqual(self.calls, 0)
        candidate = self.prepare()
        lease, action = self.allowed_lease(candidate)
        self.assertEqual(self.invoke(lease, expected_turn_id="wrong")["status"], FAILED_PRE_CALL)
        self.assertEqual(self.calls, 0)

    def test_final_fence_invalidation_paths_never_run(self):
        candidate = self.prepare()
        lease, action = self.allowed_lease(candidate)
        self.candidates.reject_candidate(server_id=self.server.server_id, tool_name="calendar.list", expected_fingerprint=candidate["current_fingerprint"], expected_source_registry_revision=candidate["current_source_registry_revision"], actor="owner", provenance="settings-admin")
        self.assertEqual(self.invoke(lease)["status"], FAILED_PRE_CALL)
        candidate = self.prepare()
        lease, _ = self.allowed_lease(candidate)
        self.policy.classify(server_id=self.server.server_id, tool_name="calendar.list", expected_fingerprint=candidate["current_fingerprint"], expected_source_registry_revision=candidate["current_source_registry_revision"], side_effect_class="unknown", actor="owner", provenance="settings-admin")
        self.assertEqual(self.invoke(lease)["status"], FAILED_PRE_CALL)
        candidate = self.prepare()
        self.server_registry.rename(self.server.server_id, "Calendar changed")
        self.assertEqual(self.invoke(self.lease((action,)))["status"], FAILED_PRE_CALL)
        self.assertEqual(self.calls, 0)

    def test_auth_binding_change_invalidates_old_approval_and_confirmation(self):
        """A real binding mutation makes the previously confirmed action stale."""
        old_server = self.server_registry.get(self.server.server_id)
        candidate = self.prepare()
        old_lease, old_action = self.allowed_lease(candidate)
        old_candidate_revision = candidate["current_source_registry_revision"]

        secret = self.secret_store.create(
            server_id=self.server.server_id,
            credential_slot="slot-a",
            secret="opaque-test-value",
        )
        changed_binding = self.auth_bindings.set_binding(
            self.server.server_id,
            "bearer",
            secret_ref=secret.secret_ref,
        )
        new_server = self.server_registry.get(self.server.server_id)
        self.assertGreater(new_server.revision, old_server.revision)
        self.assertEqual(new_server.lifecycle_state, "REVIEW_REQUIRED")
        self.assertEqual(new_server.master_state, "OFF")
        self.assertEqual(changed_binding.auth_scheme, "bearer")

        result = self.invocation.invoke(
            f"ext:{self.server.server_id}:calendar.list",
            {"q": "today"},
            old_lease,
            expected_turn_id="turn-1",
            runner=self.runner(),
        )
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        self.assertEqual(self.calls, 0)
        self.assertNotEqual(old_candidate_revision, new_server.revision)
        self.assertTrue(old_action)

    def test_allow_commits_started_before_runner_and_audits_order(self):
        candidate = self.prepare()
        lease, action = self.allowed_lease(candidate)
        observed = []
        def runner(envelope):
            self.calls += 1
            row = self.connection.execute("SELECT status FROM external_tool_invocation_attempts").fetchone()
            observed.append(row[0])
            self.assertEqual(envelope["server_id"], self.server.server_id)
            self.assertEqual(envelope["tool_name"], "calendar.list")
            self.assertEqual(envelope["endpoint"], self.server.endpoint)
            self.assertEqual(envelope["transport"], self.server.transport)
            self.assertEqual(envelope["source_registry_revision"], candidate["current_source_registry_revision"])
            self.assertEqual(envelope["fingerprint"], candidate["current_fingerprint"])
            self.assertEqual(envelope["external_action_id"], action)
            self.assertEqual(envelope["auth_scheme"], AUTH_NONE)
            self.assertEqual(envelope["auth_binding_revision"], self.auth_bindings.get_binding(self.server.server_id).revision)
            self.assertIsNone(envelope["secret_ref"])
            self.assertIsNone(envelope["credential_slot"])
            self.assertEqual(envelope["tool_input"], {"q": "today"})
            with self.assertRaises(TypeError):
                envelope["server_id"] = "attacker"
            return {"status": "SUCCESS"}
        result = self.invoke(lease, runner)
        self.assertEqual(result["status"], SUCCEEDED)
        self.assertEqual(observed, [STARTED])
        attempt = self.invocation.get_attempt(result["attempt_id"])
        self.assertEqual(attempt["fingerprint"], candidate["current_fingerprint"])
        self.assertEqual([event["status"] for event in self.invocation.list_audit(result["attempt_id"])], ["PRE_CALL", STARTED, SUCCEEDED])

    def test_missing_binding_is_durable_denial_without_runner(self):
        candidate = self.prepare()
        lease, _ = self.allowed_lease(candidate)
        self.connection.execute("DELETE FROM external_mcp_auth_bindings WHERE server_id=?", (self.server.server_id,))
        self.connection.commit()
        result = self.invoke(lease)
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        self.assertEqual(result["reason_code"], "AUTH_BINDING_MISSING")
        self.assertEqual(self.calls, 0)
        attempt = self.invocation.get_attempt(result["attempt_id"])
        self.assertEqual(attempt["reason_code"], "AUTH_BINDING_MISSING")

    def test_started_runner_keeps_frozen_auth_envelope_after_binding_row_change(self):
        candidate = self.prepare()
        lease, _ = self.allowed_lease(candidate)
        seen = []
        def runner(envelope):
            seen.append(dict(envelope))
            self.connection.execute(
                "UPDATE external_mcp_auth_bindings SET auth_scheme='bearer', secret_ref='late-ref', credential_slot='late-slot', revision=revision+1 WHERE server_id=?",
                (self.server.server_id,),
            )
            self.connection.commit()
            return {"status": "SUCCESS"}
        result = self.invoke(lease, runner)
        self.assertEqual(result["status"], SUCCEEDED)
        self.assertEqual(seen[0]["auth_scheme"], AUTH_NONE)
        self.assertIsNone(seen[0]["secret_ref"])
        self.assertIsNone(seen[0]["credential_slot"])
        self.assertEqual(seen[0]["auth_binding_revision"], 1)

    def test_snapshot_limits_and_no_plaintext_persistence(self):
        candidate = self.prepare()
        lease, _ = self.allowed_lease(candidate, {"nested": {"value": 1}})
        input_value = {"nested": {"value": 1}}
        seen = []
        def runner(envelope):
            input_value["nested"]["value"] = 2
            seen.append(envelope["tool_input"])
            return {"status": "SUCCESS"}
        result = self.invoke(lease, runner, input_value)
        self.assertEqual(seen, [{"nested": {"value": 1}}])
        dump = "\n".join(self.connection.iterdump())
        self.assertNotIn("nested", dump)
        self.assertNotIn("today", dump)
        self.assertEqual(self.invoke(lease, value={"n": float("nan")})["status"], FAILED_PRE_CALL)
        self.assertEqual(self.invoke(lease, value={"data": "x" * (MAX_TOOL_INPUT_BYTES + 1)})["status"], FAILED_PRE_CALL)

    def test_duplicate_and_concurrent_duplicate_only_invoke_once(self):
        candidate = self.prepare(EXTERNAL_STATE)
        lease, _ = self.allowed_lease(candidate, side_effect_class=EXTERNAL_STATE)
        first = self.invoke(lease)
        second = self.invoke(lease)
        self.assertEqual(first["status"], SUCCEEDED)
        self.assertEqual(second["reason_code"], "DUPLICATE_EXTERNAL_ACTION")
        self.assertEqual(self.calls, 1)

        # Two connection objects against a durable temporary DB prove the unique key,
        # rather than a process-local flag, owns the race.
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False); handle.close()
        try:
            seed = sqlite3.connect(handle.name); self.connection.backup(seed); seed.close()
            left, right = sqlite3.connect(handle.name, timeout=5, check_same_thread=False), sqlite3.connect(handle.name, timeout=5, check_same_thread=False)
            def owner(conn):
                servers = ExternalServerRegistry(conn); key = tempfile.NamedTemporaryFile(delete=False).name; secrets_store = ExternalSecretStore(conn, key_file=key, registry=servers); auth = ExternalMcpAuthBindingRegistry(conn, server_registry=servers, secret_store=secrets_store); candidates = ExternalToolCandidateRegistry(conn, server_registry=servers); policy = ExternalToolSideEffectPolicy(conn, candidate_registry=candidates, server_registry=servers); fence = ExternalToolExecutionFence(conn, server_registry=servers, candidate_registry=candidates, side_effect_policy=policy)
                return ExternalMcpInvocation(conn, server_registry=servers, candidate_registry=candidates, side_effect_policy=policy, execution_fence=fence, auth_binding_registry=auth)
            one, two = owner(left), owner(right)
            # Use a fresh turn; initial attempt is deliberately already terminal.
            fresh_lease = dict(lease); fresh_lease["turn_id"] = "turn-race"
            fresh_lease["approval_ids"] = (build_external_action_id(candidate["control_id"], candidate["current_fingerprint"], candidate["current_source_registry_revision"], EXTERNAL_STATE, {"q": "today"}),)
            calls = []; start = threading.Barrier(2)
            def race(inv):
                start.wait(); result = inv.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, fresh_lease, expected_turn_id="turn-race", runner=lambda _: calls.append(1) or {"status": "SUCCESS"}); self.assertIn(result["status"], {SUCCEEDED, STARTED})
            threads = [threading.Thread(target=race, args=(owner_,)) for owner_ in (one, two)]
            [thread.start() for thread in threads]; [thread.join(5) for thread in threads]
            self.assertEqual(len(calls), 1)
            left.close(); right.close()
        finally:
            os.unlink(handle.name)

    def test_all_outcomes_exception_and_recovery_are_unknown_without_replay(self):
        for runner_outcome, expected in (("SUCCESS", SUCCEEDED), ("TOOL_ERROR", TOOL_ERROR), ("NOT_INVOKED", FAILED_PRE_CALL), ("OUTCOME_UNKNOWN", OUTCOME_UNKNOWN)):
            with self.subTest(runner_outcome=runner_outcome):
                candidate = self.prepare(); lease, _ = self.allowed_lease(candidate, turn_id=f"turn-{runner_outcome}")
                self.assertEqual(self.invoke(lease, self.runner(runner_outcome), expected_turn_id=f"turn-{runner_outcome}")["status"], expected)
        candidate = self.prepare(); lease, _ = self.allowed_lease(candidate, turn_id="turn-exception")
        self.assertEqual(self.invoke(lease, lambda _: (_ for _ in ()).throw(RuntimeError("lost")), expected_turn_id="turn-exception")["status"], OUTCOME_UNKNOWN)
        candidate = self.prepare(); lease, _ = self.allowed_lease(candidate, turn_id="turn-malformed")
        self.assertEqual(self.invoke(lease, lambda _: {"not_status": "bad"}, expected_turn_id="turn-malformed")["status"], OUTCOME_UNKNOWN)
        candidate = self.prepare(); lease, _ = self.allowed_lease(candidate, turn_id="turn-recover")
        self.connection.execute("BEGIN IMMEDIATE")
        decision = self.fence.evaluate(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, lease, expected_turn_id="turn-recover")
        self.invocation._insert_attempt("stranded", decision, "a" * 64, 1, "PRE_CALL", "PRE_CALL")
        self.invocation._audit("stranded", decision, "PRE_CALL", "PRE_CALL")
        self.invocation._transition("stranded", decision, STARTED, "RUNNER_STARTED")
        self.connection.commit()
        recovered = self.invocation.recover_unknown("stranded")
        self.assertEqual(recovered["status"], OUTCOME_UNKNOWN)
        self.assertEqual(self.invocation.recover_unknown("stranded")["reason_code"], "RECOVERY_NOT_ALLOWED")
        self.assertEqual(self.calls, 4)

    def test_post_call_persistence_failure_is_unknown_and_leaves_started(self):
        candidate = self.prepare(); lease, _ = self.allowed_lease(candidate)
        self.connection.execute("CREATE TRIGGER fail_terminal BEFORE UPDATE ON external_tool_invocation_attempts WHEN OLD.status='STARTED' BEGIN SELECT RAISE(ABORT, 'fail finalization'); END")
        result = self.invoke(lease)
        self.assertEqual(result["status"], OUTCOME_UNKNOWN)
        self.assertEqual(result["reason_code"], "POST_CALL_PERSISTENCE_FAILED")
        self.assertEqual(self.connection.execute("SELECT status FROM external_tool_invocation_attempts").fetchone()[0], STARTED)
        self.assertEqual(self.calls, 1)

    def test_audit_is_append_only(self):
        candidate = self.prepare(); lease, _ = self.allowed_lease(candidate)
        result = self.invoke(lease)
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute("UPDATE external_tool_invocation_audit SET status='x' WHERE attempt_id=?", (result["attempt_id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute("DELETE FROM external_tool_invocation_audit WHERE attempt_id=?", (result["attempt_id"],))

    def test_call_envelope_is_exact_immutable_allow_descriptor(self):
        candidate = self.prepare()
        lease, action = self.allowed_lease(candidate)
        seen = []
        def runner(envelope):
            seen.append(envelope)
            with self.assertRaises(TypeError):
                envelope["endpoint"] = "https://attacker.invalid/mcp"
            self.server_registry.rename(self.server.server_id, "changed after STARTED")
            return {"status": "OUTCOME_UNKNOWN"}
        result = self.invoke(lease, runner)
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        envelope = seen[0]
        self.assertEqual(envelope["server_id"], self.server.server_id)
        self.assertEqual(envelope["tool_name"], "calendar.list")
        self.assertEqual(envelope["fingerprint"], candidate["current_fingerprint"])
        self.assertEqual(envelope["source_registry_revision"], candidate["current_source_registry_revision"])
        self.assertEqual(envelope["external_action_id"], action)
        self.assertEqual(envelope["endpoint"], "https://calendar.example/mcp")
        self.assertEqual(envelope["transport"], "streamable_http")
        self.assertEqual(envelope["tool_input"], {"q": "today"})

    def test_denial_identity_binds_control_and_reason(self):
        runner = self.runner()
        lease = self.lease()
        first = self.invocation.invoke("ext:server-1:missing-a", {"q": "same"}, lease, expected_turn_id="turn-1", runner=runner)
        second = self.invocation.invoke("ext:server-1:missing-b", {"q": "same"}, lease, expected_turn_id="turn-1", runner=runner)
        self.assertEqual(first["status"], FAILED_PRE_CALL)
        self.assertEqual(second["status"], FAILED_PRE_CALL)
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM external_tool_invocation_audit").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
