"""Tests for read-only generate_day_handoff script."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCRIPT_PATH = os.path.join(ROOT, 'scripts', 'generate_day_handoff.py')


def _load_script_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location('generate_day_handoff', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GenerateDayHandoffSourceTests(unittest.TestCase):
    def test_script_does_not_import_gateway(self):
        with open(SCRIPT_PATH, encoding='utf-8') as fh:
            source = fh.read()
        self.assertNotIn('from gateway import', source)
        self.assertNotIn('import gateway', source)
        self.assertNotIn('import app', source)
        self.assertNotIn('import wake', source)
        self.assertNotIn('ombre_adapter', source)


class GenerateDayHandoffDbTests(unittest.TestCase):
    def setUp(self):
        self._mod = _load_script_module()

    def test_open_readonly_db_uses_ro_uri(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                'CREATE TABLE chat_messages (id INTEGER, author TEXT, content TEXT, created_at TEXT)'
            )
            conn.commit()
            conn.close()

            with mock.patch('sqlite3.connect', wraps=sqlite3.connect) as connect_mock:
                ro_conn = self._mod.open_readonly_db(db_path)
                ro_conn.close()
                connect_mock.assert_called_once()
                uri, kwargs = connect_mock.call_args[0][0], connect_mock.call_args[1]
                self.assertTrue(kwargs.get('uri'))
                self.assertIn('mode=ro', uri)
                self.assertTrue(uri.startswith('file:'))
        finally:
            os.unlink(db_path)

    def test_missing_database_fails_without_creating_file(self):
        missing = os.path.join(tempfile.gettempdir(), 'missing-handoff-%d.db' % os.getpid())
        self.assertFalse(os.path.exists(missing))
        with self.assertRaises(FileNotFoundError):
            self._mod.open_readonly_db(missing)
        self.assertFalse(os.path.exists(missing))

    def test_generate_yaml_from_temp_readonly_database(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
            db_path = tmp.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                'CREATE TABLE chat_messages (id INTEGER, author TEXT, content TEXT, created_at TEXT)'
            )
            conn.execute(
                'INSERT INTO chat_messages VALUES (?, ?, ?, ?)',
                (1, 'hayana', '不要给我列建议，只要陪我。', '2026-07-26 10:00:00'),
            )
            conn.commit()
            conn.close()

            with mock.patch('sys.stdout', new_callable=StringIO) as stdout:
                with mock.patch('sys.stderr', new_callable=StringIO):
                    with mock.patch.object(self._mod, 'write_day_handoff_to_tmp', return_value='/tmp/skip'):
                        rc = self._mod.main(['--db', db_path, '--day', '2026-07-26'])
            self.assertEqual(rc, 0)
            out = stdout.getvalue()
            self.assertIn('requires_human_review: true', out)
            self.assertIn('extraction_mode: "conservative_rules"', out)
            self.assertIn('source_day: "2026-07-26"', out)
        finally:
            os.unlink(db_path)


if __name__ == '__main__':
    unittest.main()
