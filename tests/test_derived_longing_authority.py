"""ISV3-1B Stage B｜Derived Longing Authority — narrow acceptance cases."""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-runtime-config.db'),
)

import desire
import emotion_engine as ee
import internal_state as ist
from chat.interaction_state import read_interaction_clock


def _tau18(t_hours: float) -> float:
    L = 0.85 * (1 - (1 + t_hours / 18.0) ** (-0.8))
    return round(min(L, 0.90), 3)


class DerivedLongingAuthorityTests(unittest.TestCase):
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
            CREATE TABLE emotion_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pa REAL, na REAL, valence REAL, arousal REAL,
                mood_word TEXT, longing REAL DEFAULT 0.0,
                sternberg_p REAL, sternberg_i REAL, sternberg_c REAL,
                p_updated_at TEXT, i_updated_at TEXT,
                last_interaction TEXT, updated_at TEXT
            );
            INSERT INTO emotion_state (
                id, pa, na, valence, arousal, mood_word, longing,
                sternberg_p, sternberg_i, sternberg_c,
                p_updated_at, i_updated_at, last_interaction, updated_at
            ) VALUES (
                1, 0.5, 0.2, 0.6, 0.3, '平静', 0.0,
                0.0, 0.3, 0.7, NULL, NULL, NULL, NULL
            );
            CREATE TABLE desire_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                curiosity REAL, reflection REAL, duty REAL, social REAL,
                libido REAL, stress REAL, fatigue REAL,
                last_updated TEXT, last_hayana_msg_time TEXT
            );
            INSERT INTO desire_state (
                id, curiosity, reflection, duty, social, libido, stress, fatigue,
                last_updated, last_hayana_msg_time
            ) VALUES (
                1, 0.1, 0.1, 0.15, 0.1, 0.0, 0.1, 0.2, NULL, NULL
            );
            """
        )
        conn.commit()
        conn.close()
        self._prev_memories = os.environ.get('MEMORIES_DB')
        self._prev_desire_db = desire.DB_PATH
        self._prev_ee_db = ee.DB_PATH
        os.environ['MEMORIES_DB'] = self.db_path
        desire.DB_PATH = self.db_path
        ee.DB_PATH = self.db_path
        self.now = datetime.datetime(2026, 8, 4, 12, 0, 0)

    def tearDown(self):
        desire.DB_PATH = self._prev_desire_db
        ee.DB_PATH = self._prev_ee_db
        if self._prev_memories is None:
            os.environ.pop('MEMORIES_DB', None)
        else:
            os.environ['MEMORIES_DB'] = self._prev_memories
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _set_user_at(self, when: datetime.datetime):
        conn = self.get_db()
        conn.execute('DELETE FROM chat_messages')
        conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', 'hi', when.strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()

    def _set_legacy_timestamps(self, *, emotion_at, desire_at):
        conn = self.get_db()
        conn.execute(
            "UPDATE emotion_state SET last_interaction=? WHERE id=1",
            (emotion_at.strftime('%Y-%m-%d %H:%M:%S'),),
        )
        conn.execute(
            "UPDATE desire_state SET last_hayana_msg_time=? WHERE id=1",
            (desire_at.strftime('%Y-%m-%d %H:%M:%S'),),
        )
        conn.commit()
        conn.close()

    def test_case1_single_formula_all_consumers(self):
        idle = 18.0
        user_at = self.now - datetime.timedelta(hours=idle)
        self._set_user_at(user_at)
        expect = _tau18(idle)
        self.assertEqual(ist.derived_longing_curve(idle), expect)
        self.assertEqual(ist.longing_desire_legacy_curve(idle), expect)

        auth = ist.read_derived_longing(self.get_db, now=self.now)
        self.assertEqual(auth, expect)

        with mock.patch('chat.interaction_state._now_beijing', return_value=self.now):
            ee_l = ee.get_longing()
            des_l, _phase, des_t = desire.get_longing()
        self.assertEqual(ee_l, expect)
        self.assertEqual(des_l, expect)
        self.assertAlmostEqual(des_t, idle, places=3)

        # Override path (Wake already holding clock) matches the same curve.
        des_ov, _, t_ov = desire.get_longing(t_hours_override=idle)
        self.assertEqual(des_ov, expect)
        self.assertEqual(t_ov, idle)

    def test_case2_wake_does_not_reset_user_idle_or_longing(self):
        user_at = self.now - datetime.timedelta(hours=24)
        wake_at = self.now - datetime.timedelta(minutes=5)
        self._set_user_at(user_at)
        conn = self.get_db()
        conn.execute(
            "INSERT INTO wake_log (action, woke_at) VALUES (?,?)",
            ('message', wake_at.strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()

        clock = read_interaction_clock(self.get_db, now=self.now)
        self.assertTrue(clock.reliable)
        self.assertAlmostEqual(clock.user_idle_hours, 24.0, places=3)
        self.assertLess(clock.effective_idle_hours, 1.0)

        expect = _tau18(24.0)
        auth = ist.read_derived_longing(self.get_db, now=self.now)
        self.assertEqual(auth, expect)
        with mock.patch('chat.interaction_state._now_beijing', return_value=self.now):
            self.assertEqual(ee.get_longing(), expect)
            L, _, t = desire.get_longing()
        self.assertEqual(L, expect)
        self.assertAlmostEqual(t, 24.0, places=3)

    def test_case3_legacy_timestamps_have_no_authority(self):
        # Authoritative idle = 6h via chat_messages.
        user_at = self.now - datetime.timedelta(hours=6)
        self._set_user_at(user_at)
        # Legacy fields claim 100h / 200h — must be ignored.
        self._set_legacy_timestamps(
            emotion_at=self.now - datetime.timedelta(hours=100),
            desire_at=self.now - datetime.timedelta(hours=200),
        )
        expect = _tau18(6.0)
        tau8_from_emotion = round(
            min(0.85 * (1 - (1 + 100 / 8) ** (-0.8)), 0.92), 3,
        )
        self.assertNotAlmostEqual(expect, tau8_from_emotion, places=2)

        auth = ist.read_derived_longing(self.get_db, now=self.now)
        self.assertEqual(auth, expect)
        with mock.patch('chat.interaction_state._now_beijing', return_value=self.now):
            self.assertEqual(ee.get_longing(), expect)
            L, _, t = desire.get_longing()
        self.assertEqual(L, expect)
        self.assertAlmostEqual(t, 6.0, places=3)
        self.assertNotAlmostEqual(t, 100.0, places=1)
        self.assertNotAlmostEqual(t, 200.0, places=1)

    def test_case4_satisfy_cannot_mutate_authoritative_longing(self):
        user_at = self.now - datetime.timedelta(hours=12)
        self._set_user_at(user_at)
        before = ist.read_derived_longing(self.get_db, now=self.now)
        desire.satisfy('tease')
        after = ist.read_derived_longing(self.get_db, now=self.now)
        self.assertEqual(before, after)
        self.assertEqual(before, _tau18(12.0))
        with mock.patch('chat.interaction_state._now_beijing', return_value=self.now):
            self.assertEqual(ee.get_longing(), before)
            self.assertEqual(desire.get_longing()[0], before)

    def test_case5_clock_unreadable_fail_closed(self):
        # No chat_messages → clock unreliable.
        auth = ist.read_derived_longing(self.get_db, now=self.now)
        self.assertIsNone(auth)
        self.assertIsNone(ist.derived_longing_curve(None))

        # Poison legacy timestamps — must still fail closed, not invent high L.
        self._set_legacy_timestamps(
            emotion_at=self.now - datetime.timedelta(hours=500),
            desire_at=self.now - datetime.timedelta(hours=500),
        )
        with mock.patch('chat.interaction_state._now_beijing', return_value=self.now):
            ee_l = ee.get_longing()
            des_l, phase, t = desire.get_longing()
        self.assertEqual(ee_l, 0.0)
        self.assertEqual(des_l, 0.0)
        self.assertEqual(phase, 'content')
        self.assertEqual(t, 0.0)
        self.assertNotEqual(t, 999.0)
        self.assertLess(ee_l, 0.05)

    def test_desire_delegates_to_derived_longing_curve(self):
        """Single formula implementation: desire must not own a τ18 body."""
        sentinel = 0.424
        with mock.patch(
            'internal_state.derived_longing_curve',
            return_value=sentinel,
        ) as curve:
            L, phase, t = desire.get_longing(t_hours_override=18.0)
        curve.assert_called()
        # Called with the authoritative idle hours (override path).
        self.assertTrue(
            any(
                args and float(args[0]) == 18.0
                for args, _kwargs in curve.call_args_list
            ),
            curve.call_args_list,
        )
        self.assertEqual(L, sentinel)
        self.assertEqual(t, 18.0)
        self.assertEqual(phase, desire._longing_phase(sentinel))

        # Clock path also delegates through the same curve.
        self._set_user_at(self.now - datetime.timedelta(hours=6))
        with mock.patch(
            'internal_state.derived_longing_curve',
            return_value=sentinel,
        ) as curve2, mock.patch(
            'chat.interaction_state._now_beijing',
            return_value=self.now,
        ):
            L2, _, t2 = desire.get_longing()
        curve2.assert_called()
        self.assertEqual(L2, sentinel)
        self.assertAlmostEqual(t2, 6.0, places=3)


if __name__ == '__main__':
    unittest.main()
