"""Morning Wake gates and dedup, with no live HTTP/model calls."""

from __future__ import annotations

import datetime
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat.interaction_state import InteractionClock, wake_guard_reason
import tools.dream_wake as dream_wake


class DreamWakeMorningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / 'morning.db')
        self.log_path = str(Path(self._tmp.name) / 'dream_wake.log')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY,
                woke_at TEXT,
                thoughts TEXT,
                action TEXT,
                content TEXT,
                consumed INTEGER DEFAULT 0,
                cache_info TEXT DEFAULT '',
                wake_run_id TEXT DEFAULT ''
            );
            CREATE UNIQUE INDEX idx_wake_log_run_id
                ON wake_log(wake_run_id)
                WHERE wake_run_id IS NOT NULL AND wake_run_id != '';
            """
        )
        conn.close()
        self._orig_db = dream_wake.DB_PATH
        self._orig_log = dream_wake.LOG_FILE
        dream_wake.DB_PATH = self.db_path
        dream_wake.LOG_FILE = self.log_path

    def tearDown(self):
        dream_wake.DB_PATH = self._orig_db
        dream_wake.LOG_FILE = self._orig_log
        self._tmp.cleanup()

    def _insert_run_id(self, wake_run_id):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO wake_log (woke_at, action, wake_run_id) VALUES (?,?,?)",
            ('2026-07-22 08:55:00', 'none', wake_run_id),
        )
        conn.commit()
        conn.close()

    def test_morning_dedup_uses_exact_wake_run_id(self):
        self._insert_run_id('morning-2026-07-22')
        self.assertTrue(dream_wake._morning_already_ran('morning-2026-07-22'))
        self.assertFalse(dream_wake._morning_already_ran('morning-2026-07-23'))

    def test_duplicate_run_id_is_zero_call(self):
        self._insert_run_id('morning-2026-07-22')
        with mock.patch.object(dream_wake, '_call_wake') as call_wake:
            dream_wake.run_morning(datetime.datetime(2026, 7, 22, 8, 50))
        call_wake.assert_not_called()

    def test_recent_interaction_is_zero_call(self):
        now = datetime.datetime(2026, 7, 22, 8, 50)
        with (
            mock.patch.object(dream_wake, '_calc_t_hours', return_value=5 / 60),
            mock.patch.object(dream_wake._wcfg, 'get_float', return_value=30),
            mock.patch.object(dream_wake, '_call_wake') as call_wake,
        ):
            dream_wake.run_morning(now)
        call_wake.assert_not_called()

    def test_morning_chat_race_is_zero_call(self):
        clock = InteractionClock(
            last_user_at=datetime.datetime(2026, 7, 22, 7, 50),
            last_wake_message_at=None,
            user_idle_hours=1.0,
            effective_idle_hours=1.0,
            reliable=True,
            reason='ok',
        )
        model_call = mock.Mock()
        reason = wake_guard_reason(clock, mode='morning', chat_busy=True)
        if reason is None:
            model_call()
        self.assertEqual(reason, 'chat_generating')
        model_call.assert_not_called()

    def test_morning_calls_wake_with_exact_run_id(self):
        now = datetime.datetime(2026, 7, 22, 8, 50)
        with (
            mock.patch.object(dream_wake, '_calc_t_hours', return_value=1.0),
            mock.patch.object(dream_wake._wcfg, 'get_float', return_value=30),
            mock.patch.object(
                dream_wake, '_call_wake', return_value={'action': 'message'}
            ) as call_wake,
        ):
            dream_wake.run_morning(now)
        call_wake.assert_called_once_with({
            'mode': 'morning',
            'wake_run_id': 'morning-2026-07-22',
        })

    def test_log_file_is_temporary_and_closed(self):
        writer = mock.mock_open()
        with mock.patch('builtins.open', writer):
            dream_wake._log('morning test')
        writer.assert_called_once_with(self.log_path, 'a', encoding='utf-8')
        writer.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
