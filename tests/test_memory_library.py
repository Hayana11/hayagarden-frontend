"""记忆库 index/detail/search contract tests."""
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
               (type, content, author, created_at, tags, layer, importance, resolved, summary_title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ("MEMORY", "今天一起吃了面", "haya", "2026-07-18 10:00:00", "日常", "recent", 3, 0, "吃面"),
            ("THOUGHT", "忽然想去海边", "fyodor", "2026-07-18 11:00:00", "", "recent", 1, 0, "海边"),
            ("DREAM", "棋子漂在潮水上", "fyodor", "2026-07-18 03:10:00", "夜海", "recent", 1, 0, "夜海棋局"),
            ("DIARY", "日记一条", "haya", "2026-07-17 22:00:00", "", "recent", 2, 0, "日记"),
            ("FACT", "她不吃香菜", "fyodor", "2026-07-16 12:00:00", "fact", "long-term", 5, 0, "香菜"),
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


class MemoryLibraryIndexContractTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "conn", None):
            self.conn.close()
        if getattr(self, "db_path", None):
            Path(self.db_path).unlink(missing_ok=True)

    def setUp(self):
        long_tail = "深" * 1200
        self.db_path, self.conn = _make_posts_db([
            (
                "MEMORY",
                f"标题行\n{long_tail}",
                "haya",
                "2026-07-20 09:00:00",
                "日常,assoc:alpha|beta",
                "recent",
                3,
                0,
                "长尾记忆",
            ),
            (
                "MEMORY",
                "另一条",
                "fyodor",
                "2026-07-19 08:00:00",
                "技术,assoc:beta|gamma",
                "recent",
                2,
                0,
                "技术笔记",
            ),
            (
                "THOUGHT",
                "不应出现",
                "fyodor",
                "2026-07-18 07:00:00",
                "",
                "recent",
                1,
                0,
                "念头",
            ),
        ])

    def test_index_and_legacy_share_entry_ids(self):
        index = memory_library.build_memory_library_index(self.conn)
        full = memory_library.build_memory_library(self.conn)
        self.assertEqual(
            {e["id"] for e in index["entries"]},
            {e["id"] for e in full["entries"]},
        )

    def test_topics_match_between_index_and_legacy(self):
        index = memory_library.build_memory_library_index(self.conn)
        full = memory_library.build_memory_library(self.conn)
        self.assertEqual(index["topics"], full["topics"])

    def test_index_metadata_matches_legacy(self):
        index = memory_library.build_memory_library_index(self.conn)
        full = memory_library.build_memory_library(self.conn)
        by_id_full = {e["id"]: e for e in full["entries"]}
        for entry in index["entries"]:
            legacy = by_id_full[entry["id"]]
            for key in (
                "id", "date", "time", "weight", "title", "summaryTitle",
                "preview", "who", "topics", "tags", "links",
            ):
                self.assertEqual(entry[key], legacy[key])
            self.assertIn("excerpt", entry)
            self.assertNotIn("content", entry)

    def test_excerpt_max_length(self):
        index = memory_library.build_memory_library_index(self.conn)
        for entry in index["entries"]:
            self.assertLessEqual(len(entry["excerpt"]), memory_library.EXCERPT_MAX_LEN)

    def test_legacy_still_returns_full_content(self):
        full = memory_library.build_memory_library(self.conn)
        long_entry = next(e for e in full["entries"] if e["summaryTitle"] == "长尾记忆")
        self.assertGreater(len(long_entry["content"]), memory_library.CONTENT_HEAD_LEN)

    def test_legacy_entry_shape_has_no_excerpt(self):
        full = memory_library.build_memory_library(self.conn)
        for entry in full["entries"]:
            self.assertNotIn("excerpt", entry)

    def test_index_title_fallback_uses_full_content_when_summary_title_blank(self):
        pad = "。" * 850
        body = f"普通开头{pad}《真正标题》"
        self.db_path, self.conn = _make_posts_db([
            ("MEMORY", body, "haya", "2026-07-20 10:00:00", "日常", "recent", 3, 0, ""),
        ])
        index = memory_library.build_memory_library_index(self.conn)
        full = memory_library.build_memory_library(self.conn)
        idx_entry = index["entries"][0]
        legacy_entry = full["entries"][0]
        self.assertEqual(legacy_entry["summaryTitle"], "真正标题")
        self.assertEqual(idx_entry["summaryTitle"], legacy_entry["summaryTitle"])
        self.assertEqual(idx_entry["title"], legacy_entry["title"])
        self.assertEqual(idx_entry["preview"], legacy_entry["preview"])

    def test_inverted_links_match_pair_scan(self):
        rows = memory_library._fetch_library_rows(self.conn, 500, content_mode='head')
        entries = []
        topic_labels = {}
        topic_types = {}
        assoc_by_id = {}
        for row in rows:
            entry, assocs, topic_key, tags, ptype = memory_library._entry_base_from_row(row, include_content=False)
            if tags:
                topic_labels.setdefault(topic_key, tags[0])
            else:
                topic_labels.setdefault(topic_key, memory_library.TYPE_HINTS.get(ptype, memory_library.TYPE_HINTS['MEMORY'])['name'])
                topic_types.setdefault(topic_key, ptype)
            entries.append(dict(entry))
            assoc_by_id[entry['id']] = set(assocs)

        ref_entries = [dict(e) for e in entries]
        new_entries = [dict(e) for e in entries]
        memory_library._compute_links_pair_scan(ref_entries, assoc_by_id)
        memory_library._compute_links_inverted(new_entries, assoc_by_id)
        for ref, new in zip(ref_entries, new_entries):
            self.assertEqual(ref["links"], new["links"])


class MemoryLibraryDetailTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "conn", None):
            self.conn.close()
        if getattr(self, "db_path", None):
            Path(self.db_path).unlink(missing_ok=True)

    def test_detail_returns_full_content_for_library_type(self):
        body = "完整正文不应截断" + ("x" * 2000)
        self.db_path, self.conn = _make_posts_db([
            ("MEMORY", body, "haya", "2026-07-20 10:00:00", "日常", "recent", 3, 0, "详情"),
        ])
        entry_id = self.conn.execute("SELECT id FROM posts").fetchone()[0]
        detail = memory_library.get_memory_library_entry_detail(self.conn, entry_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["content"], body.strip())

    def test_detail_rejects_thought_dream_resolved(self):
        self.db_path, self.conn = _make_posts_db([
            ("THOUGHT", "念头", "fyodor", "2026-07-20 10:00:00", "", "recent", 1, 0, "念头"),
            ("DREAM", "梦", "fyodor", "2026-07-19 10:00:00", "", "recent", 1, 0, "梦"),
            ("MEMORY", "已解决", "haya", "2026-07-18 10:00:00", "", "recent", 1, 1, "解决"),
        ])
        for row in self.conn.execute("SELECT id FROM posts"):
            self.assertIsNone(memory_library.get_memory_library_entry_detail(self.conn, row[0]))


class MemoryLibrarySearchTests(unittest.TestCase):
    def tearDown(self):
        if getattr(self, "conn", None):
            self.conn.close()
        if getattr(self, "db_path", None):
            Path(self.db_path).unlink(missing_ok=True)

    def test_search_finds_keyword_only_in_deep_content(self):
        prefix = "可见标题"
        deep_keyword = "深埋关键词"
        body = prefix + ("。" * 500) + deep_keyword
        self.db_path, self.conn = _make_posts_db([
            ("MEMORY", body, "haya", "2026-07-20 10:00:00", "日常", "recent", 3, 0, "可见标题"),
        ])
        results = memory_library.search_memory_library(self.conn, deep_keyword, limit=4)["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["summaryTitle"], "可见标题")

        index = memory_library.build_memory_library_index(self.conn)
        shallow_hits = [
            e for e in index["entries"]
            if deep_keyword in (e.get("summaryTitle", "") + e.get("excerpt", "") + e.get("preview", ""))
        ]
        self.assertEqual(shallow_hits, [])


if __name__ == "__main__":
    unittest.main()
