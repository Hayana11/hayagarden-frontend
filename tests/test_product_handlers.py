"""Contract tests for the shared Todo/Ledger/Diary handlers."""
from __future__ import annotations

import json
import sqlite3
import unittest

from tools.product_handlers import (
    ProductHandlerError,
    create_ledger,
    create_todo,
    list_todos,
    read_ledger,
    read_ledger_budget,
    update_ledger,
    write_diary_row,
    write_ledger_budget,
)


class ProductHandlerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                done INTEGER DEFAULT 0,
                due_date TEXT,
                author TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                category TEXT,
                note TEXT,
                date TEXT,
                author TEXT,
                meta TEXT,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE TABLE ledger_budget (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT UNIQUE,
                amount REAL NOT NULL
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                author TEXT NOT NULL,
                created_at DATETIME DEFAULT (datetime('now', '+8 hours')),
                layer TEXT NOT NULL,
                processed INTEGER DEFAULT 0
            );
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_todo_handler_preserves_order_and_validation(self):
        create_todo(
            self.conn,
            content="有日期",
            due_date="2026-08-21",
            author="fyodor",
        )
        create_todo(self.conn, content="无日期")
        todos = list_todos(self.conn)
        self.assertEqual([row["content"] for row in todos], ["有日期", "无日期"])
        with self.assertRaises(ProductHandlerError) as ctx:
            create_todo(self.conn, content="  ")
        self.assertEqual(ctx.exception.payload, {"error": "content required"})

    def test_ledger_handler_preserves_summary_budget_and_meta_whitelist(self):
        created = create_ledger(
            self.conn,
            amount=-68,
            category="餐饮",
            date="2026-08-10",
            meta={"who": "fyodor", "evil": "drop"},
        )
        update_ledger(self.conn, created["id"], {"note": "小面"})
        result = read_ledger(self.conn, month="2026-08")
        self.assertEqual(result["summary"]["expense"], -68.0)
        row = result["records"][0]
        self.assertEqual(json.loads(row["meta"]), {"who": "fyodor"})
        write_ledger_budget(self.conn, month="2026-08", amount=3000)
        self.assertEqual(
            read_ledger_budget(self.conn, month="2026-08")["amount"],
            3000,
        )

    def test_diary_handler_is_model_free_and_fixed(self):
        result = write_diary_row(self.conn, "  今天值得留下的一页  ")
        self.assertEqual(result["status"], "CREATED")
        row = self.conn.execute(
            "SELECT type, content, layer, author, processed FROM posts"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("DIARY", "今天值得留下的一页", "recent", "fyodor", 0),
        )


if __name__ == "__main__":
    unittest.main()
