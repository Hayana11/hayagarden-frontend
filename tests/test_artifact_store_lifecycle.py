"""Artifact store list/delete lifecycle helpers."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ArtifactStoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'memories.db')
        self.store = str(Path(self.tmp.name) / 'artifacts')
        os.makedirs(self.store, exist_ok=True)
        self.patches = [
            mock.patch('artifact_store.DB_PATH', self.db),
            mock.patch('artifact_store.STORE_DIR', self.store),
        ]
        for p in self.patches:
            p.start()
        import artifact_store as as_
        as_._init_table()
        self.as_ = as_

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_list_recent_order(self):
        a = self.as_.save('html', 'A', '<p>a</p>')
        b = self.as_.save('markdown', 'B', '# b')
        rows = self.as_.list_recent(10)
        self.assertEqual([r['id'] for r in rows], [b['id'], a['id']])

    def test_delete_removes_file_and_row(self):
        meta = self.as_.save('html', 'X', '<p>x</p>')
        path = Path(self.store) / self.as_.get(meta['id'])['filename']
        self.assertTrue(path.is_file())
        status = self.as_.delete(meta['id'])
        self.assertEqual(status, 'deleted')
        self.assertFalse(path.exists())
        self.assertIsNone(self.as_.get(meta['id']))

    def test_delete_stale_metadata_when_file_missing(self):
        meta = self.as_.save('html', 'Y', '<p>y</p>')
        path = Path(self.store) / self.as_.get(meta['id'])['filename']
        path.unlink()
        status = self.as_.delete(meta['id'], allow_stale_metadata=True)
        self.assertEqual(status, 'stale_cleared')
        self.assertIsNone(self.as_.get(meta['id']))

    def test_delete_rejects_path_escape_filename(self):
        # Plant a row with illegal filename; resolver must refuse and not touch outside.
        import sqlite3
        outside = Path(self.tmp.name) / 'outside.txt'
        outside.write_text('secret', encoding='utf-8')
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO artifacts (type, title, filename, size) VALUES (?,?,?,?)",
            ('html', 'evil', '../outside.txt', 1),
        )
        conn.commit()
        aid = conn.execute('SELECT id FROM artifacts WHERE title=?', ('evil',)).fetchone()[0]
        conn.close()
        status = self.as_.delete(aid, allow_stale_metadata=True)
        self.assertEqual(status, 'stale_cleared')
        self.assertTrue(outside.is_file())
        self.assertEqual(outside.read_text(encoding='utf-8'), 'secret')

    def test_delete_not_found(self):
        self.assertEqual(self.as_.delete(99999), 'not_found')


if __name__ == '__main__':
    unittest.main()
