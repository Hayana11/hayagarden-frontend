"""Nexus path guard tests (M1 / M5.9 / M5.10)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from nexus_paths import (  # noqa: E402
    DEFAULT_NEXUS_WORKSPACE_ROOT,
    NexusPathError,
    resolve_nexus_workspace,
    resolve_under_nexus,
)


class NexusPathTests(unittest.TestCase):
    def test_default_constant(self):
        self.assertEqual(DEFAULT_NEXUS_WORKSPACE_ROOT, "/opt/workspace/projects/nexus/current")

    def test_import_does_not_create_production_dir(self):
        # Module import already happened; ensure resolve of missing root fails closed
        # without creating the production path as a side effect of import.
        prod = Path(DEFAULT_NEXUS_WORKSPACE_ROOT)
        existed = prod.exists()
        # Calling resolve on a missing override must fail closed.
        missing = Path(tempfile.mkdtemp()) / "no-such-nexus-root"
        with self.assertRaises(NexusPathError) as ctx:
            resolve_nexus_workspace(override=str(missing))
        self.assertEqual(ctx.exception.code, "workspace_root_missing")
        self.assertFalse(missing.exists())
        self.assertEqual(prod.exists(), existed)

    def test_reject_relative_and_frontend_fallback(self):
        with self.assertRaises(NexusPathError):
            resolve_nexus_workspace(override="relative/path")
        with self.assertRaises(NexusPathError) as ctx:
            resolve_nexus_workspace(override="/opt/frontend")
        self.assertEqual(ctx.exception.code, "workspace_root_forbidden")

    def test_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(NexusPathError) as ctx:
                resolve_under_nexus(root, "../etc/passwd")
            self.assertEqual(ctx.exception.code, "path_traversal_rejected")
            with self.assertRaises(NexusPathError):
                resolve_under_nexus(root, "/etc/passwd")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nexus"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            secret = outside / "secret.txt"
            secret.write_text("nope", encoding="utf-8")
            link = root / "escape"
            link.symlink_to(outside)
            with self.assertRaises(NexusPathError) as ctx:
                resolve_under_nexus(root, "escape/secret.txt")
            self.assertEqual(ctx.exception.code, "path_outside_nexus")

    def test_valid_temp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = resolve_nexus_workspace(override=tmp)
            self.assertTrue(root.is_dir())
            child = resolve_under_nexus(root, "a/b.txt")
            self.assertTrue(str(child).startswith(str(root)))


if __name__ == "__main__":
    unittest.main()
