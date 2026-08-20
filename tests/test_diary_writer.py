"""Focused atomic diary writer contract tests."""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest

from tools.diary_writer import write_diary_once


class DiaryWriterTests(unittest.TestCase):
    NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)

    def make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                layer TEXT NOT NULL,
                processed INTEGER DEFAULT 0
            )
            """
        )
        return conn

    def test_existing_same_day_returns_already_exists_without_second_insert(self):
        conn = self.make_db()
        conn.execute(
            "INSERT INTO posts (type, content, author, created_at, layer, processed) "
            "VALUES ('DIARY', '已有日记', 'fyodor', '2026-08-20 09:00:00', 'recent', 0)"
        )
        conn.commit()

        result = write_diary_once(conn, "第二篇不应写入", now=self.NOW)
        count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE type='DIARY' AND author='fyodor'"
        ).fetchone()[0]

        self.assertEqual(result, {"status": "ALREADY_EXISTS"})
        self.assertEqual(count, 1)

    def test_no_existing_diary_inserts_fixed_row_and_duplicate_is_atomic(self):
        conn = self.make_db()

        created = write_diary_once(conn, "今天值得留下的一页", now=self.NOW)
        row = conn.execute(
            "SELECT type, content, layer, author, processed FROM posts"
        ).fetchone()
        duplicate = write_diary_once(conn, "第二次尝试", now=self.NOW)

        self.assertEqual(created["status"], "CREATED")
        self.assertEqual(row, ("DIARY", "今天值得留下的一页", "recent", "fyodor", 0))
        self.assertEqual(duplicate, {"status": "ALREADY_EXISTS"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)

    def test_writer_has_no_generation_or_second_model_call(self):
        source = open("tools/diary_writer.py", encoding="utf-8").read()
        self.assertNotIn("auto_diary.generate", source)
        self.assertNotIn("anthropic", source.lower())
        self.assertNotIn("deepseek", source.lower())
        self.assertIn("BEGIN IMMEDIATE", source)


if __name__ == "__main__":
    unittest.main()
