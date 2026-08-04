"""ISV3-1B Stage D｜Eight Drives + Mutation Authority — minimum acceptance cases."""

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
import drive_engine as de
import emotion_engine as ee
import internal_state as isv3
import internal_state_events as events
import internal_state_shadow as shadow
import internal_state_store as store
from chat import affect_bond_authority as aba
from chat import drive_authority as da

T_BOOT = datetime.datetime(2026, 8, 4, 9, 0, 0)
T_BOOT_STR = '2026-08-04 09:00:00'
WATERMARK_MID = 20
SHADOW_ON = {shadow.SHADOW_ENABLED_ENV: '1'}
DRIVE_KEYS = list(isv3.DRIVE_KEYS)


def _seed_legacy(conn: sqlite3.Connection, *, chat_through: int = WATERMARK_MID) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS wake_log (
            id INTEGER PRIMARY KEY, action TEXT, woke_at TEXT
        );
        CREATE TABLE IF NOT EXISTS emotion_state (
            id INTEGER PRIMARY KEY,
            pa REAL, na REAL, valence REAL, arousal REAL,
            mood_word TEXT, longing REAL,
            last_interaction TEXT, updated_at TEXT,
            sternberg_i REAL, sternberg_p REAL, sternberg_c REAL,
            p_updated_at TEXT, i_updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS drive_state (
            id INTEGER PRIMARY KEY,
            attachment REAL, curiosity REAL, reflection REAL, social REAL,
            duty REAL, libido REAL, stress REAL, fatigue REAL,
            last_updated TEXT
        );
        CREATE TABLE IF NOT EXISTS desire_state (
            id INTEGER PRIMARY KEY,
            curiosity REAL, reflection REAL, duty REAL, social REAL,
            libido REAL, stress REAL, fatigue REAL,
            last_updated TEXT, last_hayana_msg_time TEXT
        );
        DELETE FROM chat_messages;
        DELETE FROM wake_log;
        DELETE FROM emotion_state;
        DELETE FROM drive_state;
        DELETE FROM desire_state;
        INSERT INTO emotion_state VALUES (
            1, 0.5, 0.2, 0.6, 0.3, '平静', 0.1,
            '2026-08-04 08:00:00', '2026-08-04 08:00:00',
            0.3, 0.0, 0.7, '2026-08-04 08:00:00', '2026-08-04 08:00:00');
        INSERT INTO drive_state VALUES (
            1, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.22,
            '2026-08-04 08:00:00');
        INSERT INTO desire_state VALUES (
            1, 0.35, 0.30, 0.20, 0.25, 0.15, 0.10, 0.22,
            '2026-08-04 08:00:00', NULL);
        """
    )
    for mid in range(1, chat_through + 1):
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) "
            "VALUES (?,?,?,?)",
            (mid, 'hayana', f'm{mid}', '2026-08-04 08:00:00'),
        )
    conn.commit()


def _production_bootstrap(db_path: str, *, watermark: int = WATERMARK_MID) -> None:
    conn = store.open_store(db_path)
    try:
        _seed_legacy(conn, chat_through=watermark)
        shadow.ensure_shadow_schema(conn)
        conn.execute('BEGIN')
        try:
            shadow.record_score_proof_in_txn(
                conn, watermark, applied_at=T_BOOT_STR,
                source='stage_d_test', score_hash='boot',
            )
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()
    with mock.patch.object(shadow, '_now_beijing_dt', return_value=T_BOOT):
        result = shadow.ensure_bootstrapped(db_path=db_path, environ=SHADOW_ON)
    assert result.ok, result.error
    ready = da.check_cutover_ready(db_path)
    assert ready.ok, (ready.status, ready.error)


class DriveAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self._prev_memories = os.environ.get('MEMORIES_DB')
        self._prev_ee = ee.DB_PATH
        self._prev_de = de.DB_PATH
        self._prev_des = desire.DB_PATH
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        ee.DB_PATH = self.db_path
        de.DB_PATH = self.db_path
        desire.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        self.assertTrue(da.ensure_authority_ready(self.db_path))

    def tearDown(self):
        self._env.stop()
        ee.DB_PATH = self._prev_ee
        de.DB_PATH = self._prev_de
        desire.DB_PATH = self._prev_des
        if self._prev_memories is None:
            os.environ.pop('MEMORIES_DB', None)
        else:
            os.environ['MEMORIES_DB'] = self._prev_memories
        self.tmp.cleanup()

    def _v3(self):
        return da.read_v3_state(self.db_path)

    def _set_legacy_drives(self, **values):
        cols = ', '.join(f'{k}=?' for k in values)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            f'UPDATE drive_state SET {cols} WHERE id=1',
            tuple(values.values()),
        )
        conn.commit()
        conn.close()

    def _set_legacy_desire(self, **values):
        cols = ', '.join(f'{k}=?' for k in values)
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            f'UPDATE desire_state SET {cols} WHERE id=1',
            tuple(values.values()),
        )
        conn.commit()
        conn.close()

    def test_case1_eight_drives_single_authority(self):
        """CASE 1: production read from V3; legacy snapshot poison is ignored."""
        before = de.get_drive()
        for key in DRIVE_KEYS:
            self.assertIn(key, before)

        poison = {k: 0.99 for k in DRIVE_KEYS}
        poison['last_updated'] = '2000-01-01 00:00:00'
        self._set_legacy_drives(**poison)
        self._set_legacy_desire(**{
            k: 0.99 for k in (
                'curiosity', 'reflection', 'duty', 'social',
                'libido', 'stress', 'fatigue',
            )
        })

        after = de.get_drive()
        desire_after = desire.get_drive()
        for key in DRIVE_KEYS:
            self.assertAlmostEqual(after[key], before[key], places=4, msg=key)
            self.assertNotAlmostEqual(after[key], 0.99, places=2, msg=key)
        for key in desire_after:
            self.assertAlmostEqual(
                desire_after[key], before[key], places=4, msg=f'desire:{key}',
            )

    def test_case2_time_evolution_unique(self):
        """CASE 2: same elapsed time applied once by V3 math only."""
        observed = '2026-08-04 15:00:00'  # +6h from bootstrap drives_updated_at
        v3 = self._v3()
        auth = da.read_current_drives(self.db_path, observed_at=observed)
        self.assertIsNotNone(auth)

        bond = events.materialize_bond(v3, observed)
        expected = events.materialize_drives(
            v3,
            observed_at=observed,
            longing_for_boost=0.0,
            passion_for_boost=float(bond['passion']),
        )
        # Facade uses derived longing; pin longing to 0 for exact match via
        # direct materialize equality against a second independent call.
        again = events.materialize_drives(
            v3,
            observed_at=observed,
            longing_for_boost=0.0,
            passion_for_boost=float(bond['passion']),
        )
        for key in DRIVE_KEYS:
            self.assertAlmostEqual(expected[key], again[key], places=4, msg=key)

        # Legacy last_updated far in the past must not create a second grow.
        self._set_legacy_drives(last_updated='2000-01-01 00:00:00', curiosity=0.01)
        with mock.patch.object(da, '_longing_for_boost', return_value=0.0):
            facaded = da.read_current_drives(self.db_path, observed_at=observed)
        self.assertIsNotNone(facaded)
        for key in DRIVE_KEYS:
            self.assertAlmostEqual(facaded[key], expected[key], places=4, msg=key)

        # Read-only: calling twice does not compound growth against stored base.
        with mock.patch.object(da, '_longing_for_boost', return_value=0.0):
            first = da.read_current_drives(self.db_path, observed_at=observed)
            second = da.read_current_drives(self.db_path, observed_at=observed)
        self.assertEqual(first, second)

    def test_case3_legacy_writers_disarmed(self):
        """CASE 3: flush/rest/discharge/satisfy cannot bypass V3."""
        before = dict(self._v3())
        before_prod = de.get_drive()

        de.rest()
        de.discharge('curiosity')
        de.discharge_by_action('explore')
        de._flush({k: 0.01 for k in DRIVE_KEYS})
        desire.calibrate_va(0.1, 0.9)
        desire.satisfy('tease')

        after = self._v3()
        after_prod = de.get_drive()
        for key in DRIVE_KEYS:
            self.assertAlmostEqual(
                float(after[key]), float(before[key]), places=4, msg=key,
            )
            self.assertAlmostEqual(
                after_prod[key], before_prod[key], places=4, msg=f'prod:{key}',
            )

    def test_case4_settlement_through_v3_no_reverse_intent(self):
        """CASE 4: Wake outcome mutates V3; does not reverse-infer Intent."""
        before = float(self._v3()['curiosity'])
        result = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-wake-1',
            executor_action='explore',
            desire_action=None,
            fired_drive='curiosity',
            desire_driven=False,
            user_idle_hours=1.0,
            outcome_at='2026-08-04 10:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(result.status, 'applied')
        after = float(self._v3()['curiosity'])
        self.assertLess(after, before)

        # Missing fired_drive for non-none must fail closed (no reverse guess).
        with self.assertRaises(store.StoreError):
            da.apply_wake_outcome_observation(
                wake_run_id='stage-d-wake-missing',
                executor_action='message',
                desire_action=None,
                fired_drive=None,
                desire_driven=False,
                user_idle_hours=1.0,
                outcome_at='2026-08-04 10:05:00',
                db_path=self.db_path,
            )

    def test_case5_duplicate_and_stale_user_rule_drive(self):
        """CASE 5: duplicate user_rule does not second-settle drives."""
        mid = WATERMARK_MID + 1
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) "
            "VALUES (?,?,?,?)",
            (mid, 'hayana', '抱抱我', '2026-08-04 11:00:00'),
        )
        conn.commit()
        conn.close()

        first = aba.apply_user_rule_observation(
            message_id=mid,
            text='抱抱我',
            created_at='2026-08-04 11:00:00',
            previous_user_at='2026-08-04 08:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(first.status, 'applied')
        after_first = {k: float(self._v3()[k]) for k in DRIVE_KEYS}

        second = aba.apply_user_rule_observation(
            message_id=mid,
            text='抱抱我',
            created_at='2026-08-04 11:00:00',
            previous_user_at='2026-08-04 08:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(second.status, 'duplicate')
        after_second = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        self.assertEqual(after_first, after_second)

        # Duplicate wake_outcome likewise.
        r1 = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-dup',
            executor_action='none',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=0.5,
            outcome_at='2026-08-04 12:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(r1.status, 'applied')
        fatigue_1 = float(self._v3()['fatigue'])
        r2 = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-dup',
            executor_action='none',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=0.5,
            outcome_at='2026-08-04 12:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(r2.status, 'duplicate')
        self.assertAlmostEqual(float(self._v3()['fatigue']), fatigue_1, places=4)

    def test_case6_bond_not_aliased_to_drives(self):
        """CASE 6: legacy Bond intimacy/passion ≠ attachment/libido aliases."""
        before = de.get_drive()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE emotion_state SET sternberg_i=0.99, sternberg_p=0.99 "
            "WHERE id=1"
        )
        conn.commit()
        conn.close()

        after = de.get_drive()
        self.assertAlmostEqual(after['attachment'], before['attachment'], places=4)
        self.assertAlmostEqual(after['libido'], before['libido'], places=4)

        # Direct V3 Bond field edit without Canonical Event must not be a
        # drive alias write path either (production writers are events).
        conn = store.open_store(self.db_path)
        try:
            conn.execute(
                "UPDATE internal_state_v3 SET intimacy=0.99, passion=0.99 "
                "WHERE id=1"
            )
            conn.commit()
        finally:
            conn.close()
        # Raw attachment/libido bases unchanged by intimacy/passion columns.
        v3 = self._v3()
        self.assertNotAlmostEqual(float(v3['attachment']), 0.99, places=2)
        # libido base column independent of passion column
        self.assertNotEqual(
            round(float(v3['libido']), 4),
            round(float(v3['passion']), 4),
        )

    def test_case7_projection_one_way(self):
        """CASE 7: V3 → legacy projection ok; legacy → V3 truth blocked."""
        da.project_v3_drive_compatibility(self.db_path)
        v3 = self._v3()
        conn = sqlite3.connect(self.db_path)
        row = conn.execute('SELECT attachment, curiosity FROM drive_state WHERE id=1').fetchone()
        conn.close()
        self.assertAlmostEqual(float(row[0]), float(v3['attachment']), places=4)
        self.assertAlmostEqual(float(row[1]), float(v3['curiosity']), places=4)

        self._set_legacy_drives(attachment=0.01, curiosity=0.01, fatigue=0.99)
        prod = de.get_drive()
        self.assertNotAlmostEqual(prod['attachment'], 0.01, places=2)
        self.assertNotAlmostEqual(prod['curiosity'], 0.01, places=2)
        self.assertNotAlmostEqual(prod['fatigue'], 0.99, places=2)
        v3_after = self._v3()
        self.assertAlmostEqual(
            float(v3_after['attachment']), float(v3['attachment']), places=4,
        )


if __name__ == '__main__':
    unittest.main()
