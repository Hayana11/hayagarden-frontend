from __future__ import annotations

import builtins
import importlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.product_handlers import search_memory_posts


app_module = None


class MemorySearchHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.temp_dir.name) / "memory-search.db")
        conn = sqlite3.connect(cls.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL DEFAULT 'user',
                content TEXT NOT NULL,
                thinking TEXT DEFAULT '',
                image_url TEXT DEFAULT '',
                session_id INTEGER DEFAULT 1,
                created_at TEXT DEFAULT ''
            );
            CREATE TABLE runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT DEFAULT 'fyodor',
                created_at TEXT,
                pinned INTEGER DEFAULT 0,
                tags TEXT DEFAULT '',
                layer TEXT DEFAULT 'recent',
                resolved INTEGER DEFAULT 0,
                recall_count INTEGER DEFAULT 0,
                last_recalled_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        global app_module
        real_connect = sqlite3.connect
        real_open = builtins.open

        def isolated_connect(database, *args, **kwargs):
            if os_fspath(database) == "/opt/frontend/memories.db":
                database = cls.db_path
            conn = real_connect(database, *args, **kwargs)
            if os_fspath(database) == cls.db_path:
                conn.row_factory = sqlite3.Row
            return conn

        def isolated_open(file, *args, **kwargs):
            if os_fspath(file) == "/opt/frontend/.env":
                return io.StringIO("")
            return real_open(file, *args, **kwargs)

        with mock.patch.object(sqlite3, "connect", side_effect=isolated_connect),              mock.patch.object(builtins, "open", side_effect=isolated_open):
            app_module = importlib.import_module("app")

        cls.db_patch = mock.patch.object(app_module, "DB_PATH", cls.db_path)
        cls.db_patch.start()
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.db_patch.stop()
        cls.temp_dir.cleanup()

    def setUp(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM posts")
        rows = [
            (1, "MEMORY", "old needle", "2026-08-01", 0, "old", "recent", 0),
            (2, "MEMORY", "pinned needle", "2026-08-02", 1, "pin", "recent", 0),
            (3, "MEMORY", "needle three", "2026-08-03", 0, "three", "recent", 0),
            (4, "DIARY", "needle diary", "2026-08-04", 0, "diary", "recent", 0),
            (5, "MEMORY", "needle five", "2026-08-05", 0, "five", "core", 0),
            (6, "MEMORY", "needle six", "2026-08-06", 0, "six", "recent", 0),
            (7, "MEMORY", "needle seven", "2026-08-07", 0, "seven", "recent", 0),
            (8, "MEMORY", "needle eight", "2026-08-08", 0, "eight", "recent", 0),
            (9, "MEMORY", "needle nine", "2026-08-09", 0, "nine", "recent", 0),
            (10, "MEMORY", "needle ten", "2026-08-10", 0, "ten", "recent", 0),
            (11, "MEMORY", "needle eleven", "2026-08-11", 0, "eleven", "recent", 0),
            (12, "MEMORY", "needle twelve", "2026-08-12", 0, "twelve", "recent", 0),
            (50, "MEMORY", "content does not match", "2026-08-13", 0, "needle", "recent", 0),
            (60, "MEMORY", "resolved needle", "2026-08-14", 0, "resolved", "recent", 1),
            (70, "MEMORY", "中文关键词：海边", "2026-08-15", 0, "中文", "recent", 0),
        ]
        conn.executemany(
            """
            INSERT INTO posts
                (id, type, content, created_at, pinned, tags, layer, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()

    def _snapshot(self):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                """
                SELECT id, content, tags, resolved, pinned,
                       recall_count, last_recalled_at
                FROM posts ORDER BY id
                """
            ).fetchall()
        finally:
            conn.close()

    def test_handler_preserves_home_content_only_order_limit_and_read_only(self):
        before = self._snapshot()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = search_memory_posts(conn, keyword="needle", limit=8)
        finally:
            conn.close()

        self.assertEqual([row["id"] for row in rows], [60, 12, 11, 10, 9, 8, 7, 6])
        self.assertNotIn(50, [row["id"] for row in rows])
        self.assertEqual(rows[0]["resolved"], 1)
        self.assertEqual(rows[1]["pinned"], 0)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        self.assertEqual(self._snapshot(), before)

    def test_handler_empty_keyword_returns_latest_rows(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = search_memory_posts(conn, keyword="", limit=8)
        finally:
            conn.close()
        self.assertEqual([row["id"] for row in rows], [70, 60, 50, 12, 11, 10, 9, 8])

    def test_handler_accepts_unicode_keyword(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = search_memory_posts(conn, keyword="海边", limit=8)
        finally:
            conn.close()
        self.assertEqual([row["id"] for row in rows], [70])

    def test_home_search_route_consumes_handler_and_keeps_json_shape(self):
        with mock.patch.object(
            app_module,
            "handle_search_memory_posts",
            wraps=search_memory_posts,
        ) as handler:
            response = self.client.get("/api/posts?search=needle&limit=8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.get_json()), {"posts"})
        self.assertEqual(
            [row["id"] for row in response.get_json()["posts"]],
            [60, 12, 11, 10, 9, 8, 7, 6],
        )
        handler.assert_called_once()
        self.assertEqual(handler.call_args.kwargs, {"keyword": "needle", "limit": 8})

    def test_home_format_and_route_are_untouched(self):
        source = Path(__file__).resolve().parents[1] / "mcp-http-server.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("limit=8", text)
        self.assertIn("content || '').slice(0, 220)", text)
        self.assertIn("没有找到相关记忆", text)
        self.assertIn("Error: ' + e.message", text)

    def test_posts_without_search_keep_existing_filters_and_bypass_handler(self):
        with mock.patch.object(
            app_module,
            "handle_search_memory_posts",
            side_effect=AssertionError("non-search route used memory handler"),
        ):
            response = self.client.get(
                "/api/posts?type=DIARY&layer=recent&tags=diary&resolved=0&limit=50"
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([row["id"] for row in payload["posts"]], [4])

        response = self.client.get("/api/posts?limit=8")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["id"] for row in response.get_json()["posts"]],
            [70, 60, 50, 12, 11, 10, 9, 8],
        )

    def test_memory_alternatives_are_not_called(self):
        with mock.patch(
            "tools.memory_tool.search_memories",
            side_effect=AssertionError("memory_tool search called"),
        ), mock.patch(
            "tools.memory_library.search_memory_library",
            side_effect=AssertionError("memory library search called"),
        ), mock.patch(
            "tools.ombre_adapter.search_memories",
            side_effect=AssertionError("Ombre search called"),
        ):
            response = self.client.get("/api/posts?search=needle&limit=8")
        self.assertEqual(response.status_code, 200)


def os_fspath(value):
    return str(value)


if __name__ == "__main__":
    unittest.main()
