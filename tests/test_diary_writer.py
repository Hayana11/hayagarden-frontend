"""Focused diary writer contract tests."""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from tools.diary_writer import write_diary_row


class DiaryWriterTests(unittest.TestCase):
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

    def test_inserts_fixed_diary_row(self):
        conn = self.make_db()

        created = write_diary_row(conn, "今天值得留下的一页")
        row = conn.execute(
            "SELECT type, content, layer, author, processed FROM posts"
        ).fetchone()

        self.assertEqual(created["status"], "CREATED")
        self.assertEqual(row, ("DIARY", "今天值得留下的一页", "recent", "fyodor", 0))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)

    def test_rejects_empty_content_without_insert(self):
        conn = self.make_db()

        result = write_diary_row(conn, "   ")

        self.assertEqual(result, {"status": "INVALID_CONTENT"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)

    def test_writer_has_no_duplicate_or_second_model_contract(self):
        source = (
            Path(__file__).resolve().parents[1] / "tools" / "diary_writer.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("ALREADY_EXISTS", source)
        self.assertNotIn("auto_diary.generate", source)
        self.assertNotIn("anthropic", source.lower())
        self.assertNotIn("deepseek", source.lower())


if __name__ == "__main__":
    unittest.main()
