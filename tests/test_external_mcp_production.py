from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from tools.external_mcp_production import (
    EXTERNAL_MCP_DB_PATH,
    EXTERNAL_MCP_KEY_FILE,
    ExternalMcpProductionInitializationError,
    open_external_mcp_production,
)
from tools.external_secret_store import ExternalSecretStore


SCHEMA_TABLES = {
    "external_server_registry",
    "external_secret_records",
    "external_mcp_auth_bindings",
    "external_tool_candidate_registry",
    "external_tool_raw_snapshots",
    "external_tool_approval_baselines",
    "external_tool_review_audit",
    "external_tool_side_effect_baselines",
    "external_tool_side_effect_audit",
    "external_tool_invocation_attempts",
    "external_tool_invocation_audit",
}
EMPTY_TABLES = {
    "external_server_registry",
    "external_secret_records",
    "external_mcp_auth_bindings",
    "external_tool_candidate_registry",
    "external_tool_invocation_attempts",
    "external_tool_invocation_audit",
}


class ProductionCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "external-mcp.db"
        self.key = root / "external-mcp.key"
        self.key.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            os.chmod(self.key, 0o600)
        real_lstat = os.lstat

        def root_owned_lstat(path: str, *, code: str):
            try:
                metadata = real_lstat(path)
            except OSError as exc:
                raise ExternalMcpProductionInitializationError(code) from exc
            if path in {str(self.key), str(self.db)}:
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_uid=0,
                    st_gid=0,
                    st_nlink=metadata.st_nlink,
                    st_dev=metadata.st_dev,
                    st_ino=metadata.st_ino,
                )
            return metadata

        self.parent_patcher = patch("tools.external_mcp_production._validate_db_parent")
        self.patchers = [
            patch("tools.external_mcp_production.EXTERNAL_MCP_DB_PATH", str(self.db)),
            patch("tools.external_mcp_production.EXTERNAL_MCP_KEY_FILE", str(self.key)),
            self.parent_patcher,
            patch("tools.external_mcp_production._lstat", side_effect=root_owned_lstat),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def _objects(self) -> set[str]:
        with sqlite3.connect(self.db) as connection:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not row[0].startswith("sqlite_")
            }

    def _counts(self) -> dict[str, int]:
        with sqlite3.connect(self.db) as connection:
            return {
                name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in EMPTY_TABLES
            }

    def _assert_open_fails_without_db(self, *, db_absent: bool = True) -> None:
        with self.assertRaises(ExternalMcpProductionInitializationError):
            with open_external_mcp_production():
                self.fail("graph unexpectedly opened")
        if db_absent:
            self.assertFalse(self.db.exists())

    def test_constants_and_import_side_effects(self) -> None:
        self.assertEqual(EXTERNAL_MCP_DB_PATH, "/var/lib/hayagarden/external-mcp.db")
        self.assertEqual(EXTERNAL_MCP_KEY_FILE, "/etc/hayagarden/external-mcp-credentials.key")
        result = subprocess.run(
            [sys.executable, "-c", "import tools.external_mcp_production"],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.db.exists())

    def test_valid_key_opens_full_graph_with_one_connection_and_closes(self) -> None:
        with open_external_mcp_production() as graph:
            connection = graph.server_registry._connection
            for owner in (
                graph.secret_store,
                graph.auth_binding_registry,
                graph.candidate_registry,
                graph.side_effect_policy,
                graph.execution_fence,
                graph.invocation,
            ):
                self.assertIs(owner._connection, connection)
            self.assertIs(graph.runtime._connection, connection)
            self.assertIs(graph.materializer._secret_store, graph.secret_store)
            self.assertEqual(graph.secret_store._key_file, str(self.key))
            self.assertEqual(graph.materializer._key_file, str(self.key))
            self.assertNotIn("connection=", repr(graph))
            self.assertEqual(self._objects(), SCHEMA_TABLES)
            self.assertEqual(self._counts(), {name: 0 for name in EMPTY_TABLES})
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_second_open_reuses_schema_and_initial_rows_stay_zero(self) -> None:
        with open_external_mcp_production():
            pass
        first_objects = self._objects()
        with open_external_mcp_production():
            pass
        self.assertEqual(self._objects(), first_objects)
        self.assertEqual(self._counts(), {name: 0 for name in EMPTY_TABLES})

    def test_key_missing_symlink_mode_owner_hardlink_and_invalid_fernet_fail_closed(self) -> None:
        self.key.unlink()
        self._assert_open_fails_without_db()

        self.key.write_bytes(Fernet.generate_key())
        if os.name != "posix":
            return
        self.key.unlink()
        self.key.symlink_to(self.db)
        self._assert_open_fails_without_db()

        self.key.unlink()
        self.key.write_bytes(Fernet.generate_key())
        os.chmod(self.key, 0o644)
        self._assert_open_fails_without_db()

        self.key.unlink()
        hardlink = self.key.with_name("hardlink")
        hardlink.write_bytes(Fernet.generate_key())
        os.chmod(hardlink, 0o600)
        os.link(hardlink, self.key)
        self._assert_open_fails_without_db()

        self.key.unlink()
        self.key.write_bytes(b"not-a-fernet-key\n")
        os.chmod(self.key, 0o600)
        self._assert_open_fails_without_db()

    def test_key_wrong_owner_fails_closed(self) -> None:
        real_lstat = os.lstat

        def fake_lstat(path: str, *, code: str):
            if path == str(self.key):
                return SimpleNamespace(
                    st_mode=stat.S_IFREG | 0o600,
                    st_uid=12345,
                    st_gid=0,
                    st_nlink=1,
                )
            return real_lstat(path)

        with patch("tools.external_mcp_production._lstat", side_effect=fake_lstat):
            self._assert_open_fails_without_db()

    def test_existing_db_failure_is_never_deleted(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE keep_me (value TEXT)")
        if os.name == "posix":
            os.chmod(self.db, 0o600)
        with patch("tools.external_mcp_production._build_graph", side_effect=RuntimeError("construction failed")):
            with self.assertRaises(RuntimeError):
                with open_external_mcp_production():
                    pass
        self.assertTrue(self.db.exists())
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='keep_me'"
                ).fetchone()[0],
                "keep_me",
            )

    def test_new_db_pre_yield_failure_is_cleaned_safely(self) -> None:
        with patch("tools.external_mcp_production._build_graph", side_effect=RuntimeError("construction failed")):
            with self.assertRaises(RuntimeError):
                with open_external_mcp_production():
                    pass
        self.assertFalse(self.db.exists())

    def test_db_symlink_mode_hardlink_and_wrong_owner_fail_closed(self) -> None:
        if os.name != "posix":
            self.skipTest("POSIX production filesystem contract")
        target = self.db.with_name("target.db")
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE keep_me (value TEXT)")
        os.chmod(target, 0o600)

        self.db.symlink_to(target)
        self._assert_open_fails_without_db(db_absent=False)
        self.db.unlink()
        os.link(target, self.db)
        self._assert_open_fails_without_db(db_absent=False)
        self.db.unlink()
        self.db.write_bytes(b"existing")
        os.chmod(self.db, 0o644)
        self._assert_open_fails_without_db(db_absent=False)
        os.chmod(self.db, 0o600)

        real_lstat = os.lstat

        def fake_lstat(path: str, *, code: str):
            metadata = real_lstat(path)
            if path == str(self.db):
                return SimpleNamespace(
                    st_mode=metadata.st_mode,
                    st_uid=12345,
                    st_gid=0,
                    st_nlink=1,
                )
            return metadata

        with patch("tools.external_mcp_production._lstat", side_effect=fake_lstat):
            self._assert_open_fails_without_db(db_absent=False)

    def test_unsafe_db_parent_fails_before_db_creation(self) -> None:
        real_lstat = os.lstat

        def fake_lstat(path: str, *, code: str):
            if path == "/var/lib/hayagarden":
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o775,
                    st_uid=0,
                    st_gid=0,
                )
            return real_lstat(path)

        self.parent_patcher.stop()
        try:
            with patch("tools.external_mcp_production._lstat", side_effect=fake_lstat):
                self._assert_open_fails_without_db()
        finally:
            self.parent_patcher.start()

    def test_open_has_zero_secret_lookup_decrypt_child_and_network(self) -> None:
        with patch.object(ExternalSecretStore, "_load_runtime_record", side_effect=AssertionError("secret lookup")) as lookup, \
            patch("tools.external_secret_store.decrypt_secret", side_effect=AssertionError("decrypt")) as decrypt, \
            patch("tools.external_mcp_runtime.subprocess.Popen", side_effect=AssertionError("node child")) as child:
            with open_external_mcp_production():
                pass
        lookup.assert_not_called()
        decrypt.assert_not_called()
        child.assert_not_called()

    def test_public_api_has_no_path_override_and_no_fallbacks(self) -> None:
        with self.assertRaises(TypeError):
            open_external_mcp_production(str(self.db), str(self.key))  # type: ignore[arg-type]
        source = (Path(__file__).parents[1] / "tools" / "external_mcp_production.py").read_text(encoding="utf-8")
        self.assertNotIn("memories.db", source)
        self.assertNotIn("relay-credentials.key", source)
        self.key.unlink()
        with patch.dict(os.environ, {"HAYAGARDEN_RELAY_VAULT_KEY_FILE": str(self.key)}, clear=False):
            self._assert_open_fails_without_db()


if __name__ == "__main__":
    unittest.main()
