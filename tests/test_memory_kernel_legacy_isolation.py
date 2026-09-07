"""Run existing memory contracts with synthetic data and no production access."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Apply before importing application modules. Record denials so that catching
# a blocked operation cannot hide an attempted production access.
_RUNNER = r"""
import builtins
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

sandbox = Path(sys.argv[2]).resolve()
violations = []

# Exact legacy deployment paths map to synthetic fixtures before the audit
# hook. No production file is opened, and provider decisions are not mocked.
real_open = builtins.open
real_connect = sqlite3.connect

def fixture_open(file, *args, **kwargs):
    if isinstance(file, (str, os.PathLike)) and os.fspath(file) == '/opt/frontend/.env':
        file = sandbox / 'fixture.env'
    return real_open(file, *args, **kwargs)

def fixture_connect(database, *args, **kwargs):
    if os.fspath(database) == '/opt/frontend/memories.db':
        database = sandbox / 'config.db'
    elif os.fspath(database) == '/var/lib/hayagarden/external-mcp.db':
        database = sandbox / 'external-mcp.db'
    return real_connect(database, *args, **kwargs)

builtins.open = fixture_open
sqlite3.connect = fixture_connect

def audit(event, args):
    blocked = False
    if event == 'sqlite3.connect':
        database = os.fsdecode(args[0])
        if database.startswith('file:'):
            database = unquote(urlsplit(database).path)
        blocked = database != ':memory:' and not Path(database).resolve().is_relative_to(sandbox)
    elif event in ('socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):
        blocked = True
    elif event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        flags = args[2] or 0
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        blocked = (
            str(path).startswith(('/opt/frontend/', '/opt/ombre/', '/root/'))
            or (writing and not path.is_relative_to(sandbox))
        )
    if blocked:
        violations.append(event + (':' + database if event == 'sqlite3.connect' else ''))
        raise PermissionError('CORE-0 isolation blocked ' + event)

sys.addaudithook(audit)
suite = unittest.defaultTestLoader.loadTestsFromName(sys.argv[1])
result = unittest.TextTestRunner(verbosity=1).run(suite)
print('ISOLATION', sys.argv[1], 'tests=', result.testsRun, 'blocked_access=', violations)
raise SystemExit(0 if result.wasSuccessful() and not violations else 1)
"""


class LegacyIsolationTests(unittest.TestCase):
    def test_case_10_existing_memory_regressions_in_isolated_processes(self):
        for module in (
            "tests.test_memory_library",
            "tests.test_memory_write_bridge",
            "tests.test_memory_unification_audit",
            "tests.test_m3_04c_daily_memory_cutover",
        ):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                env_path = root / "fixture.env"
                env_path.write_text("", encoding="utf-8")
                env = os.environ.copy()
                config_path = root / "config.db"
                with sqlite3.connect(config_path) as config:
                    # Mirror the existing runtime_config contract without any
                    # capability override. Missing key means INHERIT naturally.
                    config.execute(
                        """CREATE TABLE runtime_config (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL,
                            updated_at TEXT
                        )"""
                    )
                env.update(
                    TMPDIR=folder, TEMP=folder, TMP=folder,
                    HAYAGARDEN_CONFIG_DB_PATH=str(config_path),
                    HAYAGARDEN_ENV_PATH=str(env_path),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                result = subprocess.run(
                    [sys.executable, "-B", "-c", _RUNNER, module, folder],
                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=45,
                )
                print(result.stdout, end="")
                print(result.stderr, end="")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_synthetic_runtime_config_defaults_to_inherit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config_path = root / "config.db"
            with sqlite3.connect(config_path) as config:
                config.execute(
                    "CREATE TABLE runtime_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT)"
                )
            env = os.environ.copy()
            env.update(
                HAYAGARDEN_CONFIG_DB_PATH=str(config_path),
                HAYAGARDEN_ENV_PATH=str(root / "fixture.env"),
            )
            result = subprocess.run(
                [
                    sys.executable, "-B", "-c",
                    "from tools.capability_state import read_capability_state; "
                    "print(read_capability_state('memory.write'))",
                ],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "INHERIT")

    def test_kernel_has_no_production_importers(self):
        paths = list(ROOT.glob("*.py"))
        for directory in ("tools", "chat", "wake"):
            paths.extend((ROOT / directory).rglob("*.py"))
        for path in paths:
            if path.name in ("memory_kernel.py", "memory_kernel_schema.py"):
                continue
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("memory_kernel", path.read_text(encoding="utf-8"))

    def test_import_and_constructor_do_not_connect_or_load_providers(self):
        source = """
import sqlite3
import sys
from unittest.mock import patch
with patch.object(sqlite3, 'connect', side_effect=AssertionError('implicit DB access')):
    from tools.memory_kernel import MemoryKernel
    MemoryKernel('unused-fixture.db')
assert not any(name in sys.modules for name in (
    'app', 'gateway', 'config_store', 'tools.ombre_adapter',
    'tools.memory_internal_adapter', 'tools.memory_write_adapter',
))
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", source], cwd=ROOT,
            text=True, capture_output=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
