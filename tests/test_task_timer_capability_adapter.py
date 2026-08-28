from __future__ import annotations

import importlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class TaskTimerCapabilityAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import must be side-effect free; all lifecycle tests use temporary DBs.
        importlib.invalidate_caches()
        cls.command_store = importlib.import_module("command_store")
        cls.adapter = importlib.import_module(
            "tools.task_timer_capability_adapter"
        )

    def test_conn_is_pure_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "fresh.db"
            conn = self.command_store._conn(str(db))
            conn.close()
            check = sqlite3.connect(db)
            row = check.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='commands'"
            ).fetchone()
            check.close()
            self.assertIsNone(row)

    def test_init_is_the_schema_initializer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "initialized.db"
            self.command_store._init(str(db))
            conn = sqlite3.connect(db)
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(commands)").fetchall()
            ]
            conn.close()
            self.assertEqual(
                columns,
                [
                    "id",
                    "title",
                    "countdown_seconds",
                    "created_at",
                    "started_at",
                    "done_at",
                    "canceled",
                    "duration_ms",
                    "vs_countdown",
                    "created_by",
                    "consumed",
                ],
            )

    def test_default_issue_path_is_mocked_and_preserved(self):
        fake = MagicMock()
        fake.execute.return_value.lastrowid = 7
        with tempfile.TemporaryDirectory() as tmp:
            original_path = self.command_store.DB_PATH
            self.command_store.DB_PATH = str(Path(tmp) / "commands.db")
            try:
                with patch.object(self.command_store.sqlite3, "connect", return_value=fake) as connect:
                    self.command_store.issue("mocked task")
            finally:
                self.command_store.DB_PATH = original_path
        expected = str(Path(tmp) / "commands.db")
        self.assertEqual(
            connect.call_args_list,
            [((expected,), {"timeout": 5}), ((expected,), {"timeout": 5})],
        )
        self.assertEqual(fake.commit.call_count, 2)
        self.assertEqual(fake.close.call_count, 2)

    def test_explicit_temp_db_preserves_legacy_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            self.command_store._init(str(db))
            result = self.command_store.issue(
                " 收拾桌子 ",
                600,
                db_path=str(db),
            )
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT title, countdown_seconds, created_by FROM commands"
            ).fetchone()
            conn.close()
            self.assertIsInstance(result, int)
            self.assertEqual(row, ("收拾桌子", 600, "fyodor"))

    def test_explicit_temp_db_omitted_countdown_is_count_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            self.command_store._init(str(db))
            self.command_store.issue("看十页书", db_path=str(db))
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT title, countdown_seconds FROM commands"
            ).fetchone()
            conn.close()
            self.assertEqual(row, ("看十页书", None))

    def test_adapter_contract_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            self.command_store._init(str(db))
            created = self.adapter.start_task_timer(
                str(db),
                title=" 收拾桌子 ",
                countdown_seconds=600,
            )
            invalid_title = self.adapter.start_task_timer(
                str(db),
                title="   ",
            )
            invalid_countdown = self.adapter.start_task_timer(
                str(db),
                title="错误",
                countdown_seconds=-1,
            )
            self.assertEqual(created["status"], "CREATED")
            self.assertEqual(invalid_title["status"], "INVALID_TITLE")
            self.assertEqual(invalid_countdown["status"], "INVALID_COUNTDOWN")
            conn = sqlite3.connect(db)
            rows = conn.execute(
                "SELECT title, countdown_seconds, created_by FROM commands"
            ).fetchall()
            conn.close()
            self.assertEqual(rows, [("收拾桌子", 600, "fyodor")])

    def test_adapter_stdin_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "commands.db"
            self.command_store._init(str(db))
            stdin = io.StringIO(json.dumps({
                "operation": "start_task_timer",
                "title": "读十页书",
                "countdown_seconds": 120,
                "db_path": str(db),
            }, ensure_ascii=False))
            stdout = io.StringIO()
            with patch("sys.stdin", stdin), patch("sys.stdout", stdout):
                self.assertEqual(self.adapter._main(), 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "CREATED")


if __name__ == "__main__":
    unittest.main()
