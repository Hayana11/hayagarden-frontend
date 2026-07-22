"""dream_wake morning: dedup + Wake mode routing (no live /wake calls)."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import datetime
import tools.dream_wake as dream_wake


class DreamWakeMorningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE wake_log ("
            "id INTEGER PRIMARY KEY, woke_at TEXT, thoughts TEXT, action TEXT, "
            "content TEXT, consumed INTEGER DEFAULT 0, cache_info TEXT DEFAULT '')"
        )
        conn.commit()
        conn.close()
        self._orig_db = dream_wake.DB_PATH
        dream_wake.DB_PATH = self.db_path

    def tearDown(self):
        dream_wake.DB_PATH = self._orig_db
        Path(self.db_path).unlink(missing_ok=True)

    def test_morning_already_ran_detects_wake_log_mode(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wake_log (woke_at, action, cache_info) VALUES (?,?,?)",
            ('2026-07-22 08:55:00', 'message', '{"mode": "morning"}'),
        )
        conn.commit()
        conn.close()
        self.assertTrue(dream_wake._morning_already_ran('2026-07-22'))

    def test_morning_skips_when_already_ran(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wake_log (woke_at, action, cache_info) VALUES (?,?,?)",
            ('2026-07-22 08:55:00', 'none', '{"wake_mode": "morning"}'),
        )
        conn.commit()
        conn.close()
        with mock.patch.object(dream_wake, '_call_wake') as call_wake:
            dream_wake.run_morning(datetime.datetime(2026, 7, 22, 8, 50))
            call_wake.assert_not_called()

    def test_morning_calls_wake_with_run_id(self):
        now = dream_wake.datetime.datetime(2026, 7, 22, 8, 50)
        with mock.patch.object(dream_wake, '_call_wake', return_value={'action': 'message'}) as call_wake:
            dream_wake.run_morning(now)
        call_wake.assert_called_once_with({
            'mode': 'morning',
            'wake_run_id': 'morning-2026-07-22',
        })


if __name__ == '__main__':
    unittest.main()
