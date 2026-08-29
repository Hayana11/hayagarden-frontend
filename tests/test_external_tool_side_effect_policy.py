from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest

from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_registry import (
    APPROVED,
    ExternalToolCandidateRegistry,
    MISSING,
    PRESENT,
    REVIEW_REQUIRED,
    canonical_json,
    fingerprint_raw_tool,
)
from tools.external_tool_side_effect_policy import (
    AUTONOMOUS,
    CODE_OR_PROCESS,
    EXTERNAL_STATE,
    NONE,
    OWNER_CONFIRMED,
    UNKNOWN,
    ExternalToolSideEffectPolicy,
    SideEffectRejectedError,
    SideEffectValidationError,
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


class ExternalToolSideEffectPolicyTests(unittest.TestCase):
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

    def tearDown(self):
        self.connection.close()

    def result(self, tools, **overrides):
        result = {
            "status": "SUCCESS",
            "catalog_complete": True,
            "server_id": self.server.server_id,
            "registry_revision": self.server.revision,
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "tools": tools,
            "diagnostics": {"registry_changed_during_attempt": False},
            "model_visible": False,
            "execution_allowed": False,
        }
        result.update(overrides)
        return result

    def ingest_and_approve(self, record=None):
        record = record or tool()
        self.candidates.ingest(self.result([record]))
        candidate = self.candidates.get_candidate(self.server.server_id, record["name"])
        return self.candidates.approve_candidate(
            server_id=self.server.server_id,
            tool_name=record["name"],
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            actor="owner",
            provenance="settings-admin",
        )

    def classify(self, side_effect_class=NONE, *, actor="owner", provenance="settings-admin", execution_mode=AUTONOMOUS):
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        return self.policy.classify(
            server_id=self.server.server_id,
            tool_name="calendar.list",
            expected_fingerprint=candidate["current_fingerprint"],
            expected_source_registry_revision=candidate["current_source_registry_revision"],
            side_effect_class=side_effect_class,
            actor=actor,
            provenance=provenance,
            execution_mode=execution_mode,
        )

    def assert_rejected(self, call, code):
        with self.assertRaises(SideEffectRejectedError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)

    def test_schema_and_initialize_are_idempotent(self):
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("external_tool_side_effect_baselines", tables)
        self.assertIn("external_tool_side_effect_audit", tables)
        self.candidates.ingest(self.result([tool()]))
        before = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.policy.initialize()
        after = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(before, after)
        self.assertEqual(self.policy.list_audit(self.server.server_id, "calendar.list"), ())

    def test_no_record_is_unknown_and_annotations_do_not_classify(self):
        effective = self.policy.get_effective_classification(
            self.server.server_id, "calendar.list"
        )
        self.assertFalse(effective["effective_classified"])
        self.assertEqual(effective["effective_side_effect_class"], UNKNOWN)
        self.candidates.ingest(self.result([tool(annotations={"readOnlyHint": True, "destructiveHint": False})]))
        self.assertEqual(
            self.candidates.get_candidate(self.server.server_id, "calendar.list")["review_state"],
            REVIEW_REQUIRED,
        )
        self.assertEqual(self.policy.get_baseline(self.server.server_id, "calendar.list"), None)

    def test_all_four_classes_are_explicitly_accepted_and_invalid_is_rejected(self):
        self.ingest_and_approve()
        for side_effect_class in (NONE, EXTERNAL_STATE, CODE_OR_PROCESS, UNKNOWN):
            result = self.classify(side_effect_class)
            self.assertTrue(result["changed"])
            self.assertEqual(result["side_effect_class"], side_effect_class)
        with self.assertRaises(SideEffectValidationError) as raised:
            self.classify("readOnlyHint")
        self.assertEqual(raised.exception.code, "INVALID_SIDE_EFFECT_CLASS")

    def test_allowed_classes_default_to_autonomous_and_owner_confirmed_is_explicit(self):
        self.ingest_and_approve()
        autonomous = self.classify(NONE)
        self.assertEqual(autonomous["baseline"]["execution_mode"], AUTONOMOUS)
        effective = self.policy.get_effective_classification(
            self.server.server_id, "calendar.list"
        )
        self.assertTrue(effective["effective_classified"])
        self.assertEqual(effective["effective_execution_mode"], AUTONOMOUS)

        confirmed = self.classify(NONE, execution_mode=OWNER_CONFIRMED)
        self.assertEqual(confirmed["baseline"]["execution_mode"], OWNER_CONFIRMED)
        self.assertEqual(
            self.policy.get_effective_classification(
                self.server.server_id, "calendar.list"
            )["effective_execution_mode"],
            OWNER_CONFIRMED,
        )

        with self.assertRaises(SideEffectValidationError) as raised:
            self.classify(NONE, execution_mode="model_decides")
        self.assertEqual(raised.exception.code, "INVALID_EXECUTION_MODE")


    def test_classification_requires_current_effective_approval_and_presence(self):
        self.candidates.ingest(self.result([tool()]))
        self.assert_rejected(lambda: self.classify(), "APPROVAL_NOT_EFFECTIVE")
        self.candidates.ingest(self.result([]))
        self.assert_rejected(lambda: self.classify(), "CANDIDATE_NOT_PRESENT")
        self.assert_rejected(
            lambda: self.policy.classify(
                server_id="unknown", tool_name="calendar.list",
                expected_fingerprint="0" * 64,
                expected_source_registry_revision=self.server.revision,
                side_effect_class=NONE, actor="owner", provenance="settings-admin",
            ),
            "UNKNOWN_CANDIDATE",
        )

    def test_binding_gates_reject_fingerprint_revision_and_freshness_mismatch(self):
        self.ingest_and_approve()
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        values = {
            "server_id": self.server.server_id,
            "tool_name": "calendar.list",
            "expected_fingerprint": candidate["current_fingerprint"],
            "expected_source_registry_revision": candidate["current_source_registry_revision"],
            "side_effect_class": NONE,
            "actor": "owner",
            "provenance": "settings-admin",
        }
        values["expected_fingerprint"] = "0" * 64
        self.assert_rejected(lambda: self.policy.classify(**values), "FINGERPRINT_MISMATCH")
        values["expected_fingerprint"] = candidate["current_fingerprint"]
        values["expected_source_registry_revision"] += 1
        self.assert_rejected(lambda: self.policy.classify(**values), "SOURCE_REVISION_MISMATCH")
        self.connection.execute(
            "UPDATE external_tool_candidate_registry SET current_source_registry_revision=NULL "
            "WHERE server_id=? AND tool_name=?", (self.server.server_id, "calendar.list")
        )
        self.connection.commit()
        values["expected_source_registry_revision"] = self.server.revision
        self.assert_rejected(lambda: self.policy.classify(**values), "FRESHNESS_UNKNOWN")

    def test_server_authority_and_snapshot_integrity_are_rechecked(self):
        self.ingest_and_approve()
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        before_server = self.server_registry.get(self.server.server_id)
        self.connection.execute(
            "DELETE FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=?",
            (self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        self.assert_rejected(lambda: self.classify(), "SNAPSHOT_MISSING")
        self.candidates.ingest(self.result([tool()]))
        self.connection.execute(
            "UPDATE external_tool_raw_snapshots SET raw_snapshot_json='{}' "
            "WHERE server_id=? AND tool_name=?", (self.server.server_id, "calendar.list")
        )
        self.connection.commit()
        self.assert_rejected(lambda: self.classify(), "SNAPSHOT_MISMATCH")
        self.assertEqual(self.server_registry.get(self.server.server_id), before_server)
        self.connection.execute(
            "UPDATE external_server_registry SET lifecycle_state='REVOKED' WHERE server_id=?",
            (self.server.server_id,),
        )
        self.connection.commit()
        self.assert_rejected(lambda: self.classify(), "REVOKED_SERVER")

    def test_actor_and_provenance_are_required_exact_metadata(self):
        self.ingest_and_approve()
        for actor, provenance, code in (
            ("", "settings-admin", "INVALID_ACTOR"),
            (" owner", "settings-admin", "INVALID_ACTOR"),
            ("owner", " settings-admin", "INVALID_PROVENANCE"),
            ("anonymous", "settings-admin", "INVALID_ACTOR"),
            ("owner\n", "settings-admin", "INVALID_ACTOR"),
        ):
            with self.assertRaises(SideEffectValidationError) as raised:
                self.classify(actor=actor, provenance=provenance)
            self.assertEqual(raised.exception.code, code)

    def test_baseline_audit_fields_and_append_only_history(self):
        self.ingest_and_approve()
        result = self.classify(EXTERNAL_STATE)
        baseline = self.policy.get_baseline(self.server.server_id, "calendar.list")
        audit = self.policy.list_audit(self.server.server_id, "calendar.list")
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(result["baseline"], baseline)
        self.assertEqual(baseline["classified_fingerprint"], candidate["current_fingerprint"])
        self.assertEqual(
            baseline["classified_source_registry_revision"],
            candidate["current_source_registry_revision"],
        )
        self.assertEqual(baseline["classified_actor"], "owner")
        self.assertEqual(baseline["classified_provenance"], "settings-admin")
        self.assertEqual(audit[0]["side_effect_class"], EXTERNAL_STATE)
        self.assertEqual(audit[0]["actor"], "owner")
        event_id = audit[0]["classification_event_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE external_tool_side_effect_audit SET actor='tampered' "
                "WHERE classification_event_id=?", (event_id,)
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM external_tool_side_effect_audit WHERE classification_event_id=?",
                (event_id,),
            )
        self.connection.rollback()

    def test_replay_is_idempotent_but_changed_metadata_or_class_is_a_new_decision(self):
        self.ingest_and_approve()
        first = self.classify(NONE)
        before = self.policy.get_baseline(self.server.server_id, "calendar.list")
        replay = self.classify(NONE)
        self.assertTrue(first["changed"])
        self.assertFalse(replay["changed"])
        self.assertEqual(len(self.policy.list_audit(self.server.server_id, "calendar.list")), 1)
        self.assertEqual(self.policy.get_baseline(self.server.server_id, "calendar.list"), before)
        self.assertTrue(self.classify(EXTERNAL_STATE)["changed"])
        self.assertTrue(self.classify(EXTERNAL_STATE, actor="second-owner")["changed"])
        self.assertEqual(len(self.policy.list_audit(self.server.server_id, "calendar.list")), 3)
        self.assertEqual(
            [row["side_effect_class"] for row in self.policy.list_audit(self.server.server_id, "calendar.list")],
            [NONE, EXTERNAL_STATE, EXTERNAL_STATE],
        )

    def test_effective_classification_requires_approval_current_version_and_all_matches(self):
        self.assertEqual(
            self.policy.get_effective_classification(self.server.server_id, "calendar.list")[
                "effective_side_effect_class"
            ],
            UNKNOWN,
        )
        self.ingest_and_approve()
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])
        self.classify(CODE_OR_PROCESS)
        effective = self.policy.get_effective_classification(self.server.server_id, "calendar.list")
        self.assertTrue(effective["effective_classified"])
        self.assertEqual(effective["effective_side_effect_class"], CODE_OR_PROCESS)
        self.connection.execute(
            "UPDATE external_tool_side_effect_baselines SET classified_fingerprint=? "
            "WHERE server_id=? AND tool_name=?",
            ("0" * 64, self.server.server_id, "calendar.list"),
        )
        self.connection.commit()
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])
        self.assertEqual(
            self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_side_effect_class"],
            UNKNOWN,
        )

    def test_fingerprint_server_revision_missing_and_approval_drift_invalidate(self):
        current = tool()
        self.ingest_and_approve(current)
        self.classify(NONE)
        self.candidates.ingest(self.result([tool(description="changed")]))
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])
        self.assertEqual(
            self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_side_effect_class"],
            UNKNOWN,
        )

        self.candidates.ingest(self.result([]))
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])
        self.candidates.ingest(self.result([tool(description="changed")]))
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])

        self.candidates.approve_candidate(
            server_id=self.server.server_id, tool_name="calendar.list",
            expected_fingerprint=self.candidates.get_candidate(self.server.server_id, "calendar.list")["current_fingerprint"],
            expected_source_registry_revision=self.server.revision,
            actor="owner", provenance="settings-admin",
        )
        self.classify(NONE)
        changed_server = self.server_registry.rename(self.server.server_id, "Calendar v2")
        self.candidates.ingest(self.result([tool(description="changed")], registry_revision=changed_server.revision))
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])

        self.candidates.approve_candidate(
            server_id=self.server.server_id, tool_name="calendar.list",
            expected_fingerprint=self.candidates.get_candidate(self.server.server_id, "calendar.list")["current_fingerprint"],
            expected_source_registry_revision=changed_server.revision,
            actor="owner", provenance="settings-admin",
        )
        self.classify(NONE)
        self.candidates.reject_candidate(
            server_id=self.server.server_id, tool_name="calendar.list",
            expected_fingerprint=self.candidates.get_candidate(self.server.server_id, "calendar.list")["current_fingerprint"],
            expected_source_registry_revision=changed_server.revision,
            actor="owner", provenance="settings-admin",
        )
        self.assertFalse(self.policy.get_effective_classification(self.server.server_id, "calendar.list")["effective_classified"])

    def test_owner_does_not_change_candidate_or_server_state(self):
        self.ingest_and_approve()
        candidate_before = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        server_before = self.server_registry.get(self.server.server_id)
        self.classify(UNKNOWN)
        candidate_after = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(candidate_after, candidate_before)
        self.assertEqual(self.server_registry.get(self.server.server_id), server_before)
        self.assertEqual(candidate_after["model_visible"], 0)
        self.assertEqual(candidate_after["execution_allowed"], 0)

    def test_shared_authoritative_connection_is_required(self):
        other = sqlite3.connect(":memory:")
        try:
            other_servers = ExternalServerRegistry(other)
            other_candidates = ExternalToolCandidateRegistry(other, server_registry=other_servers)
            with self.assertRaises(SideEffectRejectedError) as raised:
                ExternalToolSideEffectPolicy(
                    self.connection,
                    candidate_registry=other_candidates,
                    server_registry=self.server_registry,
                )
            self.assertEqual(raised.exception.code, "SERVER_AUTHORITY_MISMATCH")
        finally:
            other.close()

    def test_legacy_m5_04_schema_gets_only_classification_tables(self):
        self.candidates.ingest(self.result([tool()]))
        before = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.policy.initialize()
        after = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        self.assertEqual(after, before)
        self.assertIsNone(self.policy.get_baseline(self.server.server_id, "calendar.list"))
        self.assertEqual(self.policy.list_audit(self.server.server_id, "calendar.list"), ())

    def test_classification_and_discovery_cross_races_are_safe(self):
        def make_case():
            handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
            path = handle.name
            handle.close()
            connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            servers = ExternalServerRegistry(connection)
            server = servers.register(
                display_name="Race",
                endpoint="https://race.example/mcp",
                provenance="test",
            )
            candidates = ExternalToolCandidateRegistry(connection, server_registry=servers)
            policy = ExternalToolSideEffectPolicy(
                connection, candidate_registry=candidates, server_registry=servers
            )
            original = tool()
            candidates.ingest({
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [original],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            })
            candidate = candidates.get_candidate(server.server_id, original["name"])
            candidates.approve_candidate(
                server_id=server.server_id, tool_name=original["name"],
                expected_fingerprint=candidate["current_fingerprint"],
                expected_source_registry_revision=candidate["current_source_registry_revision"],
                actor="owner", provenance="settings-admin",
            )
            other_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            other_servers = ExternalServerRegistry(other_connection)
            other_candidates = ExternalToolCandidateRegistry(other_connection, server_registry=other_servers)
            other_policy = ExternalToolSideEffectPolicy(
                other_connection, candidate_registry=other_candidates, server_registry=other_servers
            )
            return path, connection, candidates, policy, other_connection, other_candidates, other_policy, server, original

        def discovery_result(server, record):
            return {
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [record],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            }

        # Discovery first: classification starts while discovery holds the write lock.
        path, connection, candidates, policy, other_connection, other_candidates, other_policy, server, original = make_case()
        entered = threading.Event()
        release = threading.Event()
        calls = [0]

        def pause_discovery():
            if calls[0] == 0:
                calls[0] += 1
                entered.set()
                self.assertTrue(release.wait(5))
            return 0

        try:
            connection.create_function("pause_discovery", 0, pause_discovery)
            connection.executescript(
                """
                CREATE TRIGGER pause_side_effect_discovery
                BEFORE UPDATE OF current_fingerprint ON external_tool_candidate_registry
                BEGIN SELECT pause_discovery(); END;
                """
            )
            candidate = candidates.get_candidate(server.server_id, original["name"])
            errors = []
            started = threading.Event()

            def discover():
                try:
                    candidates.ingest(discovery_result(server, tool(description="new")))
                except Exception as exc:
                    errors.append(exc)

            def classify_old():
                started.set()
                try:
                    other_policy.classify(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate["current_source_registry_revision"],
                        side_effect_class=NONE, actor="owner", provenance="settings-admin",
                    )
                except Exception as exc:
                    errors.append(exc)

            discovery_thread = threading.Thread(target=discover)
            classify_thread = threading.Thread(target=classify_old)
            discovery_thread.start()
            self.assertTrue(entered.wait(5))
            classify_thread.start()
            self.assertTrue(started.wait(5))
            time.sleep(0.05)
            release.set()
            discovery_thread.join(5)
            classify_thread.join(5)
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].code, "FINGERPRINT_MISMATCH")
            self.assertEqual(policy.list_audit(server.server_id, original["name"]), ())
            self.assertIsNone(policy.get_baseline(server.server_id, original["name"]))
        finally:
            connection.close()
            other_connection.close()
            import os
            os.unlink(path)

        # Classification first: discovery starts while classification holds the write lock.
        path, connection, candidates, policy, other_connection, other_candidates, other_policy, server, original = make_case()
        entered = threading.Event()
        release = threading.Event()
        calls = [0]

        def pause_classification():
            if calls[0] == 0:
                calls[0] += 1
                entered.set()
                self.assertTrue(release.wait(5))
            return 0

        try:
            connection.create_function("pause_classification", 0, pause_classification)
            connection.executescript(
                """
                CREATE TRIGGER pause_side_effect_classification
                BEFORE INSERT ON external_tool_side_effect_audit
                BEGIN SELECT pause_classification(); END;
                """
            )
            classification_results = []
            discovery_results = []
            started = threading.Event()

            def classify_current():
                try:
                    classification_results.append(policy.classify(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidates.get_candidate(server.server_id, original["name"])["current_fingerprint"],
                        expected_source_registry_revision=server.revision,
                        side_effect_class=NONE, actor="owner", provenance="settings-admin",
                    ))
                except Exception as exc:
                    classification_results.append(exc)

            def discover_after_classification():
                started.set()
                try:
                    discovery_results.append(candidates2.ingest(discovery_result(server, tool(description="new"))))
                except Exception as exc:
                    discovery_results.append(exc)

            candidates2 = other_candidates
            classify_thread = threading.Thread(target=classify_current)
            discover_thread = threading.Thread(target=discover_after_classification)
            classify_thread.start()
            self.assertTrue(entered.wait(5))
            discover_thread.start()
            self.assertTrue(started.wait(5))
            time.sleep(0.05)
            release.set()
            classify_thread.join(5)
            discover_thread.join(5)
            self.assertEqual(len(classification_results), 1)
            self.assertTrue(classification_results[0]["changed"])
            self.assertEqual(len(discovery_results), 1)
            self.assertIsInstance(discovery_results[0], dict)
            self.assertFalse(policy.get_effective_classification(server.server_id, original["name"])["effective_classified"])
            self.assertEqual(
                policy.get_effective_classification(server.server_id, original["name"])["effective_side_effect_class"],
                UNKNOWN,
            )
        finally:
            connection.close()
            other_connection.close()
            import os
            os.unlink(path)

    def test_classification_and_approval_invalidation_race_cannot_use_old_approval(self):
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        path = handle.name
        handle.close()
        seed = sqlite3.connect(path)
        try:
            servers = ExternalServerRegistry(seed)
            server = servers.register(
                display_name="Approval race",
                endpoint="https://approval-race.example/mcp",
                provenance="test",
            )
            candidates = ExternalToolCandidateRegistry(seed, server_registry=servers)
            policy = ExternalToolSideEffectPolicy(
                seed, candidate_registry=candidates, server_registry=servers
            )
            original = tool()
            candidates.ingest({
                "status": "SUCCESS", "catalog_complete": True,
                "server_id": server.server_id, "registry_revision": server.revision,
                "tool_record_boundary": "SDK_VISIBLE_RAW", "tools": [original],
                "diagnostics": {"registry_changed_during_attempt": False},
                "model_visible": False, "execution_allowed": False,
            })
            candidate = candidates.get_candidate(server.server_id, original["name"])
            candidates.approve_candidate(
                server_id=server.server_id, tool_name=original["name"],
                expected_fingerprint=candidate["current_fingerprint"],
                expected_source_registry_revision=candidate["current_source_registry_revision"],
                actor="owner", provenance="settings-admin",
            )
        finally:
            seed.close()

        connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        other_connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        connection_servers = ExternalServerRegistry(connection)
        connection_candidates = ExternalToolCandidateRegistry(
            connection, server_registry=connection_servers
        )
        connection_policy = ExternalToolSideEffectPolicy(
            connection,
            candidate_registry=connection_candidates,
            server_registry=connection_servers,
        )
        other_servers = ExternalServerRegistry(other_connection)
        other_candidates = ExternalToolCandidateRegistry(
            other_connection, server_registry=other_servers
        )
        entered = threading.Event()
        release = threading.Event()
        calls = [0]

        def pause_classification():
            if calls[0] == 0:
                calls[0] += 1
                entered.set()
                self.assertTrue(release.wait(5))
            return 0

        try:
            connection.create_function("pause_approval_race", 0, pause_classification)
            connection.executescript(
                """
                CREATE TRIGGER pause_approval_race
                BEFORE INSERT ON external_tool_side_effect_audit
                BEGIN SELECT pause_approval_race(); END;
                """
            )
            classification_results = []
            rejection_results = []

            def classify_current():
                try:
                    classification_results.append(connection_policy.classify(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate[
                            "current_source_registry_revision"
                        ],
                        side_effect_class=NONE, actor="owner", provenance="settings-admin",
                    ))
                except Exception as exc:
                    classification_results.append(exc)

            def reject_after_classification():
                try:
                    rejection_results.append(other_candidates.reject_candidate(
                        server_id=server.server_id, tool_name=original["name"],
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate[
                            "current_source_registry_revision"
                        ],
                        actor="owner", provenance="settings-admin",
                    ))
                except Exception as exc:
                    rejection_results.append(exc)

            classify_thread = threading.Thread(target=classify_current)
            reject_thread = threading.Thread(target=reject_after_classification)
            classify_thread.start()
            self.assertTrue(entered.wait(5))
            reject_thread.start()
            time.sleep(0.05)
            release.set()
            classify_thread.join(5)
            reject_thread.join(5)
            self.assertEqual(len(classification_results), 1)
            self.assertIsInstance(classification_results[0], dict)
            self.assertEqual(len(rejection_results), 1)
            self.assertIsInstance(rejection_results[0], dict)
            effective = connection_policy.get_effective_classification(
                server.server_id, original["name"]
            )
            self.assertFalse(effective["effective_classified"])
            self.assertEqual(effective["effective_side_effect_class"], UNKNOWN)
        finally:
            connection.close()
            other_connection.close()
            import os
            os.unlink(path)

    def test_two_concurrent_classifications_leave_complete_baseline_and_history(self):
        self.ingest_and_approve()
        handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        path = handle.name
        handle.close()
        try:
            connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
            self.connection.backup(connection)
            second = sqlite3.connect(path, timeout=5, check_same_thread=False)
            first_servers = ExternalServerRegistry(connection)
            second_servers = ExternalServerRegistry(second)
            first_candidates = ExternalToolCandidateRegistry(connection, server_registry=first_servers)
            second_candidates = ExternalToolCandidateRegistry(second, server_registry=second_servers)
            first_policy = ExternalToolSideEffectPolicy(connection, candidate_registry=first_candidates, server_registry=first_servers)
            second_policy = ExternalToolSideEffectPolicy(second, candidate_registry=second_candidates, server_registry=second_servers)
            candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def run(policy, side_effect_class):
                try:
                    barrier.wait()
                    results.append(policy.classify(
                        server_id=self.server.server_id, tool_name="calendar.list",
                        expected_fingerprint=candidate["current_fingerprint"],
                        expected_source_registry_revision=candidate["current_source_registry_revision"],
                        side_effect_class=side_effect_class, actor="owner", provenance=side_effect_class,
                    ))
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=(first_policy, EXTERNAL_STATE)),
                threading.Thread(target=run, args=(second_policy, CODE_OR_PROCESS)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            baseline = first_policy.get_baseline(self.server.server_id, "calendar.list")
            audit = first_policy.list_audit(self.server.server_id, "calendar.list")
            self.assertIn(baseline["side_effect_class"], {EXTERNAL_STATE, CODE_OR_PROCESS})
            self.assertEqual(len(audit), 2)
            self.assertEqual(baseline["classified_fingerprint"], candidate["current_fingerprint"])
            self.assertEqual(
                baseline["classified_source_registry_revision"],
                candidate["current_source_registry_revision"],
            )
            second.close()
            connection.close()
        finally:
            import os
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
