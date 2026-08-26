from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.diary_capability_adapter import _main, write_diary
from tools.diary_writer import write_diary as write_diary_home


def create_posts_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE posts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "type TEXT NOT NULL, content TEXT NOT NULL, layer TEXT NOT NULL, "
        "author TEXT NOT NULL, processed INTEGER NOT NULL)"
    )
    conn.commit()
    conn.close()


class DiaryCapabilityAdapterTests(unittest.TestCase):
    def test_new_adapter_uses_explicit_db_and_preserves_row_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "new.db"
            create_posts_db(db_path)
            result = write_diary(str(db_path), content="  今天值得留下的一页  ")
            self.assertEqual(result["status"], "CREATED")
            self.assertIsInstance(result["id"], int)
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT type, layer, author, processed, content FROM posts"
            ).fetchone()
            conn.close()
            self.assertEqual(
                row,
                ("DIARY", "recent", "fyodor", 0, "今天值得留下的一页"),
            )

    def test_stdin_operation_and_home_semantic_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            home_db = Path(tmp) / "home.db"
            capability_db = Path(tmp) / "capability.db"
            create_posts_db(home_db)
            create_posts_db(capability_db)
            content = "  同一篇日记  "
            home_result = write_diary_home(content, db_path=str(home_db))
            stdin = io.StringIO(json.dumps({
                "operation": "write_diary",
                "content": content,
                "db_path": str(capability_db),
            }, ensure_ascii=False))
            stdout = io.StringIO()
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                self.assertEqual(_main(), 0)
            capability_result = json.loads(stdout.getvalue())
            self.assertEqual(home_result["status"], capability_result["status"])
            self.assertEqual(
                home_result["status"],
                "CREATED",
            )
            conn_a = sqlite3.connect(home_db)
            conn_b = sqlite3.connect(capability_db)
            row_a = conn_a.execute(
                "SELECT type, layer, author, processed, content FROM posts"
            ).fetchone()
            row_b = conn_b.execute(
                "SELECT type, layer, author, processed, content FROM posts"
            ).fetchone()
            conn_a.close()
            conn_b.close()
            self.assertEqual(row_a, row_b)


if __name__ == "__main__":
    unittest.main()
