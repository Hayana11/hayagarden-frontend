"""Owner Canary isolation — structural only; no production paths / no live Claude."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.daily_context import DEFAULT_DB_PATH
from tools.context_window_admin import run_owner_canary, _assert_isolated_temp_root
from chat.context_window_fallback import FallbackError


class OwnerCanaryIsolationTests(unittest.TestCase):
    def test_C_structural_owner_canary_isolation_and_cleanup(self):
        """Path C: success recover + reject path; temp-only; cleanup removes root."""
        # Capture production JSONL/DB identity for non-touch proof (metadata only).
        prod_db = Path(DEFAULT_DB_PATH).resolve()
        prod_db_meta = None
        if prod_db.exists():
            st = prod_db.stat()
            prod_db_meta = (st.st_mtime_ns, st.st_size, st.st_ino)

        report = run_owner_canary(confirm_live=True, structural_only=True)
        self.assertTrue(report.get('ok'), report)
        self.assertTrue(report['success_path']['ok'])
        self.assertTrue(report['reject_path']['ok'])
        self.assertFalse(report['success_path']['carryover_contains_failed_user'])
        self.assertEqual(report['success_path']['failed_user_assistant_count'], 0)
        self.assertTrue(report['cleanup']['ok'])
        self.assertFalse(Path(report['temp_root']).exists())

        # Flag untouched.
        self.assertNotIn('DAILY_SOFT_WINDOW_ENABLED', os.environ)

        # Production DB metadata unchanged when the file exists.
        if prod_db_meta is not None and prod_db.exists():
            st2 = prod_db.stat()
            self.assertEqual(
                (st2.st_mtime_ns, st2.st_size, st2.st_ino), prod_db_meta,
            )

        # Canary db path must not equal production.
        self.assertNotEqual(
            Path(report['paths']['db']).resolve(), prod_db,
        )

    def test_isolation_rejects_repo_temp_root(self):
        inside = Path(tempfile.mkdtemp(prefix='bad-', dir=str(ROOT)))
        try:
            with self.assertRaises(FallbackError) as ar:
                _assert_isolated_temp_root(inside)
            self.assertEqual(ar.exception.error_code, 'ISOLATION_ESCAPE')
        finally:
            import shutil
            shutil.rmtree(inside, ignore_errors=True)

    def test_confirm_live_required_without_structural(self):
        report = run_owner_canary(confirm_live=False, structural_only=False)
        self.assertFalse(report.get('ok'))
        self.assertEqual(report.get('error_code'), 'CONFIRM_LIVE_REQUIRED')


if __name__ == '__main__':
    unittest.main()
