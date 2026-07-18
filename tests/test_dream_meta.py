import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools.dream_meta import (
    build_dream_api_item,
    fetch_dream_items,
    resolve_dream_fields,
    sanitize_dream_content,
)


class DreamMetaTests(unittest.TestCase):
    def test_sanitize_strips_nbsp_entities(self):
        cleaned = sanitize_dream_content('第一段。\n\n&nbsp;\n\n第二段。')
        self.assertNotIn('&nbsp;', cleaned)
        self.assertIn('第一段。', cleaned)
        self.assertIn('第二段。', cleaned)

    def test_resolve_from_post_tags_and_va(self):
        row = {
            'summary_title': '雨夜图书馆',
            'content': '雨落在书页上。',
            'tags': 'warm',
            'valence': 0.72,
            'arousal': 0.28,
        }
        meta = resolve_dream_fields(row)
        self.assertEqual(meta['title'], '雨夜图书馆')
        self.assertEqual(meta['tone'], 'warm')
        self.assertEqual(meta['emotion'], '温柔')
        self.assertAlmostEqual(meta['valence'], 0.44, places=2)
        self.assertAlmostEqual(meta['arousal'], 0.28, places=2)

    def test_resolve_falls_back_to_pool(self):
        row = {
            'summary_title': '',
            'content': '漂浮在温热介质里。',
            'tags': '情绪,日常',
            'valence': 0.0,
            'arousal': 0.0,
        }
        pool = {'valence': 0.55, 'arousal': 0.35, 'tone': 'drifting'}
        meta = resolve_dream_fields(row, pool)
        self.assertEqual(meta['tone'], 'drifting')
        self.assertEqual(meta['emotion'], '漂浮')

    def test_fetch_dream_items_joins_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'memories.db'
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author TEXT DEFAULT 'fyodor',
                    created_at TEXT,
                    valence REAL DEFAULT 0,
                    arousal REAL DEFAULT 0,
                    tags TEXT DEFAULT '',
                    summary_title TEXT
                );
                CREATE TABLE dream_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT,
                    valence REAL,
                    arousal REAL,
                    tone TEXT,
                    created_at TEXT
                );
                INSERT INTO posts (type, content, created_at, summary_title)
                VALUES ('DREAM', '海潮推着棋子。', '2026-07-17 03:10:00', '夜海棋局');
                INSERT INTO dream_pool (content, valence, arousal, tone, created_at)
                VALUES ('海潮推着棋子。', 0.62, 0.71, 'vivid', '2026-07-17 03:10:00');
                """
            )
            items = fetch_dream_items(conn, limit=5)
            conn.close()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], '夜海棋局')
        self.assertEqual(items[0]['tone'], 'vivid')
        self.assertEqual(items[0]['emotion'], '鲜活')

    def test_fetch_dream_page_cursor(self):
        from tools.dream_meta import fetch_dream_page
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / 'memories.db'
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conn.executescript(
                """
                CREATE TABLE posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author TEXT DEFAULT 'fyodor',
                    created_at TEXT,
                    valence REAL DEFAULT 0,
                    arousal REAL DEFAULT 0,
                    tags TEXT DEFAULT '',
                    summary_title TEXT
                );
                CREATE TABLE dream_pool (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT, valence REAL, arousal REAL, tone TEXT, created_at TEXT
                );
                """
            )
            for i in range(5):
                conn.execute(
                    "INSERT INTO posts (type, content, created_at, summary_title) VALUES ('DREAM', ?, ?, ?)",
                    (f'梦境正文{i}足够长', f'2026-07-1{i} 03:00:00', f'题{i}'),
                )
            conn.commit()
            page1 = fetch_dream_page(conn, limit=2, before=None)
            self.assertEqual(len(page1['items']), 2)
            self.assertTrue(page1['has_more'])
            self.assertIsNotNone(page1['next_before'])
            page2 = fetch_dream_page(conn, limit=2, before=page1['next_before'])
            self.assertEqual(len(page2['items']), 2)
            ids1 = {it['id'] for it in page1['items']}
            ids2 = {it['id'] for it in page2['items']}
            self.assertFalse(ids1 & ids2)
            page3 = fetch_dream_page(conn, limit=2, before=page2['next_before'])
            self.assertEqual(len(page3['items']), 1)
            self.assertFalse(page3['has_more'])
            conn.close()

    def test_build_item_uses_rule_title_when_missing(self):
        item = build_dream_api_item({
            'id': 9,
            'author': 'fyodor',
            'content': '「旧木屋」里只有雨声。',
            'created_at': '2026-07-17 04:00:00',
            'summary_title': '',
            'tags': '',
            'valence': 0,
            'arousal': 0,
        })
        self.assertEqual(item['title'], '旧木屋')


if __name__ == '__main__':
    unittest.main()
