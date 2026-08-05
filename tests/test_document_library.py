"""Documents View union + delete contract."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCHEMA = '''
CREATE TABLE chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  author TEXT,
  content TEXT DEFAULT '',
  file_url TEXT DEFAULT '',
  file_name TEXT DEFAULT '',
  created_at TEXT,
  session_id TEXT DEFAULT '',
  tool_calls TEXT DEFAULT ''
);
'''


class DocumentLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'memories.db')
        self.store = str(Path(self.tmp.name) / 'artifacts')
        self.files = str(Path(self.tmp.name) / 'uploads')
        os.makedirs(self.store, exist_ok=True)
        os.makedirs(self.files, exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.close()

        self.patches = [
            mock.patch('artifact_store.DB_PATH', self.db),
            mock.patch('artifact_store.STORE_DIR', self.store),
        ]
        for p in self.patches:
            p.start()
        import artifact_store as as_
        as_._init_table()
        self.as_ = as_
        import importlib
        import chat.document_library as dl
        importlib.reload(dl)
        # Patch after reload so module-level DB_PATH sticks.
        self.dl_patches = [
            mock.patch.object(dl, 'DB_PATH', self.db),
            mock.patch.object(dl, 'FILES_DIR', self.files),
        ]
        for p in self.dl_patches:
            p.start()
        self.dl = dl

    def tearDown(self):
        for p in getattr(self, 'dl_patches', []):
            p.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _add_upload(self, message_id, name, created_at, author='hayana'):
        fname = 'aabbccdd_%s' % name
        path = Path(self.files) / fname
        path.write_text('hello', encoding='utf-8')
        url = '/static/uploads/files/%s' % fname
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO chat_messages (id, author, content, file_url, file_name, created_at, session_id) '
            'VALUES (?,?,?,?,?,?,?)',
            (message_id, author, 'x', url, name, created_at, 's1'),
        )
        conn.commit()
        conn.close()
        return url

    def test_l1_uploads_only(self):
        self._add_upload(10, 'a.txt', '2026-08-05 10:00:00')
        items = self.dl.list_documents()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['library_key'], 'upload:10')
        self.assertEqual(items[0]['source'], 'user_upload')
        self.assertIn('/api/chat/files/', items[0]['preview_url'])

    def test_l2_artifacts_only(self):
        meta = self.as_.save('html', 'Page', '<p>hi</p>')
        # Force created_at ordering via direct update if needed
        items = self.dl.list_documents()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['library_key'], 'artifact:%d' % meta['id'])
        self.assertEqual(items[0]['source'], 'assistant_artifact')
        self.assertEqual(items[0]['author'], 'fyodor')
        self.assertTrue(items[0]['preview_url'].endswith('/preview'))

    def test_l3_merged_time_order(self):
        self._add_upload(1, 'old.txt', '2026-08-05 09:00:00')
        meta = self.as_.save('markdown', 'New', '# n')
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE artifacts SET created_at=? WHERE id=?',
            ('2026-08-05 11:00:00', meta['id']),
        )
        conn.execute(
            'INSERT INTO chat_messages (id, author, content, file_url, file_name, created_at) '
            "VALUES (2,'hayana','x','/static/uploads/files/aabbccdd_mid.txt','mid.txt','2026-08-05 10:30:00')"
        )
        Path(self.files, 'aabbccdd_mid.txt').write_text('m', encoding='utf-8')
        conn.commit()
        conn.close()
        items = self.dl.list_documents()
        keys = [i['library_key'] for i in items]
        self.assertEqual(keys[0], 'artifact:%d' % meta['id'])
        self.assertEqual(keys[1], 'upload:2')
        self.assertEqual(keys[2], 'upload:1')

    def test_l4_numeric_id_collision_keys_differ(self):
        # Force artifact id == message id == 7
        self._add_upload(7, 'u.txt', '2026-08-05 12:00:00')
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO artifacts (id, type, title, filename, size, created_at) "
            "VALUES (7,'html','A','a_deadbeef0123.html',3,'2026-08-05 12:01:00')"
        )
        Path(self.store, 'a_deadbeef0123.html').write_text('hi', encoding='utf-8')
        conn.commit()
        conn.close()
        items = self.dl.list_documents()
        keys = {i['library_key'] for i in items}
        self.assertEqual(keys, {'upload:7', 'artifact:7'})

    def test_d1_upload_key_only(self):
        self._add_upload(3, 'u.txt', '2026-08-05 12:00:00')
        meta = self.as_.save('html', 'Keep', '<p>k</p>')
        out = self.dl.delete_documents(keys=['upload:3'], strict_keys=True)
        self.assertEqual(out['deleted_uploads'], 1)
        self.assertEqual(out['deleted_artifacts'], 0)
        self.assertIsNotNone(self.as_.get(meta['id']))
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT file_url FROM chat_messages WHERE id=3').fetchone()
        conn.close()
        self.assertEqual(row[0], '')

    def test_d2_artifact_key_only(self):
        self._add_upload(4, 'keep.txt', '2026-08-05 12:00:00')
        meta = self.as_.save('html', 'Gone', '<p>g</p>')
        out = self.dl.delete_documents(keys=['artifact:%d' % meta['id']], strict_keys=True)
        self.assertEqual(out['deleted_artifacts'], 1)
        self.assertEqual(out['deleted_uploads'], 0)
        self.assertIsNone(self.as_.get(meta['id']))
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT file_url FROM chat_messages WHERE id=4').fetchone()
        conn.close()
        self.assertTrue(row[0])

    def test_d3_same_numeric_no_cross_delete(self):
        self._add_upload(9, 'u.txt', '2026-08-05 12:00:00')
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO artifacts (id, type, title, filename, size, created_at) "
            "VALUES (9,'html','A','a_cafebabe0001.html',2,'2026-08-05 12:01:00')"
        )
        Path(self.store, 'a_cafebabe0001.html').write_text('x', encoding='utf-8')
        conn.commit()
        conn.close()
        out = self.dl.delete_documents(keys=['upload:9'], strict_keys=True)
        self.assertEqual(out['deleted_uploads'], 1)
        self.assertEqual(out['deleted_artifacts'], 0)
        self.assertIsNotNone(self.as_.get(9))

    def test_d4_legacy_ids_only_upload(self):
        self._add_upload(5, 'u.txt', '2026-08-05 12:00:00')
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO artifacts (id, type, title, filename, size, created_at) "
            "VALUES (5,'html','A','a_cafebabe0002.html',2,'2026-08-05 12:01:00')"
        )
        Path(self.store, 'a_cafebabe0002.html').write_text('x', encoding='utf-8')
        conn.commit()
        conn.close()
        out = self.dl.delete_documents(ids=[5], strict_keys=True)
        self.assertEqual(out['deleted_uploads'], 1)
        self.assertEqual(out['deleted_artifacts'], 0)
        self.assertIsNotNone(self.as_.get(5))

    def test_d5_illegal_key_strict(self):
        with self.assertRaises(ValueError):
            self.dl.delete_documents(keys=['nope:1'], strict_keys=True)

    def test_parse_library_key(self):
        self.assertEqual(self.dl.parse_library_key('upload:12'), ('user_upload', 12))
        self.assertEqual(self.dl.parse_library_key('artifact:3'), ('assistant_artifact', 3))
        self.assertIsNone(self.dl.parse_library_key('upload:-1'))
        self.assertIsNone(self.dl.parse_library_key('upload:0'))
        self.assertIsNone(self.dl.parse_library_key('file:1'))


if __name__ == '__main__':
    unittest.main()
