from __future__ import annotations

import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone

from tools.external_server_registry import (
    DuplicateEndpointError,
    ExternalServerRegistry,
    InvalidStateTransitionError,
    MASTER_OFF,
    REGISTRATION_STATE,
    REVIEW_REQUIRED_STATE,
    REVOKED_STATE,
    RegistryValidationError,
    UnknownServerError,
)


BASE_TIME = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


class ExternalServerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = self.temp_dir.name + "/external-servers.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.owner = ExternalServerRegistry(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def register(
        self, *, name="Calendar", endpoint="https://calendar.example/mcp", **fields
    ):
        return self.owner.register(
            display_name=name,
            endpoint=endpoint,
            provenance="owner-admin",
            now=BASE_TIME,
            **fields,
        )

    def test_new_registration_has_opaque_backend_generated_id_and_default_off(self):
        record = self.register()
        self.assertTrue(record.server_id)
        self.assertNotEqual(record.server_id, record.display_name)
        self.assertNotEqual(record.server_id, "https://calendar.example/mcp")
        self.assertNotIn(":", record.server_id)
        self.assertEqual(record.lifecycle_state, REGISTRATION_STATE)
        self.assertEqual(record.master_state, MASTER_OFF)
        self.assertFalse(record.model_visible)
        self.assertFalse(record.execution_allowed)
        self.assertEqual(record.transport, "streamable_http")
        self.assertEqual(record.endpoint, "https://calendar.example/mcp")

    def test_caller_cannot_set_identity_or_secret_like_fields(self):
        with self.assertRaises(RegistryValidationError) as identity_error:
            self.register(**{"server_id": "caller-id"})
        self.assertEqual(identity_error.exception.code, "UNSUPPORTED_FIELD")
        for field in (
            "token", "api_key", "key", "secret", "authorization", "cookie",
            "headers", "credential", "password", "bearer",
        ):
            with self.assertRaises(RegistryValidationError) as secret_error:
                self.register(**{field: "plaintext"})
            self.assertEqual(secret_error.exception.code, "SECRET_FIELD_FORBIDDEN")
        with self.assertRaises(RegistryValidationError):
            self.owner.register_record(
                {
                    "display_name": "Calendar",
                    "endpoint": "https://calendar.example/mcp",
                    "provenance": "owner-admin",
                    "headers": {"Authorization": "plaintext"},
                }
            )

    def test_reopen_persists_record_and_display_rename_preserves_identity(self):
        original = self.register()
        self.connection.close()
        reopened_connection = sqlite3.connect(self.path)
        reopened = ExternalServerRegistry(reopened_connection)
        restored = reopened.get(original.server_id)
        renamed = reopened.rename(original.server_id, "Renamed", now=BASE_TIME)
        self.assertEqual(restored, original)
        self.assertEqual(renamed.server_id, original.server_id)
        self.assertEqual(renamed.endpoint, original.endpoint)
        self.assertEqual(renamed.lifecycle_state, REGISTRATION_STATE)
        self.assertEqual(renamed.master_state, MASTER_OFF)
        reopened_connection.close()

    def test_endpoint_change_preserves_id_requires_review_and_forces_off(self):
        original = self.register()
        changed = self.owner.update_connection(
            original.server_id,
            endpoint="https://calendar-new.example/mcp",
            now=BASE_TIME,
        )
        self.assertEqual(changed.server_id, original.server_id)
        self.assertEqual(changed.endpoint, "https://calendar-new.example/mcp")
        self.assertEqual(changed.lifecycle_state, REVIEW_REQUIRED_STATE)
        self.assertEqual(changed.master_state, MASTER_OFF)
        self.assertFalse(changed.model_visible)
        self.assertFalse(changed.execution_allowed)

    def test_transport_outside_frozen_scope_is_rejected_without_state_change(self):
        original = self.register()
        with self.assertRaises(RegistryValidationError) as error:
            self.owner.update_transport(original.server_id, "sse", now=BASE_TIME)
        self.assertEqual(error.exception.code, "UNSUPPORTED_TRANSPORT")
        self.assertEqual(self.owner.get(original.server_id), original)

    def test_revoke_is_terminal_auditable_and_never_reuses_id(self):
        original = self.register()
        revoked = self.owner.revoke(original.server_id, now=BASE_TIME)
        replacement = self.register(name="Replacement")
        self.assertEqual(revoked.server_id, original.server_id)
        self.assertEqual(revoked.lifecycle_state, REVOKED_STATE)
        self.assertEqual(revoked.master_state, MASTER_OFF)
        self.assertEqual(self.owner.get(original.server_id).lifecycle_state, REVOKED_STATE)
        self.assertNotEqual(replacement.server_id, original.server_id)
        with self.assertRaises(InvalidStateTransitionError):
            self.owner.rename(original.server_id, "No resurrection")

    def test_same_active_endpoint_is_rejected_but_revoked_endpoint_can_be_registered(self):
        original = self.register()
        with self.assertRaises(DuplicateEndpointError):
            self.register(name="Duplicate")
        self.owner.revoke(original.server_id, now=BASE_TIME)
        replacement = self.register(name="Replacement")
        self.assertEqual(replacement.endpoint, original.endpoint)
        self.assertNotEqual(replacement.server_id, original.server_id)

    def test_unknown_server_reads_updates_and_revokes_fail_closed(self):
        operations = (
            lambda: self.owner.get("unknown"),
            lambda: self.owner.rename("unknown", "Nope"),
            lambda: self.owner.update_connection(
                "unknown", endpoint="https://new.example/mcp"
            ),
            lambda: self.owner.revoke("unknown"),
        )
        for operation in operations:
            with self.assertRaises(UnknownServerError):
                operation()

    def test_endpoint_contract_rejects_non_https_userinfo_query_and_fragment(self):
        endpoints = (
            "http://calendar.example/mcp",
            "https://user:password@calendar.example/mcp",
            "https://calendar.example/mcp?token=plaintext",
            "https://calendar.example/mcp?x=1",
            "https://calendar.example/mcp#fragment",
        )
        for endpoint in endpoints:
            with self.assertRaises(RegistryValidationError):
                self.register(endpoint=endpoint)

    def test_register_record_is_strict_and_transport_is_frozen_for_new_records(self):
        with self.assertRaises(RegistryValidationError) as transport_error:
            self.owner.register_record(
                {
                    "display_name": "Calendar",
                    "endpoint": "https://calendar.example/mcp",
                    "provenance": "owner-admin",
                    "transport": "sse",
                }
            )
        self.assertEqual(transport_error.exception.code, "UNSUPPORTED_TRANSPORT")

    def test_secret_literals_are_rejected_from_allowed_provenance_and_endpoint_path(self):
        for provenance in (
            "Authorization: Bearer plaintext",
            "api_key=plaintext",
            "secret-ref",
            "token",
        ):
            with self.assertRaises(RegistryValidationError):
                self.owner.register(
                    display_name="Calendar",
                    endpoint="https://calendar.example/mcp",
                    provenance=provenance,
                    now=BASE_TIME,
                )
        for endpoint in (
            "https://calendar.example/mcp/Authorization:Bearer/plaintext",
            "https://calendar.example/mcp/token=plaintext",
            "https://calendar.example/mcp/secret-value",
            "https://calendar.example/mcp%2Ftoken",
        ):
            with self.assertRaises(RegistryValidationError):
                self.register(endpoint=endpoint)

    def test_control_plane_has_no_network_or_tools_list_path(self):
        def fail_network(*args, **kwargs):
            raise AssertionError("external network access is forbidden in M5-01")

        old_socket = socket.socket
        old_urlopen = urllib.request.urlopen
        socket.socket = fail_network
        urllib.request.urlopen = fail_network
        try:
            record = self.register()
        finally:
            socket.socket = old_socket
            urllib.request.urlopen = old_urlopen
        self.assertTrue(record.server_id)
        self.assertNotIn("tools/list", record.endpoint)

    def test_registry_only_touches_its_injected_schema(self):
        self.connection.execute(
            "CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.connection.commit()
        record = self.register()
        self.assertTrue(self.path.endswith("external-servers.sqlite3"))
        count = self.connection.execute(
            "SELECT COUNT(*) FROM runtime_config"
        ).fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.owner.get(record.server_id).server_id, record.server_id)

    def test_concurrent_same_endpoint_allows_at_most_one_active_record(self):
        setup = sqlite3.connect(self.path)
        ExternalServerRegistry(setup)
        setup.close()
        barrier = threading.Barrier(2)
        successes = []
        failures = []
        lock = threading.Lock()

        def worker(index):
            connection = sqlite3.connect(self.path, timeout=5)
            owner = ExternalServerRegistry(connection)
            try:
                barrier.wait(timeout=5)
                record = owner.register(
                    display_name=f"Calendar {index}",
                    endpoint="https://calendar.example/mcp",
                    provenance="owner-admin",
                    now=BASE_TIME,
                )
                with lock:
                    successes.append(record)
            except Exception as exc:
                with lock:
                    failures.append(exc)
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in (1, 2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], DuplicateEndpointError)
        self.assertEqual(len({record.server_id for record in successes}), 1)


if __name__ == "__main__":
    unittest.main()
