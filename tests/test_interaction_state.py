"""A1.1 interaction clock + unified touch tests."""

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

from chat.interaction_state import (
    read_interaction_clock,
    touch_user_interaction,
    wake_guard_reason,
)
import desire


class InteractionClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, woke_at TEXT
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_authoritative_user_idle_ignores_stale_desire_field(self):
        """desire_state 335h vs chat_messages 10min → clock uses chat."""
        now = datetime.datetime(2026, 7, 20, 12, 0, 0)
        user_at = (now - datetime.timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', 'hi', user_at),
        )
        conn.commit()
        conn.close()

        clock = read_interaction_clock(self.get_db, now=now)
        self.assertTrue(clock.reliable)
        self.assertAlmostEqual(clock.user_idle_hours, 10 / 60, places=3)

        # Stale desire field would claim 335h; override must win.
        L, phase, t = desire.get_longing(t_hours_override=clock.user_idle_hours)
        self.assertAlmostEqual(t, 10 / 60, places=3)
        self.assertLess(t, 1.0)
        snip = desire.get_wake_snippet(t_hours_override=clock.user_idle_hours)
        self.assertNotIn('335', snip)
        # Force a longing phase so the idle hours line is present.
        snip_long = desire.get_wake_snippet(t_hours_override=48.0)
        self.assertIn('48.0h', snip_long)
        self.assertNotIn('335', snip_long)

    def test_fail_closed_when_timestamp_missing(self):
        clock = read_interaction_clock(self.get_db)
        self.assertFalse(clock.reliable)
        self.assertIsNone(clock.user_idle_hours)
        self.assertEqual(
            wake_guard_reason(clock, mode='normal', min_idle_minutes=30),
            'missing_user_timestamp',
        )

    def test_fail_closed_when_timestamp_corrupt(self):
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', 'hi', 'not-a-date'),
        )
        conn.commit()
        conn.close()
        clock = read_interaction_clock(self.get_db)
        self.assertFalse(clock.reliable)
        self.assertEqual(
            wake_guard_reason(clock, mode='normal'),
            'missing_user_timestamp',
        )

    def test_idle_boundary_29m59_skip_30m_allow(self):
        now = datetime.datetime(2026, 7, 20, 12, 0, 0)
        for minutes, expect_skip in ((29 + 59 / 60, True), (30.0, False)):
            user_at = (now - datetime.timedelta(minutes=minutes)).strftime(
                '%Y-%m-%d %H:%M:%S'
            )
            conn = self.get_db()
            conn.execute('DELETE FROM chat_messages')
            conn.execute(
                "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
                ('hayana', 'hi', user_at),
            )
            conn.commit()
            conn.close()
            clock = read_interaction_clock(self.get_db, now=now)
            reason = wake_guard_reason(clock, mode='normal', min_idle_minutes=30)
            if expect_skip:
                self.assertEqual(reason, 'recent_interaction', minutes)
            else:
                self.assertIsNone(reason, minutes)

    def test_chat_busy_and_wake_busy(self):
        now = datetime.datetime(2026, 7, 20, 12, 0, 0)
        user_at = (now - datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        conn = self.get_db()
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', 'hi', user_at),
        )
        conn.commit()
        conn.close()
        clock = read_interaction_clock(self.get_db, now=now)
        self.assertEqual(
            wake_guard_reason(clock, mode='normal', chat_busy=True),
            'chat_generating',
        )
        self.assertEqual(
            wake_guard_reason(clock, mode='nightwatch', chat_busy=True),
            'chat_generating',
        )
        self.assertEqual(
            wake_guard_reason(clock, mode='normal', wake_busy=True),
            'wake_in_progress',
        )


class TouchOnceTests(unittest.TestCase):
    def test_touch_calls_emotion_desire_once_without_drive_writers(self):
        """Stage D: touch only refreshes compatibility clocks; no drive writers."""
        calls = []

        class EE:
            @staticmethod
            def touch_interaction():
                calls.append('emotion')

        class Des:
            @staticmethod
            def touch_hayana():
                calls.append('desire')

        class DE:
            @staticmethod
            def rest():
                calls.append('rest')

            @staticmethod
            def get_drive():
                return {'attachment': 0.5}

            @staticmethod
            def discharge(key):
                calls.append(('discharge', key))

        with mock.patch.dict(sys.modules, {
            'emotion_engine': EE,
            'desire': Des,
            'drive_engine': DE,
        }):
            touch_user_interaction(lambda: None)
        self.assertEqual(calls, ['emotion', 'desire'])
        self.assertNotIn('rest', calls)
        self.assertNotIn(('discharge', 'attachment'), calls)

    def test_insert_user_message_touches_once(self):
        import moments_turn

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'm.db')
        conn = sqlite3.connect(db_path)
        conn.execute(
            'CREATE TABLE chat_messages ('
            'id INTEGER PRIMARY KEY, author TEXT, content TEXT, '
            "created_at TEXT DEFAULT (datetime('now','+8 hours')))"
        )
        conn.execute(
            'CREATE TABLE moments_active_turn (conversation_id TEXT, turn_key TEXT, '
            'user_message_id INTEGER, started_at TEXT)'
        )
        conn.commit()
        conn.close()

        touch_calls = []

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch(
            'chat.interaction_state.touch_user_interaction',
            side_effect=lambda *_a, **_k: touch_calls.append(1),
        ):
            moments_turn.insert_user_message(
                get_db,
                {'turn_key': 'k1', 'content': '你好'},
                '你好',
                memories_db_path=db_path,
            )
        self.assertEqual(len(touch_calls), 1)
        # Empty content must not touch.
        with mock.patch(
            'chat.interaction_state.touch_user_interaction',
            side_effect=lambda *_a, **_k: touch_calls.append(1),
        ):
            moments_turn.insert_user_message(
                get_db, {'turn_key': 'k2'}, '', memories_db_path=db_path,
            )
        self.assertEqual(len(touch_calls), 1)


if __name__ == '__main__':
    unittest.main()
