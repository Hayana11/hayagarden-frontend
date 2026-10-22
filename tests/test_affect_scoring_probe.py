import datetime
import os
import sqlite3
import tempfile
import unittest

from tools.affect_scoring_probe import check_affect_scoring_freshness

NOW = datetime.datetime(2026, 10, 23, 9, 0, 0)


class AffectScoringProbeTests(unittest.TestCase):
    def setUp(self):
        handle, self.db = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE internal_state_v3 (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_scored_message_id INTEGER, updated_at TEXT
            );
            CREATE TABLE internal_state_events (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE
            );
            INSERT INTO internal_state_v3 VALUES (1, 10, '2026-10-23 08:00:00');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)

    def _exec(self, sql, *args):
        conn = sqlite3.connect(self.db)
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def _turn(self, user_id, reply_id, replied_at):
        self._exec("INSERT INTO chat_messages VALUES (?, 'hayana', 'hi', ?)", user_id, replied_at)
        self._exec("INSERT INTO chat_messages VALUES (?, 'fyodor', 'hello', ?)", reply_id, replied_at)

    def check(self):
        return check_affect_scoring_freshness(self.db, now=NOW, grace_minutes=15)

    def test_unscored_turn_past_grace_is_reported(self):
        self._turn(11, 12, '2026-10-23 08:30:00')
        message = self.check()
        self.assertIn('#11', message)
        self.assertIn('30 分钟', message)
        self.assertIn('last_scored_message_id=10', message)

    def test_within_grace_is_fine(self):
        self._turn(11, 12, '2026-10-23 08:50:00')
        self.assertIsNone(self.check())

    def test_watermark_covers_the_turn(self):
        self._turn(11, 12, '2026-10-23 08:30:00')
        self._exec('UPDATE internal_state_v3 SET last_scored_message_id=11')
        self.assertIsNone(self.check())

    def test_event_already_recorded(self):
        self._turn(11, 12, '2026-10-23 08:30:00')
        self._exec("INSERT INTO internal_state_events (event_key) VALUES ('user_scored:11')")
        self.assertIsNone(self.check())

    def test_burst_only_the_turn_message_counts(self):
        # 11 and 12 sent together; the turn is keyed to 12, which was scored.
        self._exec("INSERT INTO chat_messages VALUES (11, 'hayana', 'a', '2026-10-23 08:29:00')")
        self._turn(12, 13, '2026-10-23 08:30:00')
        self._exec('UPDATE internal_state_v3 SET last_scored_message_id=12')
        self.assertIsNone(self.check())

    def test_message_without_reply_is_not_judged(self):
        self._turn(11, 12, '2026-10-23 08:00:00')
        self._exec('UPDATE internal_state_v3 SET last_scored_message_id=11')
        self._exec("INSERT INTO chat_messages VALUES (13, 'hayana', 'still typing', '2026-10-23 08:10:00')")
        self.assertIsNone(self.check())

    def test_missing_tables_cannot_be_judged(self):
        handle, empty = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        try:
            self.assertIsNone(check_affect_scoring_freshness(empty, now=NOW))
        finally:
            os.unlink(empty)


if __name__ == '__main__':
    unittest.main()
