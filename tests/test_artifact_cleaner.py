"""Artifact auto lifecycle — referenced never auto-deleted."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
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
  tool_calls TEXT DEFAULT ''
);
'''


def _relay_shape(aid, atype='html', title='T', size=10):
    """Exact api_relay persisted tool_calls element (lifted artifact)."""
    art = {'id': aid, 'type': atype, 'title': title, 'size': size}
    return {
        'name': 'create_%s' % ('document' if atype == 'docx' else atype),
        'args': {'title': title, 'content': '...'},
        'result': json.dumps({'artifact': art}, ensure_ascii=False),
        'success': True,
        'artifact': art,
    }


def _cc_shape(aid, atype='html', title='T', size=10):
    """CC-style: result JSON only, no top-level artifact lift."""
    art = {'id': aid, 'type': atype, 'title': title, 'size': size}
    return {
        'name': 'create_html' if atype == 'html' else 'create_markdown',
        'args': {'title': title, 'content': '...'},
        'result': json.dumps({'artifact': art}, ensure_ascii=False),
        'success': True,
    }


class ArtifactCleanerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'memories.db')
        self.store = str(Path(self.tmp.name) / 'artifacts')
        os.makedirs(self.store, exist_ok=True)
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.close()
        self.patches = [
            mock.patch('artifact_store.DB_PATH', self.db),
            mock.patch('artifact_store.STORE_DIR', self.store),
            mock.patch('tools.artifact_cleaner.DB_PATH', self.db),
            mock.patch('tools.artifact_cleaner.STORE_DIR', self.store),
            mock.patch('config_store.get_int', side_effect=self._cfg),
        ]
        self.cfg = {
            'ARTIFACT_CLEAN_ORPHAN_GRACE_MIN': 0,
            'ARTIFACT_CLEAN_CAP_MB': 500,
        }
        for p in self.patches:
            p.start()
        import artifact_store as as_
        as_._init_table()
        self.as_ = as_
        import importlib
        import tools.artifact_cleaner as ac
        importlib.reload(ac)
        self.ac = ac

    def _cfg(self, key, default=0):
        return int(self.cfg.get(key, default))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _msg(self, tool_calls):
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO chat_messages (author, content, tool_calls, created_at) VALUES (?,?,?,?)',
            ('fyodor', 'ok', json.dumps(tool_calls, ensure_ascii=False), '2026-08-05 10:00:00'),
        )
        conn.commit()
        conn.close()

    def _age_file(self, aid, seconds_ago=3600):
        meta = self.as_.get(aid)
        path = Path(self.store) / meta['filename']
        old = time.time() - seconds_ago
        os.utime(path, (old, old))

    def test_extract_relay_and_cc_shapes(self):
        ids, ok = self.ac.extract_artifact_ids_from_tool_calls([_relay_shape(7)])
        self.assertTrue(ok)
        self.assertEqual(ids, {7})
        ids, ok = self.ac.extract_artifact_ids_from_tool_calls([_cc_shape(8)])
        self.assertTrue(ok)
        self.assertEqual(ids, {8})

    def test_c1_referenced_never_auto_deleted(self):
        meta = self.as_.save('html', 'Keep', '<p>k</p>')
        self._age_file(meta['id'])
        self._msg([_relay_shape(meta['id'])])
        self.cfg['ARTIFACT_CLEAN_ORPHAN_GRACE_MIN'] = 0
        conn = self.ac._db()
        try:
            refs, certain = self.ac.scan_referenced_artifact_ids(conn)
            self.assertTrue(certain)
            self.assertEqual(refs, {meta['id']})
            n = self.ac.clean_unreferenced(conn, refs, 0, scan_certain=True)
            self.assertEqual(n, 0)
        finally:
            conn.close()
        self.assertIsNotNone(self.as_.get(meta['id']))

    def test_c2_unreferenced_within_grace_kept(self):
        meta = self.as_.save('html', 'New', '<p>n</p>')
        # mtime is now; grace 60 min
        conn = self.ac._db()
        try:
            n = self.ac.clean_unreferenced(conn, set(), 60, scan_certain=True)
            self.assertEqual(n, 0)
        finally:
            conn.close()
        self.assertIsNotNone(self.as_.get(meta['id']))

    def test_c3_unreferenced_past_grace_deleted(self):
        meta = self.as_.save('markdown', 'Old', '# o')
        self._age_file(meta['id'], 7200)
        conn = self.ac._db()
        try:
            n = self.ac.clean_unreferenced(conn, set(), 60, scan_certain=True)
            self.assertEqual(n, 1)
        finally:
            conn.close()
        self.assertIsNone(self.as_.get(meta['id']))

    def test_c4_physical_orphan_past_grace(self):
        orphan = Path(self.store) / 'a_orphanorphan1.html'
        orphan.write_text('x', encoding='utf-8')
        old = time.time() - 7200
        os.utime(orphan, (old, old))
        n = self.ac.clean_physical_orphans(60)
        self.assertEqual(n, 1)
        self.assertFalse(orphan.exists())

    def test_c5_broken_row_cleared(self):
        meta = self.as_.save('html', 'Broken', '<p>b</p>')
        path = Path(self.store) / self.as_.get(meta['id'])['filename']
        path.unlink()
        conn = self.ac._db()
        try:
            n = self.ac.clean_broken_rows(conn)
            self.assertEqual(n, 1)
        finally:
            conn.close()
        self.assertIsNone(self.as_.get(meta['id']))

    def test_c6_cap_only_deletes_unreferenced(self):
        keep = self.as_.save('html', 'Keep', 'k' * 1000)
        drop = self.as_.save('html', 'Drop', 'd' * 1000)
        self._msg([_relay_shape(keep['id'])])
        # Cap tiny so both would exceed
        self.cfg['ARTIFACT_CLEAN_CAP_MB'] = 0  # disabled when <=0
        # Use fractional MB via direct call with tiny cap: 1 byte effectively
        conn = self.ac._db()
        try:
            refs, certain = self.ac.scan_referenced_artifact_ids(conn)
            removed, over = self.ac.enforce_cap(
                conn, refs, cap_mb=1, scan_certain=certain,
            )
            # 1MB cap — with small files may not remove; force tiny by patching sizes
        finally:
            conn.close()
        # Re-run with absurdly small cap using byte trick: write large unreferenced
        big = self.as_.save('html', 'Big', 'x' * (2 * 1024 * 1024))
        self._age_file(big['id'])
        conn = self.ac._db()
        try:
            refs, certain = self.ac.scan_referenced_artifact_ids(conn)
            removed, over = self.ac.enforce_cap(
                conn, refs, cap_mb=1, scan_certain=True,
            )
            self.assertGreaterEqual(removed, 1)
            self.assertIsNotNone(self.as_.get(keep['id']))
        finally:
            conn.close()

    def test_c7_only_referenced_over_cap_zero_deletes(self):
        keep = self.as_.save('html', 'KeepBig', 'y' * (2 * 1024 * 1024))
        self._msg([_cc_shape(keep['id'])])
        conn = self.ac._db()
        try:
            refs, certain = self.ac.scan_referenced_artifact_ids(conn)
            removed, over = self.ac.enforce_cap(
                conn, refs, cap_mb=1, scan_certain=True,
            )
            self.assertEqual(removed, 0)
            self.assertTrue(over)
        finally:
            conn.close()
        self.assertIsNotNone(self.as_.get(keep['id']))

    def test_c8_malformed_tool_calls_blocks_unreferenced_delete(self):
        victim = self.as_.save('html', 'Victim', '<p>v</p>')
        self._age_file(victim['id'], 7200)
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO chat_messages (author, content, tool_calls) VALUES (?,?,?)',
            ('fyodor', 'x', '{not-json'),
        )
        conn.commit()
        conn.close()
        conn = self.ac._db()
        try:
            refs, certain = self.ac.scan_referenced_artifact_ids(conn)
            self.assertFalse(certain)
            n = self.ac.clean_unreferenced(conn, refs, 0, scan_certain=certain)
            self.assertEqual(n, 0)
        finally:
            conn.close()
        self.assertIsNotNone(self.as_.get(victim['id']))


class FilesHtmlContractTests(unittest.TestCase):
    def test_ui_uses_library_key_and_safe_preview(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / 'static' / 'files.html').read_text(encoding='utf-8')
        self.assertIn('library_key', html)
        self.assertIn("JSON.stringify({keys:keys})", html)
        self.assertIn('删除 AI 生成文件后，历史聊天里的对应 Artifact 卡片可能无法再次打开', html)
        self.assertIn('preview_url', html)
        self.assertIn('download_url', html)
        self.assertNotIn('mdRender', html)
        self.assertNotIn('fetch(f.file_url)', html)

    def test_p1_p5_outer_iframe_no_sandbox_double_layer(self):
        """Outer files.html iframe must remain trusted; shell owns sandbox."""
        root = Path(__file__).resolve().parents[1]
        html = (root / 'static' / 'files.html').read_text(encoding='utf-8')
        # P1/P2/P3: openItem must not set sandbox on the outer iframe.
        self.assertNotIn("setAttribute('sandbox'", html)
        self.assertNotIn('setAttribute("sandbox"', html)
        # P5: no local markdown renderer / raw HTML fetch path.
        self.assertNotIn('mdRender', html)
        self.assertNotIn('fetch(f.file_url)', html)
        self.assertNotIn('srcdoc', html)

        from chat.attachment_contract import sandbox_preview_shell
        shell = sandbox_preview_shell('/api/artifacts/1/content')
        # P4: inner shell sandbox is allow-scripts only (no allow-same-origin).
        self.assertIn('sandbox="allow-scripts"', shell)
        self.assertNotIn('allow-same-origin', shell)
        self.assertEqual(shell.count('sandbox='), 1)


if __name__ == '__main__':
    unittest.main()
