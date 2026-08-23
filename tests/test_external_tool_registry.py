from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest

from tools.external_server_registry import ExternalServerRegistry, REVOKED_STATE
from tools.external_tool_registry import (
    APPROVED,
    ExternalToolCandidateRegistry,
    MISSING,
    PRESENT,
    REVIEW_REQUIRED,
    ToolCatalogValidationError,
    ToolRegistryRejectedError,
    canonical_json,
    fingerprint_raw_tool,
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


class ExternalToolCandidateRegistryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.server_registry = ExternalServerRegistry(self.connection)
        self.server = self.server_registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="owner-admin",
        )
        self.owner = ExternalToolCandidateRegistry(
            self.connection, server_registry=self.server_registry
        )

    def tearDown(self):
        self.connection.close()

    def result(self, tools, **overrides):
        value = {
            "status": "SUCCESS",
            "catalog_complete": True,
            "zero_tools": not tools,
            "server_id": self.server.server_id,
            "registry_revision": self.server.revision,
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "tools": tools,
            "diagnostics": {"registry_changed_during_attempt": False},
            "model_visible": False,
            "execution_allowed": False,
        }
        value.update(overrides)
        return value

    def assertRejectedWithoutWrites(self, result, code=None):
        before = self.connection.execute(
            "SELECT COUNT(*) FROM external_tool_candidate_registry"
        ).fetchone()[0]
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.ingest(result)
        if code:
            self.assertEqual(raised.exception.code, code)
        after = self.connection.execute(
            "SELECT COUNT(*) FROM external_tool_candidate_registry"
        ).fetchone()[0]
        self.assertEqual(before, after)

    def approve(self, tool_name="calendar.list", *, actor="owner", provenance="settings-admin"):
        candidate = self.owner.get_candidate(self.server.server_id, tool_name)
        return self.owner.approve_candidate(
            server_id=self.server.server_id,
            tool_name=tool_name,
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor=actor,
            provenance=provenance,
        )

    def reject(self, tool_name="calendar.list", *, actor="owner", provenance="settings-admin"):
        candidate = self.owner.get_candidate(self.server.server_id, tool_name)
        return self.owner.reject_candidate(
            server_id=self.server.server_id,
            tool_name=tool_name,
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor=actor,
            provenance=provenance,
        )

    def test_only_complete_current_success_is_accepted(self):
        self.assertEqual(self.owner.ingest(self.result([tool()]))["present_tool_count"], 1)
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, "calendar.list")[
                "current_source_registry_revision"
            ],
            self.server.revision,
        )
        for overrides, code in (
            ({"status": "BRIDGE_ERROR"}, "INCOMPLETE_DISCOVERY"),
            ({"catalog_complete": False}, "INCOMPLETE_DISCOVERY"),
            ({"tool_record_boundary": "NORMALIZED"}, "UNSUPPORTED_RECORD_BOUNDARY"),
            ({"diagnostics": {"registry_changed_during_attempt": True}}, "STALE_DISCOVERY"),
            ({"diagnostics": {}}, "STALE_DISCOVERY"),
        ):
            self.assertRejectedWithoutWrites(self.result([tool("other")], **overrides), code)

    def test_new_schema_exposes_nullable_current_source_revision(self):
        columns = {
            row[1]: row
            for row in self.connection.execute(
                "PRAGMA table_info(external_tool_candidate_registry)"
            ).fetchall()
        }
        self.assertIn("current_source_registry_revision", columns)
        self.assertEqual(columns["current_source_registry_revision"][3], 0)
        self.assertEqual(
            self.owner.ingest(self.result([tool()]))["registry_revision"],
            self.server.revision,
        )

    def test_server_gate_is_atomic_and_fail_closed(self):
        self.assertRejectedWithoutWrites(
            self.result([], registry_revision=self.server.revision + 1), "REVISION_MISMATCH"
        )
        self.assertRejectedWithoutWrites(
            self.result([], server_id="unknown-server"), "UNKNOWN_SERVER"
        )
        self.server_registry.revoke(self.server.server_id)
        self.assertRejectedWithoutWrites(self.result([]), "REVOKED_SERVER")

    def test_injected_server_registry_is_the_only_authority(self):
        candidate_connection = sqlite3.connect(":memory:")
        authority_connection = sqlite3.connect(":memory:")
        try:
            stale_registry = ExternalServerRegistry(
                candidate_connection, id_factory=lambda: self.server.server_id
            )
            stale_registry.register(
                display_name="Stale copy",
                endpoint="https://stale-copy.example/mcp",
                provenance="test",
            )
            authority_registry = ExternalServerRegistry(
                authority_connection, id_factory=lambda: self.server.server_id
            )
            authority_registry.register(
                display_name="Authoritative",
                endpoint="https://authoritative.example/mcp",
                provenance="test",
            )
            with self.assertRaises(ToolRegistryRejectedError) as raised:
                ExternalToolCandidateRegistry(
                    candidate_connection, server_registry=authority_registry
                )
            self.assertEqual(raised.exception.code, "SERVER_AUTHORITY_MISMATCH")
            self.assertEqual(
                candidate_connection.execute(
                    "SELECT COUNT(*) FROM external_server_registry"
                ).fetchone()[0],
                1,
            )
        finally:
            candidate_connection.close()
            authority_connection.close()

    def test_registered_and_review_required_are_allowed_but_server_owner_is_unchanged(self):
        self.owner.ingest(self.result([tool()]))
        row_before = self.connection.execute(
            "SELECT server_id, endpoint, lifecycle_state, revision FROM external_server_registry"
        ).fetchone()
        changed = self.server_registry.update_connection(
            self.server.server_id, endpoint="https://calendar-2.example/mcp"
        )
        accepted = self.result([], registry_revision=changed.revision)
        accepted["server_id"] = changed.server_id
        self.assertEqual(self.owner.ingest(accepted)["missing_tool_count"], 1)
        row_after = self.connection.execute(
            "SELECT server_id, endpoint, lifecycle_state, revision FROM external_server_registry"
        ).fetchone()
        self.assertEqual(row_after[0], row_before[0])
        self.assertEqual(row_after[1], "https://calendar-2.example/mcp")
        self.assertEqual(row_after[2], "REVIEW_REQUIRED")
        self.assertEqual(row_after[3], changed.revision)
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(candidate["presence_state"], MISSING)
        self.assertEqual(candidate["current_source_registry_revision"], changed.revision)

    def test_same_fingerprint_cross_revision_updates_candidate_not_snapshot(self):
        first = tool()
        self.owner.ingest(self.result([first]))
        first_candidate = self.owner.get_candidate(self.server.server_id, first["name"])
        first_snapshot = self.owner.list_snapshots(self.server.server_id, first["name"])
        self.assertEqual(first_candidate["current_source_registry_revision"], self.server.revision)
        self.assertEqual(first_snapshot[0]["source_registry_revision"], self.server.revision)
        self.approve()

        renamed = self.server_registry.rename(self.server.server_id, "Calendar v2")
        self.owner.ingest(self.result([first], registry_revision=renamed.revision))
        current = self.owner.get_candidate(self.server.server_id, first["name"])
        snapshots = self.owner.list_snapshots(self.server.server_id, first["name"])
        self.assertEqual(current["current_source_registry_revision"], renamed.revision)
        self.assertEqual(current["current_fingerprint"], first_candidate["current_fingerprint"])
        self.assertEqual(current["review_state"], REVIEW_REQUIRED)
        self.assertEqual(current["revision"], 3)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, first["name"]))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["source_registry_revision"], self.server.revision)
        listed = self.owner.list_candidates(self.server.server_id)[0]
        self.assertEqual(listed["current_source_registry_revision"], renamed.revision)

    def test_identity_names_and_atomic_validation(self):
        with self.assertRaises(ToolCatalogValidationError):
            self.owner.ingest(self.result([tool(), tool()]))
        for name in ("", " calendar.list", "calendar.list ", "bad\nname", "é" * 200):
            with self.assertRaises(ToolCatalogValidationError):
                self.owner.ingest(self.result([tool(name)]))
        with self.assertRaises(ToolCatalogValidationError):
            self.owner.ingest(self.result([tool("good"), {"name": "bad", "x": math.nan}]))
        self.owner.ingest(self.result([tool()]))
        row = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(row["control_id"], f"ext:{self.server.server_id}:calendar.list")
        self.assertEqual(row["model_visible"], 0)
        self.assertEqual(row["execution_allowed"], 0)

    def test_fingerprint_is_full_raw_record_and_canonical(self):
        first = {"name": "x", "nested": {"b": 2, "a": 1}, "array": [1, 2]}
        reordered = {"array": [1, 2], "nested": {"a": 1, "b": 2}, "name": "x"}
        self.assertEqual(fingerprint_raw_tool(first), fingerprint_raw_tool(reordered))
        self.assertNotEqual(fingerprint_raw_tool(first), fingerprint_raw_tool({**first, "array": [2, 1]}))
        self.assertNotEqual(fingerprint_raw_tool(first), fingerprint_raw_tool({**first, "new": "field"}))
        with self.assertRaises(ToolCatalogValidationError):
            fingerprint_raw_tool({"name": "x", "value": float("inf")})
        self.owner.ingest(self.result([first]))
        snapshot = self.owner.list_snapshots(self.server.server_id, "x")[0]
        self.assertEqual(json.loads(snapshot["raw_snapshot_json"]), first)
        self.assertEqual(snapshot["fingerprint"], fingerprint_raw_tool(first))

    def test_new_unchanged_approved_drift_missing_reappear_and_rename(self):
        first = tool()
        self.owner.ingest(self.result([first]))
        candidate = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(candidate["presence_state"], PRESENT)
        self.assertEqual(candidate["review_state"], REVIEW_REQUIRED)
        self.assertEqual(candidate["current_source_registry_revision"], self.server.revision)

        self.approve()
        self.owner.ingest(self.result([first]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["review_state"], APPROVED
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])[
                "current_source_registry_revision"
            ],
            self.server.revision,
        )
        drifted = tool(description="changed")
        self.owner.ingest(self.result([drifted]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["review_state"], REVIEW_REQUIRED
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])[
                "current_source_registry_revision"
            ],
            self.server.revision,
        )
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, first["name"])), 2)
        self.owner.ingest(self.result([]))
        missing = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(missing["presence_state"], MISSING)
        self.assertEqual(missing["review_state"], REVIEW_REQUIRED)
        self.assertEqual(missing["current_source_registry_revision"], self.server.revision)
        self.owner.ingest(self.result([drifted]))
        reappeared = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(reappeared["presence_state"], PRESENT)
        self.assertEqual(reappeared["review_state"], REVIEW_REQUIRED)
        self.assertEqual(
            reappeared["current_source_registry_revision"], self.server.revision
        )
        self.owner.ingest(self.result([tool("calendar.renamed")]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["presence_state"], MISSING
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])[
                "current_source_registry_revision"
            ],
            self.server.revision,
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, "calendar.renamed")["review_state"], REVIEW_REQUIRED
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, "calendar.renamed")[
                "current_source_registry_revision"
            ],
            self.server.revision,
        )

    def test_zero_tools_marks_every_previous_tool_missing_and_deduplicates_snapshot(self):
        self.owner.ingest(self.result([tool("a"), tool("b")]))
        self.owner.ingest(self.result([tool("a"), tool("b")]))
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, "a")), 1)
        revised = self.server_registry.rename(self.server.server_id, "Calendar zero")
        self.owner.ingest(self.result([], registry_revision=revised.revision))
        self.assertEqual(
            [row["presence_state"] for row in self.owner.list_candidates(self.server.server_id)],
            [MISSING, MISSING],
        )
        for row in self.owner.list_candidates(self.server.server_id):
            self.assertEqual(row["current_source_registry_revision"], revised.revision)

    def test_rejected_result_cannot_supply_identity_or_approval(self):
        for field, value in (("control_id", "caller"), ("review_state", APPROVED), ("approved", True)):
            self.assertRejectedWithoutWrites(self.result([], **{field: value}), "UNSUPPORTED_DISCOVERY_FIELD")
        self.assertRejectedWithoutWrites(self.result([], model_visible=True), "UNSAFE_DISCOVERY_RESULT")
        self.assertTrue(callable(self.owner.approve_candidate))
        self.assertTrue(callable(self.owner.reject_candidate))

    def test_b2_schema_is_idempotent_and_legacy_approval_has_no_audit(self):
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("external_tool_approval_baselines", tables)
        self.assertIn("external_tool_review_audit", tables)
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.owner.initialize()
        after = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM external_tool_review_audit"
            ).fetchone()[0],
            0,
        )

    def test_approve_binds_current_candidate_snapshot_and_server_without_enabling(self):
        self.owner.ingest(self.result([tool()]))
        before_server = self.server_registry.get(self.server.server_id)
        result = self.approve()
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        baseline = self.owner.get_approval_baseline(self.server.server_id, "calendar.list")
        audit = self.owner.list_review_audit(self.server.server_id, "calendar.list")
        self.assertTrue(result["changed"])
        self.assertEqual(candidate["review_state"], APPROVED)
        self.assertEqual(baseline["approved_fingerprint"], candidate["current_fingerprint"])
        self.assertEqual(
            baseline["approved_source_registry_revision"],
            candidate["current_source_registry_revision"],
        )
        self.assertEqual(baseline["approved_actor"], "owner")
        self.assertEqual(baseline["approved_provenance"], "settings-admin")
        self.assertEqual(audit[0]["decision"], "APPROVE")
        self.assertEqual(audit[0]["actor"], "owner")
        self.assertEqual(audit[0]["provenance"], "settings-admin")
        self.assertEqual(candidate["model_visible"], 0)
        self.assertEqual(candidate["execution_allowed"], 0)
        self.assertEqual(self.server_registry.get(self.server.server_id), before_server)
        self.assertTrue(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])

    def test_reject_clears_baseline_but_keeps_candidate_snapshot_and_server(self):
        self.owner.ingest(self.result([tool()]))
        self.approve()
        before_server = self.server_registry.get(self.server.server_id)
        result = self.reject()
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertTrue(result["changed"])
        self.assertEqual(candidate["review_state"], REVIEW_REQUIRED)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.list"))
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, "calendar.list")), 1)
        self.assertEqual(self.owner.list_review_audit(self.server.server_id, "calendar.list")[-1]["decision"], "REJECT")
        self.assertEqual(self.server_registry.get(self.server.server_id), before_server)
        self.assertFalse(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])

    def test_review_gates_require_present_fresh_exact_server_and_snapshot(self):
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.approve_candidate(
                server_id=self.server.server_id, tool_name="missing.tool",
                expected_fingerprint="0" * 64, expected_source_registry_revision=self.server.revision,
                actor="owner", provenance="settings-admin",
            )
        self.assertEqual(raised.exception.code, "UNKNOWN_CANDIDATE")
        self.owner.ingest(self.result([tool()]))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        cases = (
            ("FINGERPRINT_MISMATCH", {"expected_fingerprint": "0" * 64}),
            ("SOURCE_REVISION_MISMATCH", {"expected_source_registry_revision": candidate["current_source_registry_revision"] + 1}),
        )
        for code, overrides in cases:
            values = {
                "server_id": self.server.server_id,
                "tool_name": "calendar.list",
                "expected_fingerprint": candidate["current_fingerprint"],
                "expected_source_registry_revision": candidate["current_source_registry_revision"],
                "actor": "owner", "provenance": "settings-admin",
            }
            values.update(overrides)
            with self.assertRaises(ToolRegistryRejectedError) as raised:
                self.owner.approve_candidate(**values)
            self.assertEqual(raised.exception.code, code)
        self.connection.execute(
            "UPDATE external_tool_candidate_registry SET current_source_registry_revision=NULL "
            "WHERE server_id=? AND tool_name=?", (self.server.server_id, "calendar.list")
        )
        self.connection.commit()
        values = {
            "server_id": self.server.server_id,
            "tool_name": "calendar.list",
            "expected_fingerprint": candidate["current_fingerprint"],
            "expected_source_registry_revision": self.server.revision,
            "actor": "owner", "provenance": "settings-admin",
        }
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.approve_candidate(**values)
        self.assertEqual(raised.exception.code, "FRESHNESS_UNKNOWN")

    def test_unknown_and_revoked_servers_fail_closed(self):
        self.owner.ingest(self.result([tool()]))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        values = {
            "server_id": "unknown-server",
            "tool_name": "calendar.list",
            "expected_fingerprint": candidate["current_fingerprint"],
            "expected_source_registry_revision": candidate["current_source_registry_revision"],
            "actor": "owner", "provenance": "settings-admin",
        }
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.approve_candidate(**values)
        self.assertEqual(raised.exception.code, "UNKNOWN_CANDIDATE")
        self.connection.execute(
            "DELETE FROM external_server_registry WHERE server_id=?",
            (self.server.server_id,),
        )
        self.connection.commit()
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.approve_candidate(
                server_id=self.server.server_id, tool_name="calendar.list",
                expected_fingerprint=candidate["current_fingerprint"],
                expected_source_registry_revision=candidate["current_source_registry_revision"],
                actor="owner", provenance="settings-admin",
            )
        self.assertEqual(raised.exception.code, "UNKNOWN_SERVER")

        self.server = self.server_registry.register(
            display_name="Calendar replacement",
            endpoint="https://calendar-replacement.example/mcp",
            provenance="owner-admin",
        )
        self.owner.ingest(self.result([tool()]))
        self.connection.execute(
            "UPDATE external_server_registry SET lifecycle_state='REVOKED' WHERE server_id=?",
            (self.server.server_id,),
        )
        self.connection.commit()
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.approve()
        self.assertEqual(raised.exception.code, "REVOKED_SERVER")

    def test_raw_snapshot_missing_or_tampered_rejects_atomically(self):
        self.owner.ingest(self.result([tool()]))
        candidate_before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.connection.execute(
            "DELETE FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=?",
            (self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.approve()
        self.assertEqual(raised.exception.code, "SNAPSHOT_MISSING")
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list"), candidate_before)
        self.owner.ingest(self.result([tool()]))
        self.connection.execute(
            "UPDATE external_tool_raw_snapshots SET raw_snapshot_json='{}' "
            "WHERE server_id=? AND tool_name=?", (self.server.server_id, "calendar.list")
        )
        self.connection.commit()
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.approve()
        self.assertEqual(raised.exception.code, "SNAPSHOT_MISMATCH")
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.list"))
        self.assertEqual(len(self.owner.list_review_audit(self.server.server_id, "calendar.list")), 0)

    def test_review_required_server_is_reviewable_but_registry_is_not_changed(self):
        self.owner.ingest(self.result([tool()]))
        changed = self.server_registry.update_connection(
            self.server.server_id, endpoint="https://calendar-v2.example/mcp"
        )
        self.owner.ingest(self.result([tool()], registry_revision=changed.revision))
        before = self.server_registry.get(self.server.server_id)
        result = self.approve()
        self.assertTrue(result["changed"])
        self.assertEqual(self.server_registry.get(self.server.server_id), before)
        self.assertTrue(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])

    def test_missing_candidate_cannot_be_rejected_as_a_current_version(self):
        self.owner.ingest(self.result([tool()]))
        self.owner.ingest(self.result([]))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        with self.assertRaises(ToolRegistryRejectedError) as raised:
            self.owner.reject_candidate(
                server_id=self.server.server_id, tool_name="calendar.list",
                expected_fingerprint=candidate["current_fingerprint"],
                expected_source_registry_revision=candidate["current_source_registry_revision"],
                actor="owner", provenance="settings-admin",
            )
        self.assertEqual(raised.exception.code, "CANDIDATE_NOT_PRESENT")

    def test_actor_and_provenance_are_strict_backend_metadata(self):
        self.owner.ingest(self.result([tool()]))
        for actor, provenance, code in (
            ("", "settings-admin", "INVALID_ACTOR"),
            (" owner", "settings-admin", "INVALID_ACTOR"),
            ("owner", " settings-admin", "INVALID_PROVENANCE"),
            ("anonymous", "settings-admin", "INVALID_ACTOR"),
            (None, "settings-admin", "INVALID_ACTOR"),
            ("owner\n", "settings-admin", "INVALID_ACTOR"),
        ):
            with self.assertRaises(ToolRegistryRejectedError) as raised:
                self.approve(actor=actor, provenance=provenance)
            self.assertEqual(raised.exception.code, code)

    def test_same_decision_is_idempotent_but_new_actor_is_a_new_review(self):
        self.owner.ingest(self.result([tool()]))
        first = self.approve()
        candidate_after_first = self.owner.get_candidate(self.server.server_id, "calendar.list")
        second = self.approve()
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(len(self.owner.list_review_audit(self.server.server_id, "calendar.list")), 1)
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, "calendar.list")["revision"],
            candidate_after_first["revision"],
        )
        third = self.approve(actor="second-owner", provenance="owner-control-panel")
        self.assertTrue(third["changed"])
        self.assertEqual(len(self.owner.list_review_audit(self.server.server_id, "calendar.list")), 2)
        self.assertEqual(self.owner.get_approval_baseline(self.server.server_id, "calendar.list")["approved_actor"], "second-owner")

    def test_reject_idempotence_and_approve_reject_approve_history(self):
        self.owner.ingest(self.result([tool()]))
        first = self.reject()
        second = self.reject()
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(len(self.owner.list_review_audit(self.server.server_id, "calendar.list")), 1)
        self.approve()
        self.reject()
        self.approve()
        self.assertEqual(
            [row["decision"] for row in self.owner.list_review_audit(self.server.server_id, "calendar.list")],
            ["REJECT", "APPROVE", "REJECT", "APPROVE"],
        )
        self.assertTrue(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])

    def test_fingerprint_and_server_revision_drift_clear_baseline(self):
        first = tool()
        self.owner.ingest(self.result([first]))
        self.approve()
        self.owner.ingest(self.result([tool(description="changed")]))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(candidate["review_state"], REVIEW_REQUIRED)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.list"))
        self.owner.ingest(self.result([tool(description="changed")]))
        changed_server = self.server_registry.rename(self.server.server_id, "Calendar v2")
        self.owner.ingest(self.result([tool(description="changed")], registry_revision=changed_server.revision))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(candidate["review_state"], REVIEW_REQUIRED)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.list"))

    def test_missing_reappear_and_rename_never_restore_approval(self):
        first = tool()
        self.owner.ingest(self.result([first]))
        self.approve()
        self.owner.ingest(self.result([]))
        self.assertEqual(self.owner.get_candidate(self.server.server_id, first["name"])["presence_state"], MISSING)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, first["name"]))
        self.owner.ingest(self.result([first]))
        self.assertEqual(self.owner.get_candidate(self.server.server_id, first["name"])["review_state"], REVIEW_REQUIRED)
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, first["name"]))
        self.owner.ingest(self.result([tool("calendar.renamed")]))
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.list"))
        self.assertIsNone(self.owner.get_approval_baseline(self.server.server_id, "calendar.renamed"))

    def test_effective_approval_requires_all_fields_and_read_does_not_write(self):
        self.owner.ingest(self.result([tool()]))
        self.approve()
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.connection.execute(
            "UPDATE external_tool_approval_baselines SET approved_fingerprint=? "
            "WHERE server_id=? AND tool_name=?",
            ("0" * 64, self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        self.assertFalse(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])
        self.connection.execute(
            "UPDATE external_tool_approval_baselines SET approved_fingerprint=? "
            "WHERE server_id=? AND tool_name=?",
            (before["current_fingerprint"], self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        self.server_registry.rename(self.server.server_id, "Calendar changed")
        self.assertFalse(self.owner.get_effective_approval(self.server.server_id, "calendar.list")["effective_approved"])
        after = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(after["revision"], before["revision"])

    def test_audit_is_append_only_and_annotations_do_not_auto_approve(self):
        self.owner.ingest(self.result([tool(annotations={"readOnlyHint": False, "destructiveHint": False})]))
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list")["review_state"], REVIEW_REQUIRED)
        self.approve()
        audit_id = self.owner.list_review_audit(self.server.server_id, "calendar.list")[0]["review_event_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE external_tool_review_audit SET actor='tampered' WHERE review_event_id=?",
                (audit_id,),
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM external_tool_review_audit WHERE review_event_id=?",
                (audit_id,),
            )
        self.connection.rollback()

    def test_concurrent_reviews_are_serial_and_never_tear_baseline(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
            path = handle.name
        first_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        second_connection = None
        try:
            first_servers = ExternalServerRegistry(first_connection)
            first_server = first_servers.register(
                display_name="Concurrent review",
                endpoint="https://review.example/mcp",
                provenance="test",
            )
            first_owner = ExternalToolCandidateRegistry(first_connection, server_registry=first_servers)
            first_owner.ingest({
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": first_server.server_id, "registry_revision": first_server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [tool()],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            })
            second_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            second_servers = ExternalServerRegistry(second_connection)
            second_owner = ExternalToolCandidateRegistry(second_connection, server_registry=second_servers)
            candidate = first_owner.get_candidate(first_server.server_id, "calendar.list")
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def run(owner, actor):
                try:
                    barrier.wait()
                    results.append(owner.approve_candidate(
                        server_id=first_server.server_id,
                        tool_name="calendar.list",
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate["current_source_registry_revision"],
                        actor=actor, provenance="settings-admin",
                    ))
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(first_owner, "owner-a")),
                threading.Thread(target=run, args=(second_owner, "owner-b")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            row = first_owner.get_candidate(first_server.server_id, "calendar.list")
            baseline = first_owner.get_approval_baseline(first_server.server_id, "calendar.list")
            self.assertEqual(row["review_state"], APPROVED)
            self.assertIsNotNone(baseline)
            self.assertEqual(baseline["approved_fingerprint"], row["current_fingerprint"])
            self.assertEqual(
                baseline["approved_source_registry_revision"],
                row["current_source_registry_revision"],
            )
            self.assertEqual(len(first_owner.list_review_audit(first_server.server_id, "calendar.list")), 2)
        finally:
            first_connection.close()
            if second_connection is not None:
                second_connection.close()
            import os
            os.unlink(path)

    def test_review_and_discovery_cross_races_are_fail_closed(self):
        def make_case():
            handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
            path = handle.name
            handle.close()
            connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            servers = ExternalServerRegistry(connection)
            server = servers.register(
                display_name="Cross race",
                endpoint="https://cross-race.example/mcp",
                provenance="test",
            )
            owner = ExternalToolCandidateRegistry(connection, server_registry=servers)
            original = tool()
            owner.ingest({
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [original],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            })
            other_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            other_servers = ExternalServerRegistry(other_connection)
            other_owner = ExternalToolCandidateRegistry(
                other_connection, server_registry=other_servers
            )
            return path, connection, owner, other_connection, other_owner, server, original

        def discovery_result(server, catalog):
            return {
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": catalog,
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            }

        # Discovery takes the write lock first. The review starts while that
        # transaction is held and must reject the stale expected fingerprint.
        path, connection, owner, other_connection, other_owner, server, original = make_case()
        entered = threading.Event()
        release = threading.Event()
        callback_calls = [0]

        def pause_discovery():
            if callback_calls[0] == 0:
                callback_calls[0] += 1
                entered.set()
                self.assertTrue(release.wait(5))
            return 0

        try:
            connection.create_function("pause_discovery", 0, pause_discovery)
            connection.executescript(
                """
                CREATE TRIGGER pause_cross_race_discovery
                BEFORE UPDATE OF current_fingerprint ON external_tool_candidate_registry
                BEGIN
                    SELECT pause_discovery();
                END;
                """
            )
            candidate = owner.get_candidate(server.server_id, original["name"])
            discovery_errors = []
            review_errors = []
            review_started = threading.Event()

            def discover_new_version():
                try:
                    owner.ingest(discovery_result(server, [tool(description="new")]))
                except Exception as exc:  # pragma: no cover - asserted below
                    discovery_errors.append(exc)

            def review_old_version():
                review_started.set()
                try:
                    other_owner.approve_candidate(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate["current_source_registry_revision"],
                        actor="owner", provenance="settings-admin",
                    )
                except Exception as exc:
                    review_errors.append(exc)

            discovery_thread = threading.Thread(target=discover_new_version)
            review_thread = threading.Thread(target=review_old_version)
            discovery_thread.start()
            self.assertTrue(entered.wait(5))
            review_thread.start()
            self.assertTrue(review_started.wait(5))
            time.sleep(0.05)
            release.set()
            discovery_thread.join(5)
            review_thread.join(5)
            self.assertFalse(discovery_thread.is_alive())
            self.assertFalse(review_thread.is_alive())
            self.assertEqual(discovery_errors, [])
            self.assertEqual(len(review_errors), 1)
            self.assertEqual(review_errors[0].code, "FINGERPRINT_MISMATCH")
            final = owner.get_candidate(server.server_id, original["name"])
            self.assertEqual(final["review_state"], REVIEW_REQUIRED)
            self.assertIsNone(owner.get_approval_baseline(server.server_id, original["name"]))
            self.assertEqual(owner.list_review_audit(server.server_id, original["name"]), ())
        finally:
            connection.close()
            other_connection.close()
            os.unlink(path)

        # Review takes the write lock first. Discovery starts concurrently,
        # then commits a new version and must invalidate the approval baseline.
        path, connection, owner, other_connection, other_owner, server, original = make_case()
        entered = threading.Event()
        release = threading.Event()
        callback_calls = [0]

        def pause_review():
            if callback_calls[0] == 0:
                callback_calls[0] += 1
                entered.set()
                self.assertTrue(release.wait(5))
            return 0

        try:
            connection.create_function("pause_review", 0, pause_review)
            other_connection.create_function("pause_review", 0, lambda: 0)
            connection.executescript(
                """
                CREATE TRIGGER pause_cross_race_review
                BEFORE UPDATE OF review_state ON external_tool_candidate_registry
                BEGIN
                    SELECT pause_review();
                END;
                """
            )
            candidate = owner.get_candidate(server.server_id, original["name"])
            review_results = []
            discovery_results = []
            discovery_started = threading.Event()

            def approve_current_version():
                try:
                    review_results.append(owner.approve_candidate(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate["current_source_registry_revision"],
                        actor="owner", provenance="settings-admin",
                    ))
                except Exception as exc:  # pragma: no cover - asserted below
                    review_results.append(exc)

            def discover_after_review_starts():
                discovery_started.set()
                try:
                    discovery_results.append(
                        other_owner.ingest(discovery_result(server, [tool(description="new")]))
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    discovery_results.append(exc)

            review_thread = threading.Thread(target=approve_current_version)
            discovery_thread = threading.Thread(target=discover_after_review_starts)
            review_thread.start()
            self.assertTrue(entered.wait(5))
            discovery_thread.start()
            self.assertTrue(discovery_started.wait(5))
            time.sleep(0.05)
            release.set()
            review_thread.join(5)
            discovery_thread.join(5)
            self.assertFalse(review_thread.is_alive())
            self.assertFalse(discovery_thread.is_alive())
            self.assertEqual(len(review_results), 1)
            self.assertTrue(review_results[0]["changed"])
            self.assertEqual(len(discovery_results), 1)
            self.assertIsInstance(discovery_results[0], dict)
            final = owner.get_candidate(server.server_id, original["name"])
            self.assertEqual(final["review_state"], REVIEW_REQUIRED)
            self.assertIsNone(owner.get_approval_baseline(server.server_id, original["name"]))
            self.assertEqual(
                [row["decision"] for row in owner.list_review_audit(server.server_id, original["name"])],
                ["APPROVE"],
            )
        finally:
            connection.close()
            other_connection.close()
            os.unlink(path)
    def test_rejected_and_stale_discovery_do_not_change_source_revision(self):
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        revised = self.server_registry.rename(self.server.server_id, "Calendar stale")
        stale = self.result(
            [tool()],
            registry_revision=before["current_source_registry_revision"],
            diagnostics={"registry_changed_during_attempt": True},
        )
        self.assertRejectedWithoutWrites(stale, "STALE_DISCOVERY")
        after = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(after["current_source_registry_revision"], before["current_source_registry_revision"])
        self.assertEqual(after["current_fingerprint"], before["current_fingerprint"])
        self.assertEqual(revised.revision, before["current_source_registry_revision"] + 1)

    def test_atomic_failure_cannot_tear_fingerprint_from_source_revision(self):
        original = tool()
        self.owner.ingest(self.result([original]))
        before = self.owner.get_candidate(self.server.server_id, original["name"])
        self.connection.executescript(
            """
            CREATE TRIGGER fail_candidate_update
            BEFORE UPDATE ON external_tool_candidate_registry
            BEGIN
                SELECT RAISE(ABORT, 'forced candidate update failure');
            END;
            """
        )
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.owner.ingest(self.result([tool(description="drift")]))
        finally:
            self.connection.execute("DROP TRIGGER fail_candidate_update")
            self.connection.commit()
        after = self.owner.get_candidate(self.server.server_id, original["name"])
        self.assertEqual(after["current_fingerprint"], before["current_fingerprint"])
        self.assertEqual(
            after["current_source_registry_revision"],
            before["current_source_registry_revision"],
        )
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, original["name"])), 1)

    def test_legacy_schema_migrates_to_null_and_fresh_ingest_repairs_it(self):
        connection = sqlite3.connect(":memory:")
        try:
            server_registry = ExternalServerRegistry(
                connection, id_factory=lambda: "legacy-server"
            )
            server = server_registry.register(
                display_name="Legacy",
                endpoint="https://legacy.example/mcp",
                provenance="test",
            )
            legacy_tool = tool("legacy.tool")
            legacy_json = canonical_json(legacy_tool)
            legacy_fingerprint = fingerprint_raw_tool(legacy_tool)
            connection.executescript(
                """
                CREATE TABLE external_tool_candidate_registry (
                    server_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    control_id TEXT NOT NULL UNIQUE,
                    presence_state TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    current_fingerprint TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    model_visible INTEGER NOT NULL DEFAULT 0,
                    execution_allowed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (server_id, tool_name)
                );
                CREATE TABLE external_tool_raw_snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    raw_snapshot_json TEXT NOT NULL,
                    source_registry_revision INTEGER NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    UNIQUE (server_id, tool_name, fingerprint)
                );
                """
            )
            connection.execute(
                "INSERT INTO external_tool_candidate_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    server.server_id, legacy_tool["name"],
                    f"ext:{server.server_id}:{legacy_tool['name']}", PRESENT,
                    APPROVED, legacy_fingerprint, "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 7, 0, 0,
                ),
            )
            connection.execute(
                "INSERT INTO external_tool_raw_snapshots VALUES (NULL, ?, ?, ?, ?, ?, ?)",
                (
                    server.server_id, legacy_tool["name"], legacy_fingerprint,
                    legacy_json, 99, "2026-01-01T00:00:00Z",
                ),
            )
            connection.commit()
            owner = ExternalToolCandidateRegistry(connection, server_registry=server_registry)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(external_tool_candidate_registry)"
                ).fetchall()
            }
            self.assertIn("current_source_registry_revision", columns)
            legacy = owner.get_candidate(server.server_id, legacy_tool["name"])
            self.assertEqual(legacy["current_source_registry_revision"], None)
            self.assertEqual(legacy["current_fingerprint"], legacy_fingerprint)
            self.assertEqual(legacy["review_state"], REVIEW_REQUIRED)
            self.assertEqual(legacy["revision"], 7)
            self.assertEqual(owner.list_snapshots(server.server_id, legacy_tool["name"])[0]["source_registry_revision"], 99)

            owner_again = ExternalToolCandidateRegistry(connection, server_registry=server_registry)
            self.assertIsNone(
                owner_again.get_candidate(server.server_id, legacy_tool["name"])[
                    "current_source_registry_revision"
                ]
            )
            fresh = {
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [legacy_tool],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            }
            owner_again.ingest(fresh)
            repaired = owner_again.get_candidate(server.server_id, legacy_tool["name"])
            self.assertEqual(repaired["current_source_registry_revision"], server.revision)
            self.assertEqual(repaired["revision"], 8)
            snapshots = owner_again.list_snapshots(server.server_id, legacy_tool["name"])
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["source_registry_revision"], 99)
        finally:
            connection.close()

    def test_concurrent_complete_ingests_are_serial_and_never_hybrid(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as handle:
            path = handle.name
        try:
            first_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            first_servers = ExternalServerRegistry(first_connection)
            first_server = first_servers.register(
                display_name="Concurrent", endpoint="https://concurrent.example/mcp", provenance="test"
            )
            first_owner = ExternalToolCandidateRegistry(first_connection, server_registry=first_servers)
            second_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            second_servers = ExternalServerRegistry(second_connection)
            second_owner = ExternalToolCandidateRegistry(second_connection, server_registry=second_servers)
            catalog_a = [tool("a"), tool("a-only")]
            catalog_b = [tool("b"), tool("b-only")]
            barrier = threading.Barrier(2)
            errors = []

            def run(owner, catalog):
                try:
                    barrier.wait()
                    owner.ingest({
                        "status": "SUCCESS", "catalog_complete": True, "server_id": first_server.server_id,
                        "registry_revision": 1, "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": catalog,
                        "diagnostics": {"registry_changed_during_attempt": False},
                        "model_visible": False, "execution_allowed": False,
                    })
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(first_owner, catalog_a)),
                threading.Thread(target=run, args=(second_owner, catalog_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            final_names = {
                row["tool_name"]
                for row in first_owner.list_candidates(first_server.server_id)
                if row["presence_state"] == PRESENT
            }
            self.assertIn(final_names, ({"a", "a-only"}, {"b", "b-only"}))
            for row in first_owner.list_candidates(first_server.server_id):
                self.assertEqual(row["current_source_registry_revision"], 1)
        finally:
            first_connection.close()
            second_connection.close()
            import os
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
