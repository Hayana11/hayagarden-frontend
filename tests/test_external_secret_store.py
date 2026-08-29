import json
import importlib.util
import os
import secrets
import socket
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from tools.external_secret_store import (
    ACTIVE_STATE,
    ActiveSlotConflictError,
    ExternalSecretStore,
    REVOKED_SECRET_STATE,
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreValidationError,
)
from tools.external_server_registry import ExternalServerRegistry, REVOKED_STATE


_VAULT_PATH = Path(__file__).resolve().parents[1] / "relay" / "credential_vault.py"
_VAULT_SPEC = importlib.util.spec_from_file_location("_test_credential_vault", _VAULT_PATH)
assert _VAULT_SPEC is not None and _VAULT_SPEC.loader is not None
_VAULT = importlib.util.module_from_spec(_VAULT_SPEC)
_VAULT_SPEC.loader.exec_module(_VAULT)
decrypt_secret = _VAULT.decrypt_secret


class ExternalSecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "secrets.sqlite3"
        self.key_path = self.root / "external.key"
        self.key_path.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            self.key_path.chmod(0o600)
        self.connection = sqlite3.connect(self.db_path)
        self.registry = ExternalServerRegistry(self.connection)
        self.server = self.registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="admin-import",
        )
        self.store = ExternalSecretStore(
            self.connection, key_file=self.key_path, registry=self.registry
        )

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_create_returns_metadata_and_encrypts_at_rest(self):
        marker = "opaque-token-7f9e"
        record = self.store.create(
            server_id=self.server.server_id, credential_slot="default", secret=marker
        )
        self.assertEqual(record.lifecycle_state, ACTIVE_STATE)
        self.assertTrue(record.configured)
        self.assertNotIn(marker, repr(record))
        self.assertNotIn(marker, json.dumps(record.to_public_dict()))
        row = self.connection.execute(
            "SELECT ciphertext FROM external_secret_records WHERE secret_ref = ?",
            (record.secret_ref,),
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertNotIn(marker, row[0])
        self.assertEqual(decrypt_secret(row[0], key_file=self.key_path), marker)

    def test_metadata_is_only_public_surface(self):
        public_names = set(dir(ExternalSecretStore))
        self.assertFalse(any("decrypt" in name or "plaintext" in name for name in public_names))
        self.assertNotIn("ciphertext", ExternalSecretStore.__annotations__)

    def test_private_runtime_seam_is_exact_ref_and_hides_ciphertext_in_repr(self):
        record = self.store.create(
            server_id=self.server.server_id, slot="runtime", secret="runtime-marker"
        )
        runtime = self.store._load_runtime_record(record.secret_ref)
        self.assertEqual(runtime.secret_ref, record.secret_ref)
        self.assertEqual(runtime.server_id, self.server.server_id)
        self.assertEqual(runtime.credential_slot, "runtime")
        self.assertEqual(runtime.lifecycle, ACTIVE_STATE)
        self.assertTrue(runtime.ciphertext)
        self.assertNotIn(runtime.ciphertext, repr(runtime))
        with self.assertRaises(SecretNotFoundError):
            self.store._load_runtime_record("missing-ref")

    def test_runtime_seam_does_not_create_slot_or_plaintext_public_apis(self):
        public_names = set(dir(ExternalSecretStore))
        for forbidden in ("get_plaintext", "decrypt_secret", "read_secret", "get_secret", "latest_secret_for_slot"):
            self.assertNotIn(forbidden, public_names)
        self.assertFalse(hasattr(self.store, "find_active_secret"))

    def test_ref_is_backend_generated_and_caller_ref_rejected(self):
        with self.assertRaises(SecretStoreValidationError) as caught:
            self.store.create(
                server_id=self.server.server_id,
                credential_slot="default",
                secret="value",
                secret_ref="caller-choice",
            )
        self.assertEqual(caught.exception.code, "CALLER_SECRET_REF_FORBIDDEN")
        record = self.store.create(
            server_id=self.server.server_id, credential_slot="default", secret="value"
        )
        self.assertTrue(record.secret_ref.startswith("extsecret_"))

    def test_same_server_slot_has_one_active_secret(self):
        self.store.create(server_id=self.server.server_id, slot="default", secret="one")
        with self.assertRaises(ActiveSlotConflictError):
            self.store.create(server_id=self.server.server_id, slot="default", secret="two")

    def test_rotate_preserves_ref_changes_ciphertext_and_revision(self):
        first = self.store.create(
            server_id=self.server.server_id, slot="default", secret="first-marker"
        )
        old_ciphertext = self.connection.execute(
            "SELECT ciphertext FROM external_secret_records WHERE secret_ref = ?",
            (first.secret_ref,),
        ).fetchone()[0]
        second = self.store.rotate(
            server_id=self.server.server_id, slot="default", secret="second-marker"
        )
        new_ciphertext = self.connection.execute(
            "SELECT ciphertext FROM external_secret_records WHERE secret_ref = ?",
            (first.secret_ref,),
        ).fetchone()[0]
        self.assertEqual(second.secret_ref, first.secret_ref)
        self.assertEqual(second.revision, first.revision + 1)
        self.assertNotEqual(old_ciphertext, new_ciphertext)
        self.assertEqual(decrypt_secret(new_ciphertext, key_file=self.key_path), "second-marker")
        third = self.store.rotate(secret_ref=first.secret_ref, secret="third-marker")
        self.assertEqual(third.secret_ref, first.secret_ref)
        self.assertEqual(third.revision, second.revision + 1)

    def test_revoke_is_terminal_clears_ciphertext_and_allows_new_ref(self):
        first = self.store.create(
            server_id=self.server.server_id, slot="default", secret="revoke-marker"
        )
        revoked = self.store.revoke(first.secret_ref)
        self.assertEqual(revoked.lifecycle_state, REVOKED_SECRET_STATE)
        self.assertFalse(revoked.configured)
        self.assertEqual(
            self.connection.execute(
                "SELECT ciphertext FROM external_secret_records WHERE secret_ref = ?",
                (first.secret_ref,),
            ).fetchone()[0],
            None,
        )
        again = self.store.revoke(first.secret_ref)
        self.assertEqual(again.revision, revoked.revision)
        replacement = self.store.create(
            server_id=self.server.server_id, slot="default", secret="replacement"
        )
        self.assertNotEqual(replacement.secret_ref, first.secret_ref)

    def test_unknown_and_revoked_server_fail_closed(self):
        with self.assertRaises(SecretStoreError) as unknown:
            self.store.create(server_id="missing-server", slot="default", secret="x")
        self.assertEqual(unknown.exception.code, "UNKNOWN_SERVER")
        existing = self.store.create(
            server_id=self.server.server_id, slot="default", secret="before-revoke"
        )
        self.registry.revoke(self.server.server_id)
        with self.assertRaises(SecretStoreError) as create_error:
            self.store.create(server_id=self.server.server_id, slot="other", secret="x")
        self.assertEqual(create_error.exception.code, "SERVER_REVOKED")
        with self.assertRaises(SecretStoreError) as rotate_error:
            self.store.rotate(server_id=self.server.server_id, slot="default", secret="x")
        self.assertEqual(rotate_error.exception.code, "SERVER_REVOKED")
        self.assertEqual(self.store.revoke(existing.secret_ref).lifecycle_state, REVOKED_SECRET_STATE)

    def test_secret_input_is_opaque_and_rejects_controls_and_overflow(self):
        with self.assertRaises(SecretStoreValidationError):
            self.store.create(server_id=self.server.server_id, slot="default", secret="")
        with self.assertRaises(SecretStoreValidationError):
            self.store.create(server_id=self.server.server_id, slot="default", secret="bad\nvalue")
        with self.assertRaises(SecretStoreValidationError):
            self.store.create(
                server_id=self.server.server_id,
                slot="default",
                secret="x" * 4097,
            )

    def test_allowed_metadata_cannot_carry_secret_literals(self):
        for field in ("secret_ref", "slot"):
            kwargs = {field: "Authorization: Bearer top-secret"}
            with self.assertRaises(SecretStoreValidationError):
                self.store.create(
                    server_id=self.server.server_id,
                    credential_slot="other" if field == "secret_ref" else None,
                    secret="value",
                    **kwargs,
                )

    def test_key_missing_invalid_and_wrong_fail_without_rows(self):
        missing = self.root / "missing.key"
        missing_connection = sqlite3.connect(":memory:")
        missing_registry = ExternalServerRegistry(missing_connection)
        missing_server = missing_registry.register(
            display_name="Missing key test",
            endpoint="https://missing-key.example/mcp",
            provenance="test",
        )
        missing_store = ExternalSecretStore(
            missing_connection, key_file=missing, registry=missing_registry
        )
        with self.assertRaises(SecretStoreError) as missing_error:
            missing_store.create(server_id=missing_server.server_id, slot="default", secret="marker")
        self.assertEqual(missing_error.exception.code, "ENCRYPTION_FAILED")
        self.assertEqual(missing_connection.execute("SELECT COUNT(*) FROM external_secret_records").fetchone()[0], 0)
        missing_connection.close()

        invalid = self.root / "invalid.key"
        invalid.write_text("not-a-fernet-key", encoding="ascii")
        if os.name == "posix":
            invalid.chmod(0o600)
        invalid_store = ExternalSecretStore(
            self.connection, key_file=invalid, registry=self.registry
        )
        with self.assertRaises(SecretStoreError) as invalid_error:
            invalid_store.create(server_id=self.server.server_id, slot="default", secret="marker")
        self.assertEqual(invalid_error.exception.code, "ENCRYPTION_FAILED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM external_secret_records").fetchone()[0], 0)

        wrong_key = self.root / "wrong.key"
        wrong_key.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            wrong_key.chmod(0o600)
        valid = self.store.create(server_id=self.server.server_id, slot="default", secret="wrong-key-marker")
        ciphertext = self.connection.execute(
            "SELECT ciphertext FROM external_secret_records WHERE secret_ref = ?",
            (valid.secret_ref,),
        ).fetchone()[0]
        with self.assertRaises(Exception):
            decrypt_secret(ciphertext, key_file=wrong_key)
        self.assertEqual(self.store.get_metadata(valid.secret_ref).revision, 1)
        with self.assertRaises(Exception):
            decrypt_secret(Fernet.generate_key().decode("ascii"), key_file=wrong_key)

    @unittest.skipUnless(os.name == "posix", "POSIX key permissions are required")
    def test_key_permissions_fail_closed(self):
        insecure = self.root / "insecure.key"
        insecure.write_bytes(Fernet.generate_key())
        insecure.chmod(0o644)
        store = ExternalSecretStore(self.connection, key_file=insecure, registry=self.registry)
        with self.assertRaises(SecretStoreError) as caught:
            store.create(server_id=self.server.server_id, slot="default", secret="marker")
        self.assertEqual(caught.exception.code, "ENCRYPTION_FAILED")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM external_secret_records").fetchone()[0], 0)

    def test_reopen_preserves_reference_and_ciphertext_is_not_registry_owned(self):
        record = self.store.create(server_id=self.server.server_id, slot="default", secret="persist")
        self.connection.close()
        reopened = sqlite3.connect(self.db_path)
        registry = ExternalServerRegistry(reopened)
        store = ExternalSecretStore(reopened, key_file=self.key_path, registry=registry)
        self.assertEqual(store.get_metadata(record.secret_ref), record)
        registry_columns = {
            row[1] for row in reopened.execute("PRAGMA table_info(external_server_registry)")
        }
        self.assertNotIn("ciphertext", registry_columns)
        secret_columns = {
            row[1] for row in reopened.execute("PRAGMA table_info(external_secret_records)")
        }
        self.assertIn("ciphertext", secret_columns)
        self.assertNotIn("plaintext", secret_columns)
        reopened.close()
        self.connection = sqlite3.connect(self.db_path)

    def test_collision_does_not_overwrite_existing_record(self):
        refs = iter(("ref-collision", "ref-collision", "ref-second"))
        store = ExternalSecretStore(
            self.connection,
            key_file=self.key_path,
            registry=self.registry,
            id_factory=lambda: next(refs),
        )
        first = store.create(server_id=self.server.server_id, slot="first", secret="one")
        second = store.create(server_id=self.server.server_id, slot="second", secret="two")
        self.assertEqual(first.secret_ref, "ref-collision")
        self.assertEqual(second.secret_ref, "ref-second")
        self.assertEqual(store.get_metadata(first.secret_ref).revision, 1)

    def test_no_network_is_required(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            record = self.store.create(server_id=self.server.server_id, slot="default", secret="offline")
        self.assertEqual(record.lifecycle_state, ACTIVE_STATE)

    def test_concurrent_same_slot_has_at_most_one_winner(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(value):
            connection = sqlite3.connect(self.db_path, timeout=5)
            try:
                registry = ExternalServerRegistry(connection)
                store = ExternalSecretStore(connection, key_file=self.key_path, registry=registry)
                barrier.wait()
                results.append(store.create(server_id=self.server.server_id, slot="race", secret=value))
            except Exception as exc:  # each worker reports, never swallows a race
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(f"value-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ActiveSlotConflictError)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM external_secret_records WHERE server_id = ? "
                "AND credential_slot = 'race' AND lifecycle_state = 'ACTIVE'",
                (self.server.server_id,),
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()

