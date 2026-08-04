"""ISV3-1B Stage C｜Affect + Bond Authority — narrow acceptance cases."""

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

import emotion_engine as ee
import internal_state_events as events
import internal_state_store as store
from chat import affect_bond_authority as aba


class AffectBondAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT,
                created_at TEXT DEFAULT (datetime('now','+8 hours'))
            );
            CREATE TABLE emotion_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                pa REAL DEFAULT 0.5, na REAL DEFAULT 0.2,
                valence REAL DEFAULT 0.6, arousal REAL DEFAULT 0.3,
                mood_word TEXT DEFAULT '平静', longing REAL DEFAULT 0.0,
                sternberg_p REAL DEFAULT 0.0, sternberg_i REAL DEFAULT 0.3,
                sternberg_c REAL DEFAULT 0.7,
                p_updated_at TEXT, i_updated_at TEXT,
                last_interaction TEXT, updated_at TEXT
            );
            INSERT INTO emotion_state (id) VALUES (1);
            CREATE TABLE drive_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                attachment REAL DEFAULT 0.1, curiosity REAL DEFAULT 0.2,
                reflection REAL DEFAULT 0.1, social REAL DEFAULT 0.1,
                duty REAL DEFAULT 0.15, libido REAL DEFAULT 0.0,
                stress REAL DEFAULT 0.1, fatigue REAL DEFAULT 0.2,
                last_updated TEXT
            );
            INSERT INTO drive_state (id) VALUES (1);
            CREATE TABLE desire_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                curiosity REAL, reflection REAL, duty REAL, social REAL,
                libido REAL, stress REAL, fatigue REAL,
                last_updated TEXT, last_hayana_msg_time TEXT
            );
            INSERT INTO desire_state (id) VALUES (1);
            """
        )
        conn.commit()
        conn.close()
        self._prev_memories = os.environ.get('MEMORIES_DB')
        self._prev_ee = ee.DB_PATH
        os.environ['MEMORIES_DB'] = self.db_path
        ee.DB_PATH = self.db_path
        self.assertTrue(aba.ensure_authority_ready(self.db_path))

    def tearDown(self):
        ee.DB_PATH = self._prev_ee
        if self._prev_memories is None:
            os.environ.pop('MEMORIES_DB', None)
        else:
            os.environ['MEMORIES_DB'] = self._prev_memories
        self.tmp.cleanup()

    def _insert_user(self, text: str, *, created_at: str, prev_at=None) -> int:
        conn = sqlite3.connect(self.db_path)
        cur = conn.execute(
            "INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)",
            ('hayana', text, created_at),
        )
        mid = int(cur.lastrowid)
        conn.commit()
        conn.close()
        result = aba.apply_user_rule_observation(
            message_id=mid,
            text=text,
            created_at=created_at,
            previous_user_at=prev_at,
            db_path=self.db_path,
        )
        self.assertIn(result.status, ('applied', 'duplicate'))
        return mid

    def test_case1_affect_single_authority(self):
        mid = self._insert_user('你好', created_at='2026-08-04 10:00:00')
        scores = {
            'valence': 0.80, 'arousal': 0.55, 'mood_word': '温柔',
            'passion_delta': 0.02, 'intimacy_delta': 0.03,
            'source': 'test',
        }
        result = aba.apply_scored_observation(
            message_id=mid, scores=scores,
            scored_at='2026-08-04 10:00:05', db_path=self.db_path,
        )
        self.assertEqual(result.status, 'applied')

        # Poison legacy emotion_state — must not change production Affect.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE emotion_state SET pa=0.01, na=0.99, valence=0.01, "
            "arousal=0.01, mood_word='假的' WHERE id=1"
        )
        conn.commit()
        conn.close()

        state = ee.get_state()
        self.assertAlmostEqual(state['valence'], 0.80, places=3)
        self.assertAlmostEqual(state['arousal'], 0.55, places=3)
        self.assertEqual(state['mood_word'], '温柔')
        self.assertNotEqual(state['mood_word'], '假的')
        self.assertGreater(state['pa'], 0.2)

    def test_case2_bond_single_authority(self):
        mid = self._insert_user(
            '抱抱我好不好', created_at='2026-08-04 11:00:00',
        )
        v3 = aba.read_v3_state(self.db_path)
        self.assertIsNotNone(v3)
        auth_i = float(v3['intimacy'])
        auth_p = float(v3['passion'])
        self.assertGreater(auth_i, 0.3)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE emotion_state SET sternberg_i=0.01, sternberg_p=0.99, "
            "sternberg_c=0.01 WHERE id=1"
        )
        conn.commit()
        conn.close()

        desire = ee.get_desire()
        # Facade follows V3, not poisoned legacy sternberg_*.
        self.assertNotAlmostEqual(desire['i'], 0.01, places=2)
        self.assertAlmostEqual(desire['i'], auth_i, places=2)
        self.assertAlmostEqual(desire['p'], auth_p, places=2)
        del mid

    def test_case3_same_event_not_double_applied(self):
        mid = self._insert_user('想你', created_at='2026-08-04 12:00:00')
        scores = {
            'valence': 0.70, 'arousal': 0.40, 'mood_word': '思念',
            'passion_delta': 0.01, 'intimacy_delta': 0.02,
            'source': 'test',
        }
        r1 = aba.apply_scored_observation(
            message_id=mid, scores=scores,
            scored_at='2026-08-04 12:00:05', db_path=self.db_path,
        )
        before = aba.read_v3_state(self.db_path)
        r2 = aba.apply_scored_observation(
            message_id=mid, scores=scores,
            scored_at='2026-08-04 12:00:05', db_path=self.db_path,
        )
        after = aba.read_v3_state(self.db_path)
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(before['state_version'], after['state_version'])
        self.assertEqual(before['pa'], after['pa'])

        # user_rule retry also duplicate
        r3 = aba.apply_user_rule_observation(
            message_id=mid, text='想你',
            created_at='2026-08-04 12:00:00', previous_user_at=None,
            db_path=self.db_path,
        )
        self.assertEqual(r3.status, 'duplicate')

    def test_case4_stale_async_score_safe(self):
        m101 = self._insert_user('第一条', created_at='2026-08-04 13:00:00')
        m102 = self._insert_user(
            '第二条', created_at='2026-08-04 13:05:00',
            prev_at='2026-08-04 13:00:00',
        )
        r102 = aba.apply_scored_observation(
            message_id=m102,
            scores={
                'valence': 0.90, 'arousal': 0.60, 'mood_word': '新',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at='2026-08-04 13:05:05', db_path=self.db_path,
        )
        self.assertEqual(r102.status, 'applied')
        after_102 = aba.read_v3_state(self.db_path)
        r101 = aba.apply_scored_observation(
            message_id=m101,
            scores={
                'valence': 0.10, 'arousal': 0.10, 'mood_word': '旧',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at='2026-08-04 13:06:00', db_path=self.db_path,
        )
        self.assertEqual(r101.status, 'stale_skipped')
        after_stale = aba.read_v3_state(self.db_path)
        self.assertEqual(after_stale['mood_word'], '新')
        self.assertEqual(after_stale['valence'], after_102['valence'])
        self.assertEqual(
            after_stale['state_version'], after_102['state_version'],
        )

    def test_case5_ombre_has_no_write_authority(self):
        # Ombre adapter is read-only observation; mutating its return must not
        # invent a second Affect store — only blended scores enter V3 reducer.
        # Timestamps must be after Stage C bootstrap wall-clock (state clocks).
        mid = self._insert_user('嗯', created_at='2026-08-04 16:00:00')
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.0,  # [-1,1] → 0.5 after map
            'arousal': 0.2,
            'mood_word': '平静',
            'passion_delta': 0.0,
            'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(0.9, 0.9)), \
                mock.patch.object(ee, '_now_str', return_value='2026-08-04 16:00:05'):
            ee.score_and_update('excerpt', message_id=mid)
        v3 = aba.read_v3_state(self.db_path)
        # 0.7*0.5 + 0.3*0.9 = 0.62
        self.assertAlmostEqual(float(v3['valence']), 0.62, places=2)
        # No ombre_* columns / second state table.
        self.assertNotIn('ombre_valence', v3)
        conn = sqlite3.connect(self.db_path)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertNotIn('ombre_affect', tables)

    def test_case6_legacy_writer_lost_authority(self):
        mid = self._insert_user('在吗', created_at='2026-08-04 15:00:00')
        before = aba.read_v3_state(self.db_path)
        ee.apply_desire_delta(0.5, 0.5)
        ee.apply_desire_delta_async(0.5, 0.5)
        after = aba.read_v3_state(self.db_path)
        self.assertEqual(before['passion'], after['passion'])
        self.assertEqual(before['intimacy'], after['intimacy'])
        self.assertEqual(before['state_version'], after['state_version'])

        # Direct emotion_state UPDATE cannot change facade reads.
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE emotion_state SET pa=0.11, valence=0.11, "
            "sternberg_p=0.88 WHERE id=1"
        )
        conn.commit()
        conn.close()
        state = ee.get_state()
        self.assertNotAlmostEqual(state['pa'], 0.11, places=2)
        self.assertNotAlmostEqual(state['sternberg_p'], 0.88, places=2)
        del mid

    def test_case7_no_drive_product_semantics_via_facade(self):
        # Production Drive authority remains drive_state; Stage C must not
        # rewrite that table even while V3 Bond/Affect advance.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        before = dict(conn.execute('SELECT * FROM drive_state WHERE id=1').fetchone())
        conn.close()
        mid = self._insert_user('抱抱', created_at='2026-08-04 16:10:00')
        aba.apply_scored_observation(
            message_id=mid,
            scores={
                'valence': 0.75, 'arousal': 0.4, 'mood_word': '暖',
                'passion_delta': 0.01, 'intimacy_delta': 0.02,
                'source': 't',
            },
            scored_at='2026-08-04 16:10:05', db_path=self.db_path,
        )
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        after = dict(conn.execute('SELECT * FROM drive_state WHERE id=1').fetchone())
        conn.close()
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
