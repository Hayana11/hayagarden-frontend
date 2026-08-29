from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from tools.external_mcp_secret_materializer import (
    AUTH_BINDING_INVALID,
    AUTH_SCHEME_UNSUPPORTED,
    AUTH_SECRET_UNAVAILABLE,
    ExternalMcpSecretMaterializer,
    ExternalMcpSecretMaterializerError,
    MaterializedExternalMcpAuth,
)
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import ExternalServerRegistry


class ExternalMcpSecretMaterializerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.key_file = root / "external.key"
        self.key_file.write_bytes(Fernet.generate_key())
        self.connection = sqlite3.connect(root / "secrets.sqlite3")
        self.registry = ExternalServerRegistry(self.connection)
        self.server = self.registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="test",
        )
        self.store = ExternalSecretStore(
            self.connection, key_file=self.key_file, registry=self.registry
        )
        self.materializer = ExternalMcpSecretMaterializer(
            self.store, key_file=self.key_file
        )

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def auth(self, **overrides):
        value = {
            "server_id": self.server.server_id,
            "auth_scheme": "bearer",
            "secret_ref": None,
            "credential_slot": None,
        }
        value.update(overrides)
        return value

    def test_none_has_zero_secret_lookup_decrypt_and_key_access(self):
        with mock.patch.object(self.store, "_load_runtime_record", side_effect=AssertionError("lookup")), \
             mock.patch("tools.external_mcp_secret_materializer.decrypt_secret", side_effect=AssertionError("decrypt")), \
             mock.patch("builtins.open", side_effect=AssertionError("key access")):
            result = self.materializer.materialize(
                self.auth(auth_scheme="none", secret_ref=None, credential_slot=None)
            )
        self.assertEqual(result, MaterializedExternalMcpAuth(scheme="none"))
        self.assertIsNone(result.credential)

    def test_malformed_none_and_unsupported_scheme_have_stable_errors(self):
        with self.assertRaises(ExternalMcpSecretMaterializerError) as malformed:
            self.materializer.materialize(
                self.auth(auth_scheme="none", secret_ref="ref", credential_slot=None)
            )
        self.assertEqual(malformed.exception.code, AUTH_BINDING_INVALID)
        with self.assertRaises(ExternalMcpSecretMaterializerError) as unsupported:
            self.materializer.materialize(self.auth(auth_scheme="basic"))
        self.assertEqual(unsupported.exception.code, AUTH_SCHEME_UNSUPPORTED)

    def test_bearer_loads_only_exact_active_ref_server_and_slot(self):
        record = self.store.create(
            server_id=self.server.server_id, slot="default", secret="canary-credential"
        )
        result = self.materializer.materialize(
            self.auth(secret_ref=record.secret_ref, credential_slot="default")
        )
        self.assertEqual(result.scheme, "bearer")
        self.assertEqual(result.credential, "canary-credential")
        self.assertNotIn("canary-credential", repr(result))
        for overrides in (
            {"secret_ref": "missing", "credential_slot": "default"},
            {"secret_ref": record.secret_ref, "credential_slot": "wrong"},
            {"server_id": "wrong", "secret_ref": record.secret_ref, "credential_slot": "default"},
        ):
            with self.assertRaises(ExternalMcpSecretMaterializerError) as caught:
                self.materializer.materialize(self.auth(**overrides))
            self.assertEqual(caught.exception.code, AUTH_SECRET_UNAVAILABLE)

    def test_revoked_secret_and_decrypt_failure_are_unavailable(self):
        record = self.store.create(
            server_id=self.server.server_id, slot="revoked", secret="credential"
        )
        self.store.revoke(record.secret_ref)
        with self.assertRaises(ExternalMcpSecretMaterializerError) as revoked:
            self.materializer.materialize(
                self.auth(secret_ref=record.secret_ref, credential_slot="revoked")
            )
        self.assertEqual(revoked.exception.code, AUTH_SECRET_UNAVAILABLE)
        active = self.store.create(
            server_id=self.server.server_id, slot="decrypt", secret="credential"
        )
        with mock.patch("tools.external_mcp_secret_materializer.decrypt_secret", side_effect=ValueError("secret must not escape")):
            with self.assertRaises(ExternalMcpSecretMaterializerError) as failed:
                self.materializer.materialize(
                    self.auth(secret_ref=active.secret_ref, credential_slot="decrypt")
                )
        self.assertEqual(failed.exception.code, AUTH_SECRET_UNAVAILABLE)
        self.assertNotIn("secret must not escape", str(failed.exception))

    def test_credential_bounds_and_exact_input_keys(self):
        with self.assertRaises(ExternalMcpSecretMaterializerError) as extra:
            self.materializer.materialize({**self.auth(), "extra": "reject"})
        self.assertEqual(extra.exception.code, AUTH_BINDING_INVALID)
        with mock.patch("tools.external_mcp_secret_materializer.decrypt_secret", return_value="x" * (16 * 1024 + 1)):
            record = self.store.create(
                server_id=self.server.server_id, slot="oversize", secret="credential"
            )
            with self.assertRaises(ExternalMcpSecretMaterializerError) as oversized:
                self.materializer.materialize(
                    self.auth(secret_ref=record.secret_ref, credential_slot="oversize")
                )
        self.assertEqual(oversized.exception.code, AUTH_SECRET_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()

