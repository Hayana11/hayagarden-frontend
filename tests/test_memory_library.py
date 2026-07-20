"""记忆库不应包含念头/梦境（这两种只走朋友圈）。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools import memory_library


def _make_posts_db(rows):
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = handle.name
    handle.close()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            content TEXT,
            author TEXT,
            created_at TEXT,
            pinned INTEGER DEFAULT 0,
            tags TEXT,
            layer TEXT,
            importance INTEGER DEFAULT 0,
            resolved INTEGER DEFAULT 0,
            summary_title TEXT
        );
        """
    )
    for row in rows:
        conn.execute(
            """INSERT INTO posts
               (type, content, author, created_at, tags, layer, importance, summary_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
    conn.commit()
    return path, conn


class MemoryLibraryExclusionTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "conn", None):
            self.conn.close()
        if getattr(self, "db_path", None):
            Path(self.db_path).unlink(missing_ok=True)

    def test_library_excludes_thought_and_dream(self):
        self.db_path, self.conn = _make_posts_db([
            ("MEMORY", "今天一起吃了面", "haya", "2026-07-18 10:00:00", "日常", "recent", 3, "吃面"),
            ("THOUGHT", "忽然想去海边", "fyodor", "2026-07-18 11:00:00", "", "recent", 1, "海边"),
            ("DREAM", "棋子漂在潮水上", "fyodor", "2026-07-18 03:10:00", "夜海", "recent", 1, "夜海棋局"),
            ("DIARY", "日记一条", "haya", "2026-07-17 22:00:00", "", "recent", 2, "日记"),
            ("FACT", "她不吃香菜", "fyodor", "2026-07-16 12:00:00", "fact", "long-term", 5, "香菜"),
        ])
        lib = memory_library.build_memory_library(self.conn)
        types_by_title = {e["summaryTitle"] or e["title"]: e for e in lib["entries"]}
        self.assertIn("吃面", types_by_title)
        self.assertIn("日记", types_by_title)
        self.assertIn("香菜", types_by_title)
        self.assertNotIn("海边", types_by_title)
        self.assertNotIn("夜海棋局", types_by_title)
        topic_names = {t["name"] for t in lib["topics"]}
        self.assertNotIn("梦境", topic_names)
        self.assertNotIn("想法", topic_names)

    def test_library_types_constant(self):
        self.assertNotIn("THOUGHT", memory_library.LIBRARY_TYPES)
        self.assertNotIn("DREAM", memory_library.LIBRARY_TYPES)
        self.assertIn("MEMORY", memory_library.LIBRARY_TYPES)
        self.assertIn("DIARY", memory_library.LIBRARY_TYPES)
