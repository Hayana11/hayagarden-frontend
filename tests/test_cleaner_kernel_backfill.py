"""cleaner task 5 must never hold a post the Memory Kernel already owns."""
from __future__ import annotations

import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import_cleaner():
    # cleaner reads /opt/frontend/.env at import time; give it an empty one.
    sys.modules.pop("tools.cleaner", None)
    with mock.patch("builtins.open", mock.mock_open(read_data="")):
        return importlib.import_module("tools.cleaner")


cleaner = _import_cleaner()

_POSTS = (
    "CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,"
    " layer TEXT, importance INTEGER, valence REAL, arousal REAL, processed INTEGER DEFAULT 0)"
)
# Same shape as tools.memory_tool._BACKFILL_SCHEMA.
_BACKFILL = (
    "CREATE TABLE memory_kernel_backfill (post_id INTEGER PRIMARY KEY, queued_at TEXT NOT NULL,"
    " reason TEXT NOT NULL, done_at TEXT, state_id TEXT)"
)


class CleanerKernelBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "memories.db")
        self.held = []

        async def fake_hold(items):
            self.held.extend(item["id"] for item in items)
            return [(item["id"], "bucket", None) for item in items]

        answer = {"importance": 6, "valence": 0.1, "arousal": 0.2, "tags": "日常"}
        for patcher in (
            mock.patch.object(cleaner, "DB_PATH", self.db),
            mock.patch.object(cleaner, "ask", return_value=answer),
            mock.patch.object(cleaner, "_batch_hold", fake_hold),
            mock.patch.object(cleaner, "log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self, *, backfill):
        conn = sqlite3.connect(self.db)
        conn.execute(_POSTS)
        conn.executemany(
            "INSERT INTO posts (id, type, content) VALUES (?, ?, ?)",
            [(1, "MEMORY", "普通记忆"), (2, "DIARY", "日记"), (3, "MISS", "想你"),
             (4, "MEMORY", "回退待补"), (5, "FACT", "回退已补")],
        )
        if backfill:
            conn.execute(_BACKFILL)
            conn.executemany(
                "INSERT INTO memory_kernel_backfill (post_id, queued_at, reason, done_at, state_id)"
                " VALUES (?, 't', 'kernel_error', ?, ?)",
                [(4, None, None), (6, "t2", "st:x")],
            )
            conn.execute("INSERT INTO posts (id, type, content) VALUES (6, 'MISS', '回退已补完')")
        conn.commit()
        conn.close()

    def test_pending_backfill_is_not_held(self):
        self._seed(backfill=True)
        cleaner.process_and_sync()
        self.assertNotIn(4, self.held)

    def test_completed_backfill_is_not_held(self):
        self._seed(backfill=True)
        cleaner.process_and_sync()
        self.assertNotIn(6, self.held)

    def test_ordinary_rows_unchanged_with_and_without_backfill_table(self):
        for backfill in (True, False):
            with self.subTest(backfill=backfill):
                Path(self.db).unlink(missing_ok=True)
                self.held.clear()
                self._seed(backfill=backfill)
                cleaner.process_and_sync()
                expected = [1, 2, 3] if backfill else [1, 2, 3, 4]
                self.assertEqual(sorted(self.held), expected)
                conn = sqlite3.connect(self.db)
                processed = [r[0] for r in conn.execute(
                    "SELECT id FROM posts WHERE processed=1 ORDER BY id")]
                conn.close()
                self.assertEqual(processed, expected)


if __name__ == "__main__":
    unittest.main()
