from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tools.external_server_registry import ExternalServerRegistry, REVOKED_STATE
from tools.external_tool_registry import (
    ExternalToolCandidateRegistry,
    MAX_CATALOG_BYTES,
    MISSING,
    PRESENT,
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

    def test_catalog_count_and_byte_limits_leave_no_partial_rows(self):
        first, second = tool("first"), tool("second")
        first_bytes = len(canonical_json(first).encode("utf-8"))
        total_bytes = first_bytes + len(canonical_json(second).encode("utf-8"))
        self.assertGreater(MAX_CATALOG_BYTES, total_bytes)
        for limit, value, catalog, code in (
            ("MAX_CATALOG_TOOLS", 1, [first, second], "CATALOG_TOO_LARGE"),
            ("MAX_TOOL_SNAPSHOT_BYTES", first_bytes - 1, [first], "SNAPSHOT_TOO_LARGE"),
            ("MAX_CATALOG_BYTES", total_bytes - 1, [first, second], "CATALOG_TOO_LARGE"),
        ):
            with self.subTest(limit=limit), patch("tools.external_tool_registry." + limit, value):
                with self.assertRaises(ToolCatalogValidationError) as raised:
                    self.owner.ingest(self.result(catalog))
                self.assertEqual(raised.exception.code, code)
                for table in ("external_tool_candidate_registry", "external_tool_raw_snapshots"):
                    self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

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

    def test_connected_and_disconnected_ingest_preserves_server_authority(self):
        self.server_registry.mark_connected(self.server.server_id, self.server.revision)
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
        self.assertEqual(row_after[2], "DISCONNECTED")
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

        renamed = self.server_registry.rename(self.server.server_id, "Calendar v2")
        self.owner.ingest(self.result([first], registry_revision=renamed.revision))
        current = self.owner.get_candidate(self.server.server_id, first["name"])
        snapshots = self.owner.list_snapshots(self.server.server_id, first["name"])
        self.assertEqual(current["current_source_registry_revision"], renamed.revision)
        self.assertEqual(current["current_fingerprint"], first_candidate["current_fingerprint"])
        self.assertEqual(current["revision"], 2)
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

    def test_new_unchanged_drift_missing_reappear_and_rename(self):
        first = tool()
        self.owner.ingest(self.result([first]))
        candidate = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(candidate["presence_state"], PRESENT)
        self.assertEqual(candidate["current_source_registry_revision"], self.server.revision)

        self.owner.ingest(self.result([first]))
        unchanged = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(unchanged["current_fingerprint"], candidate["current_fingerprint"])
        self.assertEqual(unchanged["control_id"], candidate["control_id"])
        self.assertEqual(unchanged["current_source_registry_revision"], self.server.revision)

        drifted = tool(description="changed")
        self.owner.ingest(self.result([drifted]))
        changed = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(changed["current_fingerprint"], fingerprint_raw_tool(drifted))
        self.assertEqual(changed["current_source_registry_revision"], self.server.revision)
        self.assertEqual(len(self.owner.list_snapshots(self.server.server_id, first["name"])), 2)
        self.owner.ingest(self.result([]))
        missing = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(missing["presence_state"], MISSING)
        self.assertEqual(missing["current_source_registry_revision"], self.server.revision)
        self.owner.ingest(self.result([drifted]))
        reappeared = self.owner.get_candidate(self.server.server_id, first["name"])
        self.assertEqual(reappeared["presence_state"], PRESENT)
        self.assertEqual(reappeared["control_id"], candidate["control_id"])
        self.assertEqual(reappeared["current_source_registry_revision"], self.server.revision)
        self.owner.ingest(self.result([tool("calendar.renamed")]))
        self.assertEqual(self.owner.get_candidate(self.server.server_id, first["name"])["presence_state"], MISSING)
        self.assertEqual(self.owner.get_candidate(self.server.server_id, first["name"])["current_source_registry_revision"], self.server.revision)
        renamed = self.owner.get_candidate(self.server.server_id, "calendar.renamed")
        self.assertEqual(renamed["presence_state"], PRESENT)
        self.assertEqual(renamed["current_source_registry_revision"], self.server.revision)
        self.assertNotEqual(renamed["control_id"], candidate["control_id"])

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

    def test_rejected_result_cannot_supply_identity(self):
        self.assertRejectedWithoutWrites(
            self.result([], control_id="caller"), "UNSUPPORTED_DISCOVERY_FIELD"
        )

    def test_candidate_schema_is_idempotent_without_mutating_rows(self):
        tables = {
            row[0] for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ) if not row[0].startswith("sqlite_")
        }
        self.assertEqual(tables, {
            "external_server_registry", "external_tool_candidate_registry",
            "external_tool_raw_snapshots",
        })
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        snapshots = self.owner.list_snapshots(self.server.server_id, "calendar.list")
        self.owner.initialize()
        after = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(after, before)
        self.assertEqual(self.owner.list_snapshots(self.server.server_id, "calendar.list"), snapshots)

    def test_source_revision_requires_positive_integer_and_matches_server(self):
        self.assertIsNone(self.owner.get_candidate(self.server.server_id, "missing.tool"))
        self.owner.ingest(self.result([tool()]))
        for revision in (None, True, False, 0, -1, "1", 1.5):
            with self.subTest(revision=revision):
                self.assertRejectedWithoutWrites(
                    self.result([tool()], registry_revision=revision),
                    "INVALID_REGISTRY_REVISION",
                )
        self.assertRejectedWithoutWrites(
            self.result([tool()], registry_revision=self.server.revision + 1),
            "REVISION_MISMATCH",
        )

    def test_unknown_and_revoked_servers_fail_closed(self):
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertRejectedWithoutWrites(self.result([tool()], server_id="unknown-server"), "UNKNOWN_SERVER")
        self.connection.execute(
            "DELETE FROM external_server_registry WHERE server_id=?", (self.server.server_id,)
        )
        self.connection.commit()
        self.assertRejectedWithoutWrites(self.result([tool()]), "UNKNOWN_SERVER")
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list"), before)
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
        self.assertRejectedWithoutWrites(self.result([tool()]), "REVOKED_SERVER")

    def test_raw_snapshot_missing_or_tampered_rejects_atomically(self):
        self.owner.ingest(self.result([tool()]))
        candidate_before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        snapshot_before = self.owner.list_snapshots(self.server.server_id, "calendar.list")[0]
        self.connection.execute(
            "DELETE FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=?",
            (self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        self.assertRejectedWithoutWrites(self.result([tool()]), "SNAPSHOT_MISSING")
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list"), candidate_before)
        self.assertEqual(self.owner.list_snapshots(self.server.server_id, "calendar.list"), ())
        self.connection.execute(
            "INSERT INTO external_tool_raw_snapshots (server_id,tool_name,fingerprint,raw_snapshot_json,source_registry_revision,first_observed_at) "
            "VALUES (?,?,?,?,?,?)",
            (self.server.server_id, "calendar.list", snapshot_before["fingerprint"], "{}",
             snapshot_before["source_registry_revision"], snapshot_before["first_observed_at"]),
        )
        self.connection.commit()
        self.assertRejectedWithoutWrites(self.result([tool()]), "SNAPSHOT_MISMATCH")
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list"), candidate_before)
        self.assertEqual(self.owner.list_snapshots(self.server.server_id, "calendar.list")[0]["raw_snapshot_json"], "{}")

        with self.subTest(history="tampered older fingerprint must not become current"):
            self.connection.execute(
                "UPDATE external_tool_raw_snapshots SET raw_snapshot_json=? WHERE server_id=? AND fingerprint=?",
                (snapshot_before["raw_snapshot_json"], self.server.server_id, snapshot_before["fingerprint"]),
            )
            self.connection.commit()
            self.owner.ingest(self.result([tool(description="version B")]))
            self.connection.execute(
                "UPDATE external_tool_raw_snapshots SET raw_snapshot_json='{}' WHERE server_id=? AND fingerprint=?",
                (self.server.server_id, snapshot_before["fingerprint"]),
            )
            self.connection.commit()
            before = self.owner.get_candidate(self.server.server_id, "calendar.list")
            snapshots = self.owner.list_snapshots(self.server.server_id, "calendar.list")
            self.assertRejectedWithoutWrites(self.result([tool()]), "SNAPSHOT_MISMATCH")
            self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list"), before)
            self.assertEqual(self.owner.list_snapshots(self.server.server_id, "calendar.list"), snapshots)

    def test_disconnected_server_accepts_fresh_catalog_without_changing_registry(self):
        self.owner.ingest(self.result([tool()]))
        changed = self.server_registry.update_connection(
            self.server.server_id, endpoint="https://calendar-v2.example/mcp"
        )
        before = self.server_registry.get(self.server.server_id)
        self.owner.ingest(self.result([tool()], registry_revision=changed.revision))
        self.assertEqual(before.lifecycle_state, "DISCONNECTED")
        self.assertEqual(self.server_registry.get(self.server.server_id), before)
        self.assertEqual(self.owner.get_candidate(self.server.server_id, "calendar.list")["presence_state"], PRESENT)

    def test_missing_candidate_retains_identity_without_current_presence(self):
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.owner.ingest(self.result([]))
        candidate = self.owner.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(candidate["presence_state"], MISSING)
        self.assertEqual(candidate["control_id"], before["control_id"])
        self.assertEqual(candidate["current_fingerprint"], before["current_fingerprint"])

    def test_candidate_and_snapshot_reads_do_not_write_after_authority_drift(self):
        self.owner.ingest(self.result([tool()]))
        before = self.owner.get_candidate(self.server.server_id, "calendar.list")
        snapshots_before = self.owner.list_snapshots(self.server.server_id, "calendar.list")
        self.server_registry.rename(self.server.server_id, "Calendar changed")
        total_changes = self.connection.total_changes
        after = self.owner.get_candidate(self.server.server_id, "calendar.list")
        snapshots_after = self.owner.list_snapshots(self.server.server_id, "calendar.list")
        self.assertEqual(after, before)
        self.assertEqual(snapshots_after, snapshots_before)
        self.assertEqual(self.connection.total_changes, total_changes)

    def test_registry_update_and_discovery_cross_races_are_coherent(self):
        for first_writer in ("registry", "discovery"):
            with self.subTest(first_writer=first_writer), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "cross-race.sqlite3")
                connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
                other_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
                try:
                    servers = ExternalServerRegistry(connection)
                    server = servers.register(
                        display_name="Cross race", endpoint="https://cross-race.example/mcp",
                        provenance="test",
                    )
                    owner = ExternalToolCandidateRegistry(connection, server_registry=servers)
                    other_servers = ExternalServerRegistry(other_connection)
                    other_owner = ExternalToolCandidateRegistry(other_connection, server_registry=other_servers)
                    def result(catalog):
                        return {
                            "status": "SUCCESS", "catalog_complete": True,
                            "server_id": server.server_id, "registry_revision": server.revision,
                            "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": catalog,
                            "diagnostics": {"registry_changed_during_attempt": False},
                        }
                    original = tool()
                    owner.ingest(result([original]))
                    before = owner.get_candidate(server.server_id, original["name"])
                    errors = []
                    entered = threading.Event()
                    release = threading.Event()
                    second_started = threading.Event()

                    if first_writer == "registry":
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "UPDATE external_server_registry SET endpoint=?,revision=revision+1 WHERE server_id=?",
                            ("https://cross-race-new.example/mcp", server.server_id),
                        )
                        def ingest_old_revision():
                            second_started.set()
                            try:
                                other_owner.ingest(result([tool(description="new")]))
                            except Exception as exc:
                                errors.append(exc)
                        worker = threading.Thread(target=ingest_old_revision)
                        worker.start()
                        self.assertTrue(second_started.wait(5))
                        connection.commit()
                        worker.join(5)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(errors), 1)
                        self.assertEqual(errors[0].code, "REVISION_MISMATCH")
                        self.assertEqual(owner.get_candidate(server.server_id, original["name"]), before)
                        self.assertEqual(len(owner.list_snapshots(server.server_id, original["name"])), 1)
                    else:
                        def pause_discovery():
                            entered.set()
                            if not release.wait(5):
                                raise AssertionError("discovery transaction release timeout")
                            return 0
                        connection.create_function("pause_discovery", 0, pause_discovery)
                        connection.executescript(
                            "CREATE TRIGGER pause_cross_race BEFORE UPDATE OF current_fingerprint "
                            "ON external_tool_candidate_registry BEGIN SELECT pause_discovery(); END;"
                        )
                        def ingest_new_version():
                            try:
                                owner.ingest(result([tool(description="new")]))
                            except Exception as exc:
                                errors.append(exc)
                        def update_registry():
                            second_started.set()
                            try:
                                other_servers.update_connection(
                                    server.server_id, endpoint="https://cross-race-new.example/mcp"
                                )
                            except Exception as exc:
                                errors.append(exc)
                        discovery_thread = threading.Thread(target=ingest_new_version)
                        registry_thread = threading.Thread(target=update_registry)
                        discovery_thread.start()
                        self.assertTrue(entered.wait(5))
                        registry_thread.start()
                        self.assertTrue(second_started.wait(5))
                        release.set()
                        discovery_thread.join(5)
                        registry_thread.join(5)
                        self.assertFalse(discovery_thread.is_alive())
                        self.assertFalse(registry_thread.is_alive())
                        self.assertEqual(errors, [])
                        final = owner.get_candidate(server.server_id, original["name"])
                        self.assertEqual(final["current_fingerprint"], fingerprint_raw_tool(tool(description="new")))
                        self.assertEqual(final["current_source_registry_revision"], server.revision)
                        self.assertEqual(servers.get(server.server_id).revision, server.revision + 1)
                        self.assertEqual(len(owner.list_snapshots(server.server_id, original["name"])), 2)
                finally:
                    connection.close()
                    other_connection.close()

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
                    "APPROVED", legacy_fingerprint, "2026-01-01T00:00:00Z",
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
