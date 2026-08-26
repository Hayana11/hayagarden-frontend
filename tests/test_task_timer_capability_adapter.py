from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import command_store
from tools.task_timer_capability_adapter import _main, start_task_timer


def create_commands_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE commands ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "title TEXT NOT NULL, "
        "countdown_seconds INTEGER, "
        "created_at INTEGER NOT NULL, "
        "started_at INTEGER, "
        "done_at INTEGER, "
        "canceled INTEGER DEFAULT 0, "
        "duration_ms INTEGER, "
        "vs_countdown INTEGER, "
        "created_by TEXT DEFAULT 'fyodor', "
        "consumed INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()


class TaskTimerCapabilityAdapterTests(unittest.TestCase):
    def test_valid_countdown_uses_explicit_temp_db_and_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            create_commands_db(db)
            result = start_task_timer(str(db), title=" 收拾桌子 ", countdown_seconds=600)
            self.assertEqual(result["status"], "CREATED")
            self.assertIsInstance(result["id"], int)
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT title, countdown_seconds, created_by FROM commands"
            ).fetchone()
            conn.close()
            self.assertEqual(row, ("收拾桌子", 600, "fyodor"))

    def test_omitted_and_zero_are_count_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            create_commands_db(db)
            omitted = start_task_timer(str(db), title="看十页书")
            zero = start_task_timer(str(db), title="喝水", countdown_seconds=0)
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT title, countdown_seconds FROM commands ORDER BY id"
            ).fetchall()
            conn.close()
            self.assertEqual(omitted["status"], "CREATED")
            self.assertEqual(zero["status"], "CREATED")
            self.assertEqual(rows, [("看十页书", None), ("喝水", None)])

    def test_invalid_title_and_negative_countdown_do_not_create_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            create_commands_db(db)
            self.assertEqual(start_task_timer(str(db), title="   ")["status"], "INVALID_TITLE")
            self.assertEqual(
                start_task_timer(str(db), title="错误", countdown_seconds=-1)["status"],
                "INVALID_COUNTDOWN",
            )
            conn = sqlite3.connect(db)
            count = conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
            conn.close()
            self.assertEqual(count, 0)

    def test_stdin_contract_and_operation_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            create_commands_db(db)
            stdin = io.StringIO(json.dumps({
                "operation": "start_task_timer",
                "title": "读十页书",
                "countdown_seconds": 120,
                "db_path": str(db),
            }, ensure_ascii=False))
            stdout = io.StringIO()
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                self.assertEqual(_main(), 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "CREATED")

    def test_old_and_new_create_semantics_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_db = Path(tmp) / "old.db"
            new_db = Path(tmp) / "new.db"
            create_commands_db(old_db)
            create_commands_db(new_db)
            old_id = command_store.issue(" 同一任务 ", 600, db_path=str(old_db))
            new_result = start_task_timer(
                str(new_db), title=" 同一任务 ", countdown_seconds=600
            )
            self.assertEqual(new_result["status"], "CREATED")
            conn_a = sqlite3.connect(old_db)
            conn_b = sqlite3.connect(new_db)
            row_a = conn_a.execute(
                "SELECT title, countdown_seconds, created_by FROM commands"
            ).fetchone()
            row_b = conn_b.execute(
                "SELECT title, countdown_seconds, created_by FROM commands"
            ).fetchone()
            conn_a.close()
            conn_b.close()
            self.assertEqual(row_a, row_b)
            self.assertIsInstance(old_id, int)


if __name__ == "__main__":
    unittest.main()
