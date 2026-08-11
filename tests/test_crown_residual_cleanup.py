"""Crown residual cleanup after Stage D merge — minimal acceptance guards.

Does not grant STATE AUTHORITY COMPLETE; that remains a human crown review.
"""

from __future__ import annotations

import ast
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class CrownChatZeroInjectionTests(unittest.TestCase):
    def test_build_system_source_has_no_legacy_bp3_state_injection(self):
        src = Path(ROOT, 'chat/system_builder.py').read_text(encoding='utf-8')
        # Function body of build_system must not call legacy state horns.
        tree = ast.parse(src)
        build = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'build_system':
                build = node
                break
        self.assertIsNotNone(build)
        body = ast.get_source_segment(src, build) or ''
        # Call sites only — comments may name the retired APIs.
        self.assertNotIn('.get_bp3_snippet(', body)
        self.assertNotIn('.get_longing_system_hint(', body)
        self.assertNotIn('emotion_engine', body)
        self.assertNotIn('drive_engine', body)
        self.assertNotIn('import desire', body)

    def test_cc_collect_state_leaves_emotion_drive_empty(self):
        from chat.system_builder import _cc_collect_state

        def get_db():
            raise AssertionError('db not needed for empty emotion/drive')

        with mock.patch('urllib.request.urlopen', side_effect=OSError('no light')), \
             mock.patch('config_store.get_bool', return_value=True), \
             mock.patch('chat.system_builder._format_structured_emotion_snippet') as emo, \
             mock.patch('chat.system_builder._format_structured_drive_snippet') as drv:
            emo.return_value = 'MUST_NOT_APPEAR'
            drv.return_value = 'MUST_NOT_APPEAR'
            lean = _cc_collect_state(get_db, lean=True)
            full = _cc_collect_state(get_db, lean=False)
        self.assertEqual(lean['emotion'], '')
        self.assertEqual(lean['drive'], '')
        self.assertEqual(full['emotion'], '')
        self.assertEqual(full['drive'], '')
        emo.assert_not_called()
        drv.assert_not_called()
        self.assertNotIn('主动找话题', full['drive'])
        self.assertNotIn('话少一些', full['drive'])


class CrownImportNoWriteTests(unittest.TestCase):
    def test_legacy_modules_do_not_ensure_table_on_import(self):
        for mod_name in ('drive_engine', 'desire', 'emotion_engine'):
            src = Path(ROOT, f'{mod_name}.py').read_text(encoding='utf-8')
            tree = ast.parse(src)
            # Top-level Call to ensure_table() must be absent.
            for node in tree.body:
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == 'ensure_table'
                ):
                    self.fail(f'{mod_name}.py still calls ensure_table() at import')

    def test_import_does_not_create_legacy_tables(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'empty.db')
        # Fresh empty file — no tables.
        sqlite3.connect(db_path).close()
        env = {
            'MEMORIES_DB': db_path,
            'HAYAGARDEN_CONFIG_DB_PATH': str(
                Path(tmp.name) / 'cfg.db'
            ),
        }
        with mock.patch.dict(os.environ, env, clear=False):
            for name in ('drive_engine', 'desire', 'emotion_engine'):
                if name in sys.modules:
                    del sys.modules[name]
            import drive_engine as de
            import desire as des
            import emotion_engine as ee
            de.DB_PATH = db_path
            des.DB_PATH = db_path
            ee.DB_PATH = db_path
            # Re-import after path patch to exercise module body again.
            importlib.reload(de)
            importlib.reload(des)
            importlib.reload(ee)
        conn = sqlite3.connect(db_path)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        conn.close()
        self.assertNotIn('drive_state', tables)
        self.assertNotIn('desire_state', tables)
        self.assertNotIn('emotion_state', tables)


class CrownStandaloneSettleHelperTests(unittest.TestCase):
    def test_best_effort_has_no_production_caller(self):
        prod_files = [
            Path(ROOT, 'gateway.py'),
            Path(ROOT, 'wake/executor.py'),
            Path(ROOT, 'wake/builder.py'),
            Path(ROOT, 'app.py'),
        ]
        for path in prod_files:
            text = path.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('apply_wake_outcome_best_effort', text)
            # observation helper also must not be production Wake path
            if path.name in ('gateway.py', 'wake/executor.py', 'wake/builder.py'):
                if path.name == 'wake/executor.py':
                    self.assertIn('apply_wake_outcome_on_conn', text)
                self.assertNotIn('apply_wake_outcome_best_effort', text)

    def test_best_effort_doc_marks_non_production(self):
        src = Path(ROOT, 'chat/drive_authority.py').read_text(encoding='utf-8')
        self.assertIn('Test / recovery helper only', src)
        self.assertIn('never wire into production gateway', src)


if __name__ == '__main__':
    unittest.main()
