from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import threading
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

    def test_only_complete_current_success_is_accepted(self):
        self.assertEqual(self.owner.ingest(self.result([tool()]))["present_tool_count"], 1)
        for overrides, code in (
            ({"status": "BRIDGE_ERROR"}, "INCOMPLETE_DISCOVERY"),
            ({"catalog_complete": False}, "INCOMPLETE_DISCOVERY"),
            ({"tool_record_boundary": "NORMALIZED"}, "UNSUPPORTED_RECORD_BOUNDARY"),
            ({"diagnostics": {"registry_changed_during_attempt": True}}, "STALE_DISCOVERY"),
            ({"diagnostics": {}}, "STALE_DISCOVERY"),
        ):
            self.assertRejectedWithoutWrites(self.result([tool("other")], **overrides), code)

    def test_server_gate_is_atomic_and_fail_closed(self):
        self.assertRejectedWithoutWrites(
            self.result([], registry_revision=self.server.revision + 1), "REVISION_MISMATCH"
        )
        self.assertRejectedWithoutWrites(
            self.result([], server_id="unknown-server"), "UNKNOWN_SERVER"
        )
        self.server_registry.revoke(self.server.server_id)
        self.assertRejectedWithoutWrites(self.result([]), "REVOKED_SERVER")

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

        self.connection.execute(
            "UPDATE external_tool_candidate_registry SET review_state='APPROVED' "
            "WHERE server_id=? AND tool_name=?", (self.server.server_id, first["name"])
        )
        self.connection.commit()
        self.owner.ingest(self.result([first]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["review_state"], APPROVED
        )
        drifted = tool(description="changed")
        self.owner.ingest(self.result([drifted]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["review_state"], REVIEW_REQUIRED
        )
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, first["name"])), 2)
        self.owner.ingest(self.result([]))
        missing = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(missing["presence_state"], MISSING)
        self.assertEqual(missing["review_state"], REVIEW_REQUIRED)
        self.owner.ingest(self.result([drifted]))
        reappeared = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(reappeared["presence_state"], PRESENT)
        self.assertEqual(reappeared["review_state"], REVIEW_REQUIRED)
        self.owner.ingest(self.result([tool("calendar.renamed")]))
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, first["name"])["presence_state"], MISSING
        )
        self.assertEqual(
            self.owner.get_candidate(self.server.server_id, "calendar.renamed")["review_state"], REVIEW_REQUIRED
        )

    def test_zero_tools_marks_every_previous_tool_missing_and_deduplicates_snapshot(self):
        self.owner.ingest(self.result([tool("a"), tool("b")]))
        self.owner.ingest(self.result([tool("a"), tool("b")]))
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, "a")), 1)
        self.owner.ingest(self.result([]))
        self.assertEqual(
            [row["presence_state"] for row in self.owner.list_candidates(self.server.server_id)],
            [MISSING, MISSING],
        )

    def test_rejected_result_cannot_supply_identity_or_approval(self):
        for field, value in (("control_id", "caller"), ("review_state", APPROVED), ("approved", True)):
            self.assertRejectedWithoutWrites(self.result([], **{field: value}), "UNSUPPORTED_DISCOVERY_FIELD")
        self.assertRejectedWithoutWrites(self.result([], model_visible=True), "UNSAFE_DISCOVERY_RESULT")

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
        finally:
            first_connection.close()
            second_connection.close()
            import os
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
