from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from cryptography.fernet import Fernet

from tools.external_mcp_auth_binding import (
    AUTH_BEARER,
    AUTH_NONE,
    ExternalMcpAuthBindingError,
    ExternalMcpAuthBindingRegistry,
)
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import (
    MASTER_OFF,
    REVIEW_REQUIRED_STATE,
    ExternalServerRegistry,
)


class ExternalMcpAuthBindingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        ids = iter(("server-1", "server-2", "server-3"))
        self.servers = ExternalServerRegistry(self.connection, id_factory=lambda: next(ids))
        self.server = self.servers.register(
            display_name="Calendar", endpoint="https://calendar.example/mcp", provenance="owner-admin"
        )
        self.tempdir = tempfile.TemporaryDirectory()
        self.key_path = os.path.join(self.tempdir.name, "key")
        with open(self.key_path, "wb") as key_file:
            key_file.write(Fernet.generate_key())
        if os.name == "posix":
            os.chmod(self.key_path, 0o600)
        self.store = ExternalSecretStore(self.connection, key_file=self.key_path, registry=self.servers)
        self.bindings = ExternalMcpAuthBindingRegistry(
            self.connection, server_registry=self.servers, secret_store=self.store
        )

    def tearDown(self):
        self.connection.close()
        self.tempdir.cleanup()

    def test_none_binding_has_null_metadata_and_invalidates_trust(self):
        binding = self.bindings.set_binding(self.server.server_id, AUTH_NONE)
        current = self.servers.get(self.server.server_id)
        self.assertEqual((binding.secret_ref, binding.credential_slot), (None, None))
        self.assertEqual(binding.revision, 1)
        self.assertEqual(current.revision, self.server.revision + 1)
        self.assertEqual(current.lifecycle_state, REVIEW_REQUIRED_STATE)
        self.assertEqual(current.master_state, MASTER_OFF)
        self.assertEqual(self.bindings.set_binding(self.server.server_id, AUTH_NONE).revision, 1)

    def test_bearer_requires_active_exact_server_and_derived_slot(self):
        secret = self.store.create(server_id=self.server.server_id, credential_slot="slot-a", secret="opaque-test-value")
        binding = self.bindings.set_binding(self.server.server_id, AUTH_BEARER, secret_ref=secret.secret_ref)
        self.assertEqual(binding.secret_ref, secret.secret_ref)
        self.assertEqual(binding.credential_slot, "slot-a")
        with self.assertRaisesRegex(ExternalMcpAuthBindingError, "credential slot"):
            self.bindings.set_binding(self.server.server_id, AUTH_BEARER, secret_ref=secret.secret_ref, credential_slot="slot-b")
        other = self.servers.register(display_name="Other", endpoint="https://other.example/mcp", provenance="owner-admin")
        with self.assertRaises(ExternalMcpAuthBindingError):
            self.bindings.set_binding(other.server_id, AUTH_BEARER, secret_ref=secret.secret_ref)

    def test_unknown_and_revoked_secret_fail_closed_without_ciphertext_in_binding(self):
        with self.assertRaises(ExternalMcpAuthBindingError):
            self.bindings.set_binding(self.server.server_id, AUTH_BEARER, secret_ref="missing")
        secret = self.store.create(server_id=self.server.server_id, credential_slot="slot-a", secret="opaque-test-value")
        self.store.revoke(secret.secret_ref)
        with self.assertRaises(ExternalMcpAuthBindingError):
            self.bindings.set_binding(self.server.server_id, AUTH_BEARER, secret_ref=secret.secret_ref)
        row = self.connection.execute("SELECT server_id, auth_scheme, secret_ref, credential_slot FROM external_mcp_auth_bindings").fetchone()
        self.assertIsNone(row)
        self.assertNotIn("opaque-test-value", "\n".join(self.connection.iterdump()))

    def test_failed_binding_write_rolls_back_binding_and_server_revision(self):
        before = self.servers.get(self.server.server_id)
        self.connection.execute(
            "CREATE TRIGGER fail_auth_insert BEFORE INSERT ON external_mcp_auth_bindings "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )
        with self.assertRaises(sqlite3.Error):
            self.bindings.set_binding(self.server.server_id, AUTH_NONE)
        self.assertIsNone(self.bindings.get_binding(self.server.server_id))
        self.assertEqual(self.servers.get(self.server.server_id).revision, before.revision)

    def test_constructor_rejects_mixed_authority(self):
        other = sqlite3.connect(":memory:")
        try:
            other_servers = ExternalServerRegistry(other)
            other_store = ExternalSecretStore(other, key_file=tempfile.NamedTemporaryFile(delete=False).name, registry=other_servers)
            with self.assertRaises(Exception):
                ExternalMcpAuthBindingRegistry(self.connection, server_registry=other_servers, secret_store=other_store)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
