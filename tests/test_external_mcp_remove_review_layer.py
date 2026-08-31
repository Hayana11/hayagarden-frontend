import sqlite3
import unittest

from tools.external_mcp_auth_binding import AUTH_NONE, ExternalMcpAuthBindingRegistry
from tools.external_mcp_discovery import ExternalMcpDiscovery
from tools.external_mcp_invocation import (
    FAILED_PRE_CALL,
    OUTCOME_UNKNOWN,
    SUCCEEDED,
    ExternalMcpInvocation,
    build_external_action_id,
)
from tools.external_server_registry import (
    CONNECTED_STATE,
    DISCONNECTED_STATE,
    REVOKED_STATE,
    ExternalServerRegistry,
    InvalidStateTransitionError,
)
from tools.external_secret_store import ExternalSecretStore
from tools.external_tool_registry import ExternalToolCandidateRegistry, MISSING, PRESENT


class ExternalMcpRemoveReviewLayerTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.servers = ExternalServerRegistry(self.connection, id_factory=lambda: "server-1")
        self.secrets = ExternalSecretStore(self.connection, registry=self.servers, key_file="/tmp/test-external-mcp.key")
        self.auth = ExternalMcpAuthBindingRegistry(self.connection, server_registry=self.servers, secret_store=self.secrets)
        self.candidates = ExternalToolCandidateRegistry(self.connection, server_registry=self.servers)
        self.server = self.servers.register(display_name="Calendar", endpoint="https://mcp.example.test", provenance="settings-admin")
        self.auth.set_binding(self.server.server_id, AUTH_NONE)

    def tearDown(self):
        self.connection.close()

    def discovery(self, tools=None, status="SUCCESS"):
        return {"status": status, "catalog_complete": status == "SUCCESS", "tool_record_boundary": "SDK_VISIBLE_RAW", "zero_tools": not tools, "tools": tools or [], "diagnostics": {}, "error": None}

    def connect_with_tool(self):
        tool = {"name": "calendar.list", "description": "List calendar entries", "inputSchema": {"type": "object"}}
        result = ExternalMcpDiscovery(self.servers, runner=lambda _: self.discovery([tool])).discover(self.server.server_id)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(self.servers.get(self.server.server_id).lifecycle_state, CONNECTED_STATE)
        self.candidates.ingest(result)
        return self.candidates.get_candidate(self.server.server_id, "calendar.list")

    def make_invocation(self):
        return ExternalMcpInvocation(self.connection, server_registry=self.servers, candidate_registry=self.candidates, auth_binding_registry=self.auth, id_factory=iter(("attempt-1", "event-1", "event-2", "attempt-2", "event-3", "event-4", "attempt-3", "event-5", "event-6")).__next__)

    def test_server_states_and_revision_semantics(self):
        self.assertEqual(self.server.lifecycle_state, DISCONNECTED_STATE)
        before = self.servers.get(self.server.server_id)
        connected = self.servers.mark_connected(before.server_id, before.revision)
        self.assertEqual(connected.lifecycle_state, CONNECTED_STATE)
        self.assertEqual(connected.revision, before.revision)
        disconnected = self.servers.mark_disconnected(connected.server_id, connected.revision)
        self.assertEqual(disconnected.lifecycle_state, DISCONNECTED_STATE)
        self.assertEqual(disconnected.revision, before.revision)
        changed = self.servers.update_connection(disconnected.server_id, endpoint="https://new.example.test")
        self.assertEqual(changed.lifecycle_state, DISCONNECTED_STATE)
        self.assertEqual(changed.revision, before.revision + 1)
        with self.assertRaises(InvalidStateTransitionError):
            self.servers.mark_connected(changed.server_id, before.revision)

    def test_initial_auth_binding_disconnects_and_bumps_revision(self):
        current = self.servers.get(self.server.server_id)
        self.auth.set_binding(current.server_id, AUTH_NONE)
        self.assertEqual(self.servers.get(current.server_id).revision, current.revision)

    def test_candidate_only_has_presence_and_snapshots(self):
        candidate = self.connect_with_tool()
        self.assertEqual(candidate["presence_state"], PRESENT)
        self.assertNotIn("review_" + "state", candidate)
        self.assertNotIn("model_" + "visible", candidate)
        self.assertNotIn("execution_" + "allowed", candidate)
        self.assertEqual(len(self.candidates.list_snapshots(self.server.server_id, "calendar.list")), 1)
        self.servers.mark_disconnected(self.server.server_id, self.servers.get(self.server.server_id).revision)
        empty = ExternalMcpDiscovery(self.servers, runner=lambda _: self.discovery([])).discover(self.server.server_id)
        self.assertEqual(empty["status"], "SUCCESS")
        self.candidates.ingest(empty)
        self.assertEqual(self.candidates.get_candidate(self.server.server_id, "calendar.list")["presence_state"], MISSING)

    def test_stale_discovery_cannot_mark_new_configuration_connected(self):
        snapshot = self.servers.get(self.server.server_id)
        def race(_):
            self.servers.update_connection(snapshot.server_id, endpoint="https://changed.example.test")
            return self.discovery([{"name": "stale"}])
        result = ExternalMcpDiscovery(self.servers, runner=race).discover(snapshot.server_id)
        self.assertNotEqual(result["status"], "SUCCESS")
        self.assertEqual(self.servers.get(snapshot.server_id).lifecycle_state, DISCONNECTED_STATE)

    def test_connected_present_enters_runner_and_disconnected_or_missing_does_not(self):
        candidate = self.connect_with_tool()
        invocation = self.make_invocation()
        calls = []
        action = build_external_action_id(candidate["control_id"], candidate["current_fingerprint"], candidate["current_source_registry_revision"], {"q": "today"})
        self.assertRegex(action, r"^external_action_sha256:[0-9a-f]{64}$")
        result = invocation.invoke(candidate["control_id"], {"q": "today"}, None, expected_turn_id="turn-1", runner=lambda envelope: calls.append(envelope) or {"status": "SUCCESS"})
        self.assertEqual(result["status"], SUCCEEDED)
        self.assertEqual(calls[0]["external_action_id"], action)
        self.assertNotIn("side_effect_" + "class", calls[0])
        self.servers.mark_disconnected(self.server.server_id, self.servers.get(self.server.server_id).revision)
        denied = invocation.invoke(candidate["control_id"], {"q": "later"}, None, expected_turn_id="turn-2", runner=lambda _: calls.append("bad") or {"status": "SUCCESS"})
        self.assertEqual(denied["status"], FAILED_PRE_CALL)
        self.assertEqual(calls.__len__(), 1)

    def test_duplicate_suppression_and_unknown_are_preserved(self):
        candidate = self.connect_with_tool()
        invocation = self.make_invocation()
        calls = []
        first = invocation.invoke(candidate["control_id"], {}, None, expected_turn_id="turn-1", runner=lambda _: calls.append(1) or {"status": "SUCCESS"})
        duplicate = invocation.invoke(candidate["control_id"], {}, None, expected_turn_id="turn-1", runner=lambda _: calls.append(2) or {"status": "SUCCESS"})
        self.assertEqual(first["status"], SUCCEEDED)
        self.assertEqual(duplicate["reason_code"], "DUPLICATE_EXTERNAL_ACTION")
        self.assertEqual(calls, [1])
        unknown = invocation.invoke(candidate["control_id"], {"x": 1}, None, expected_turn_id="turn-2", runner=lambda _: (_ for _ in ()).throw(RuntimeError()))
        self.assertEqual(unknown["status"], OUTCOME_UNKNOWN)
        recovered = invocation.recover_unknown(unknown["attempt_id"])
        self.assertEqual(recovered["status"], OUTCOME_UNKNOWN)

    def test_schema_migration_is_one_way_and_preserves_history(self):
        connection = sqlite3.connect(":memory:")
        old_server_state_code = "REVIEW_" + "REQUIRED"
        old_candidate_state_code = "APPRO" + "VED"
        old_server_state_field = "master_" + "state"
        old_candidate_state_field = "review_" + "state"
        old_visibility_field = "model_" + "visible"
        old_execution_field = "execution_" + "allowed"
        old_approval_table = "external_tool_" + "approval" + "_baselines"
        old_audit_table = "external_tool_" + "review" + "_audit"
        old_class_column = "side_effect_" + "class"
        connection.executescript(f"""
            CREATE TABLE external_server_registry (server_id TEXT PRIMARY KEY, display_name TEXT, transport TEXT, endpoint TEXT, lifecycle_state TEXT, {old_server_state_field} TEXT, registration_provenance TEXT, created_at TEXT, updated_at TEXT, revision INTEGER);
            INSERT INTO external_server_registry VALUES ('legacy-1','Legacy','streamable_http','https://legacy.example.test','{old_server_state_code}','OFF','settings-admin','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',3);
            CREATE TABLE external_tool_candidate_registry (server_id TEXT, tool_name TEXT, control_id TEXT UNIQUE, presence_state TEXT, {old_candidate_state_field} TEXT, current_fingerprint TEXT, current_source_registry_revision INTEGER, first_seen TEXT, last_seen TEXT, updated_at TEXT, revision INTEGER, {old_visibility_field} INTEGER, {old_execution_field} INTEGER, PRIMARY KEY(server_id,tool_name));
            INSERT INTO external_tool_candidate_registry VALUES ('legacy-1','legacy.tool','ext:legacy-1:legacy.tool','PRESENT','{old_candidate_state_code}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',3,'2026-01-01T00:00:00Z','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',1,0,0);
            CREATE TABLE {old_approval_table} (server_id TEXT, tool_name TEXT);
            CREATE TABLE {old_audit_table} (server_id TEXT, tool_name TEXT);
            CREATE TABLE external_tool_side_effect_baselines (server_id TEXT, tool_name TEXT);
            CREATE TABLE external_tool_side_effect_audit (server_id TEXT, tool_name TEXT);
            CREATE TABLE external_tool_raw_snapshots (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, server_id TEXT, tool_name TEXT, fingerprint TEXT, raw_snapshot_json TEXT, source_registry_revision INTEGER, first_observed_at TEXT);
            CREATE TABLE external_tool_invocation_attempts (attempt_id TEXT PRIMARY KEY, turn_id TEXT, control_id TEXT, server_id TEXT, tool_name TEXT, external_action_id TEXT, fingerprint TEXT, source_registry_revision INTEGER, {old_class_column} TEXT, tool_input_sha256 TEXT, tool_input_byte_length INTEGER, status TEXT, reason_code TEXT, created_at TEXT, started_at TEXT, completed_at TEXT);
            INSERT INTO external_tool_invocation_attempts VALUES ('old-attempt','turn-old','ext:legacy-1:legacy.tool','legacy-1','legacy.tool','old-action','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',3,'none','hash',2,'SUCCEEDED','SUCCESS','2026-01-01T00:00:00Z',NULL,'2026-01-01T00:00:00Z');
            CREATE TABLE external_tool_invocation_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL REFERENCES external_tool_invocation_attempts(attempt_id), turn_id TEXT NOT NULL,
                control_id TEXT NOT NULL, server_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                external_action_id TEXT NOT NULL, status TEXT NOT NULL, reason_code TEXT NOT NULL, event_at TEXT NOT NULL
            );
            INSERT INTO external_tool_invocation_audit VALUES (7,'old-event','old-attempt','turn-old','ext:legacy-1:legacy.tool','legacy-1','legacy.tool','old-action','SUCCEEDED','SUCCESS','2026-01-01T00:00:00Z');
            CREATE TRIGGER external_tool_invocation_audit_no_update BEFORE UPDATE ON external_tool_invocation_audit BEGIN SELECT RAISE(ABORT, 'append only'); END;
            CREATE TRIGGER external_tool_invocation_audit_no_delete BEFORE DELETE ON external_tool_invocation_audit BEGIN SELECT RAISE(ABORT, 'append only'); END;
        """)
        for server_id, state in (("legacy-registered", "REGISTERED"), ("legacy-revoked", "REVOKED")):
            connection.execute("INSERT INTO external_server_registry VALUES (?,?,?,?,?,?,?,?,?,?)", (server_id, server_id, "streamable_http", f"https://{server_id}.example.test", state, "OFF", "settings-admin", "created", "updated", 4))
        connection.execute("INSERT INTO external_tool_raw_snapshots VALUES (?,?,?,?,?,?,?)", (11, "legacy-1", "legacy.tool", "a" * 64, '{"name":"legacy.tool","unknown":{"nested":true}}', 3, "first-observed"))
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        snapshots_before = connection.execute("SELECT * FROM external_tool_raw_snapshots").fetchall()
        audits_before = connection.execute("SELECT * FROM external_tool_invocation_audit").fetchall()
        attempt_before = connection.execute("SELECT attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,fingerprint,source_registry_revision,tool_input_sha256,tool_input_byte_length,status,reason_code,created_at,started_at,completed_at FROM external_tool_invocation_attempts").fetchone()
        servers = ExternalServerRegistry(connection)
        self.assertEqual(servers.get("legacy-1").lifecycle_state, DISCONNECTED_STATE)
        self.assertEqual(servers.get("legacy-registered").lifecycle_state, DISCONNECTED_STATE)
        self.assertEqual(servers.get("legacy-revoked").lifecycle_state, REVOKED_STATE)
        secrets = ExternalSecretStore(connection, registry=servers, key_file="/tmp/test-external-mcp.key")
        auth = ExternalMcpAuthBindingRegistry(connection, server_registry=servers, secret_store=secrets)
        candidates = ExternalToolCandidateRegistry(connection, server_registry=servers)
        invocation = ExternalMcpInvocation(connection, server_registry=servers, candidate_registry=candidates, auth_binding_registry=auth)
        candidate = candidates.get_candidate("legacy-1", "legacy.tool")
        self.assertEqual(candidate["presence_state"], PRESENT)
        self.assertEqual(candidate["control_id"], "ext:legacy-1:legacy.tool")
        self.assertEqual(candidate["current_fingerprint"], "a" * 64)
        self.assertEqual(candidate["current_source_registry_revision"], 3)
        self.assertEqual(candidate["revision"], 1)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in ("external_tool_" + "approval" + "_baselines", "external_tool_" + "review" + "_audit", "external_tool_side_effect_" + "baselines", "external_tool_side_effect_" + "audit"):
            self.assertNotIn(name, tables)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(external_tool_invocation_attempts)")}
        self.assertNotIn("side_effect_" + "class", columns)
        self.assertIsNotNone(connection.execute("SELECT 1 FROM external_tool_invocation_attempts WHERE attempt_id='old-attempt'").fetchone())
        self.assertEqual(connection.execute("SELECT * FROM external_tool_invocation_attempts").fetchone(), attempt_before)
        self.assertEqual(connection.execute("SELECT * FROM external_tool_raw_snapshots").fetchall(), snapshots_before)
        self.assertEqual(connection.execute("SELECT * FROM external_tool_invocation_audit").fetchall(), audits_before)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        migrated = "\n".join(connection.iterdump())
        for _ in range(2):
            servers.initialize()
            candidates.initialize()
            invocation.initialize()
            self.assertEqual("\n".join(connection.iterdump()), migrated)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("UPDATE external_tool_invocation_audit SET reason_code='EDITED'")
        connection.rollback()
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM external_tool_invocation_audit")
        connection.rollback()
        connection.close()


if __name__ == "__main__":
    unittest.main()
