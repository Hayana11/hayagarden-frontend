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


class DecisionTimeProvenanceTests(unittest.TestCase):
    """Stage D R3 — Decision-time provenance contract (P1–P4)."""

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

    def test_p1_no_action_to_drive_inference_in_production_path(self):
        """CASE P1: production Wake path must not derive provenance from Action."""
        gateway_src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        block = gateway_src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertNotIn('infer_fired_drive_for_action', block)
        self.assertIn('decision_provenance', block)
        self.assertIn("primary_drive", block)
        # Retired helper must not invent provenance from Action.
        self.assertIsNone(de.infer_fired_drive_for_action('message'))
        self.assertIsNone(de.infer_fired_drive_for_action('explore'))
        self.assertIsNone(de.infer_fired_drive_for_action('none'))

    def test_p2_decision_time_provenance_survives_post_action_state_shift(self):
        """CASE P2: frozen primary_drive survives later drive-state changes."""
        decision = {
            'fired': 'curiosity',
            'action': 'explore',
            'hint': 'x',
            'blocked': False,
            'drive': {
                'attachment': 0.2, 'curiosity': 0.80, 'reflection': 0.2,
                'social': 0.2, 'duty': 0.2, 'libido': 0.1, 'stress': 0.1,
                'fatigue': 0.2,
            },
            'contributors': [],
        }
        provenance = de.freeze_decision_provenance(decision)
        self.assertEqual(provenance['source'], 'drive_engine.decide')
        self.assertEqual(provenance['primary_drive'], 'curiosity')
        self.assertIsInstance(provenance['captured_at'], str)

        # Final Action is message, but Settlement must still discharge curiosity
        # (frozen P), not re-infer from Action.
        result = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-p2',
            executor_action='message',
            desire_action=None,
            fired_drive=provenance['primary_drive'],
            desire_driven=False,
            user_idle_hours=1.0,
            outcome_at='2026-08-04 10:30:00',
            db_path=self.db_path,
        )
        self.assertEqual(result.status, 'applied')
        diag = result.result or {}
        before = diag['materialized_before']
        after_fixed = diag['legacy_fixed_after']
        self.assertLess(after_fixed['curiosity'], before['curiosity'])
        self.assertAlmostEqual(after_fixed['social'], before['social'], places=4)
        self.assertAlmostEqual(
            after_fixed['curiosity'],
            max(0.0, before['curiosity'] - 0.45),
            places=4,
        )

    def test_p3_same_action_different_provenance_metamorphic(self):
        """CASE P3: same Action + different P → different Settlement targets."""
        r1 = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-p3-att',
            executor_action='message',
            desire_action=None,
            fired_drive='attachment',
            desire_driven=False,
            user_idle_hours=0.5,
            outcome_at='2026-08-04 13:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(r1.status, 'applied')
        d1 = r1.result or {}
        self.assertLess(
            d1['legacy_fixed_after']['attachment'],
            d1['materialized_before']['attachment'],
        )
        self.assertAlmostEqual(
            d1['legacy_fixed_after']['social'],
            d1['materialized_before']['social'],
            places=4,
        )

        r2 = da.apply_wake_outcome_observation(
            wake_run_id='stage-d-p3-soc',
            executor_action='message',
            desire_action=None,
            fired_drive='social',
            desire_driven=False,
            user_idle_hours=0.5,
            outcome_at='2026-08-04 13:05:00',
            db_path=self.db_path,
        )
        self.assertEqual(r2.status, 'applied')
        d2 = r2.result or {}
        self.assertLess(
            d2['legacy_fixed_after']['social'],
            d2['materialized_before']['social'],
        )
        self.assertAlmostEqual(
            d2['legacy_fixed_after']['attachment'],
            d2['materialized_before']['attachment'],
            places=4,
        )

    def test_p4_missing_provenance_fail_closed(self):
        """CASE P4: non-none Action without provenance must not mutate V3."""
        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        ok = da.apply_wake_outcome_best_effort(
            wake_run_id='stage-d-p4',
            executor_action='message',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=1.0,
            outcome_at='2026-08-04 14:00:00',
            db_path=self.db_path,
        )
        self.assertFalse(ok)
        after = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        self.assertEqual(before, after)
        # No wake_outcome event consumed.
        conn = store.open_store(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:stage-d-p4'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row)


class StageDFinalWiringTests(unittest.TestCase):
    """Production wiring blockers after R3 provenance primitive PASS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self._prev_memories = os.environ.get('MEMORIES_DB')
        self._prev_ee = ee.DB_PATH
        self._prev_de = de.DB_PATH
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        ee.DB_PATH = self.db_path
        de.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        # Replace bootstrap stub wake_log with executor-capable schema.
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            DROP TABLE IF EXISTS wake_log;
            DROP TABLE IF EXISTS posts;
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thoughts TEXT, action TEXT, content TEXT, consumed INTEGER,
                woke_at TEXT, wake_run_id TEXT, notified INTEGER,
                chat_id TEXT, context_id INTEGER, context_epoch INTEGER,
                resident_generation INTEGER, cache_info TEXT,
                surfaced_desire_ids TEXT
            );
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT, content TEXT, layer TEXT, author TEXT, processed INTEGER
            );
            """
        )
        cols = {r[1] for r in conn.execute('PRAGMA table_info(chat_messages)')}
        for col, typ in (
            ('thinking', 'TEXT'),
            ('cache_info', 'TEXT'),
            ('source_kind', 'TEXT'),
        ):
            if col not in cols:
                conn.execute(f'ALTER TABLE chat_messages ADD COLUMN {col} {typ}')
        conn.commit()
        conn.close()
        self.assertTrue(da.ensure_authority_ready(self.db_path))

    def tearDown(self):
        self._env.stop()
        ee.DB_PATH = self._prev_ee
        de.DB_PATH = self._prev_de
        if self._prev_memories is None:
            os.environ.pop('MEMORIES_DB', None)
        else:
            os.environ['MEMORIES_DB'] = self._prev_memories
        self.tmp.cleanup()

    def _get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _v3(self):
        return da.read_v3_state(self.db_path)

    def test_b1_stale_window_does_not_settle(self):
        """Blocker 1: gate-blocked Action must not mutate V3 drives."""
        from wake.executor import execute
        import chat.window_identity as wi

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        identity = {
            'chat_id': 'c1', 'context_id': 1,
            'context_epoch': 1, 'resident_generation': 1,
        }
        with mock.patch.object(wi, 'soft_window_enabled', return_value=True), \
             mock.patch.object(
                 wi, 'gate_captured_against_conn',
                 return_value=(wi.REASON_STALE, identity, None),
             ), mock.patch.object(wi, 'ensure_wake_window_identity_columns'):
            out = execute(
                'message', 't', 'hello', 'normal', self._get_db,
                wake_run_id='b1-stale',
                window_identity=identity,
                settle_fired_drive='curiosity',
                settle_user_idle_hours=1.0,
                settle_outcome_at='2026-08-04 15:00:00',
            )
        self.assertFalse(out['delivered'])
        self.assertFalse(out['settled'])
        after = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        self.assertEqual(before, after)
        conn = sqlite3.connect(self.db_path)
        ev = conn.execute(
            "SELECT 1 FROM internal_state_events "
            "WHERE event_key='wake_outcome:b1-stale'"
        ).fetchone()
        msgs = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='fyodor'"
        ).fetchone()[0]
        conn.close()
        self.assertIsNone(ev)
        self.assertEqual(msgs, 0)

    def test_b1_settle_failure_rolls_back_action(self):
        """Blocker 1: Action + Settlement atomic — settle fail ⇒ no Action."""
        from wake.executor import execute

        before_cur = float(self._v3()['curiosity'])
        with mock.patch(
            'chat.drive_authority.apply_wake_outcome_on_conn',
            side_effect=RuntimeError('simulated settle fault'),
        ):
            with self.assertRaises(RuntimeError):
                execute(
                    'message', 't', 'hello-atomic', 'normal', self._get_db,
                    wake_run_id='b1-atomic',
                    settle_fired_drive='curiosity',
                    settle_user_idle_hours=1.0,
                    settle_outcome_at='2026-08-04 15:10:00',
                )
        conn = sqlite3.connect(self.db_path)
        msgs = conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE content='hello-atomic'"
        ).fetchone()[0]
        wakes = conn.execute(
            "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b1-atomic'"
        ).fetchone()[0]
        ev = conn.execute(
            "SELECT 1 FROM internal_state_events "
            "WHERE event_key='wake_outcome:b1-atomic'"
        ).fetchone()
        conn.close()
        self.assertEqual(msgs, 0)
        self.assertEqual(wakes, 0)
        self.assertIsNone(ev)
        self.assertAlmostEqual(float(self._v3()['curiosity']), before_cur, places=4)

        # Retry with healthy settle succeeds once (recoverable).
        out = execute(
            'message', 't', 'hello-atomic', 'normal', self._get_db,
            wake_run_id='b1-atomic',
            settle_fired_drive='curiosity',
            settle_user_idle_hours=1.0,
            settle_outcome_at='2026-08-04 15:10:00',
        )
        self.assertTrue(out['delivered'])
        self.assertTrue(out['settled'])
        self.assertLess(float(self._v3()['curiosity']), before_cur)

    def test_b1_normal_path_action_and_event_once(self):
        """Blocker 1: happy path commits Action + wake_outcome once."""
        from wake.executor import execute

        before = float(self._v3()['attachment'])
        out = execute(
            'message', 't', 'once', 'normal', self._get_db,
            wake_run_id='b1-once',
            settle_fired_drive='attachment',
            settle_user_idle_hours=0.5,
            settle_outcome_at='2026-08-04 15:20:00',
        )
        self.assertTrue(out['delivered'])
        self.assertTrue(out['settled'])
        self.assertEqual(out['settle_status'], 'applied')
        self.assertLess(float(self._v3()['attachment']), before)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE content='once'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:b1-once'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_b1_missing_provenance_rolls_back_action(self):
        """Blocker 1: non-none without primary_drive must not commit Action."""
        from wake.executor import execute

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        with self.assertRaises(Exception):
            execute(
                'message', 't', 'no-prov', 'normal', self._get_db,
                wake_run_id='b1-no-prov',
                settle_fired_drive=None,
                settle_provenance_present=True,  # object ok; primary missing
                settle_user_idle_hours=1.0,
                settle_outcome_at='2026-08-04 15:30:00',
            )
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE content='no-prov'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b1-no-prov'"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:b1-no-prov'"
            ).fetchone()
        )
        conn.close()
        self.assertEqual(before, {k: float(self._v3()[k]) for k in DRIVE_KEYS})

    def test_b1_message_empty_content_no_settlement(self):
        """Narrow B1: message + empty CONTENT must not settle unexecuted Action."""
        from wake.executor import execute

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        with self.assertRaises(RuntimeError):
            execute(
                'message', 't', '', 'normal', self._get_db,
                wake_run_id='b1-empty-msg',
                settle_fired_drive='curiosity',
                settle_user_idle_hours=1.0,
                settle_outcome_at='2026-08-04 15:40:00',
            )
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b1-empty-msg'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='fyodor'"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:b1-empty-msg'"
            ).fetchone()
        )
        conn.close()
        self.assertEqual(before, {k: float(self._v3()[k]) for k in DRIVE_KEYS})

    def test_b1_diary_empty_content_no_settlement(self):
        """Narrow B1: diary + empty CONTENT must not settle unexecuted Action."""
        from wake.executor import execute

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        with self.assertRaises(RuntimeError):
            execute(
                'diary', 't', '   ', 'normal', self._get_db,
                wake_run_id='b1-empty-diary',
                settle_fired_drive='reflection',
                settle_user_idle_hours=1.0,
                settle_outcome_at='2026-08-04 15:45:00',
            )
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='b1-empty-diary'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:b1-empty-diary'"
            ).fetchone()
        )
        conn.close()
        self.assertEqual(before, {k: float(self._v3()[k]) for k in DRIVE_KEYS})

    def test_n3_live_wake_requires_wake_run_id_before_model(self):
        """N3: pure gate + gateway source order (no Flask/gateway import)."""
        from wake.wake_run_id import missing_live_wake_run_id

        self.assertTrue(
            missing_live_wake_run_id('normal', dry_run=False, wake_run_id='')
        )
        self.assertTrue(
            missing_live_wake_run_id('morning', dry_run=False, wake_run_id='  ')
        )
        self.assertTrue(
            missing_live_wake_run_id(
                'nightwatch', dry_run=False, wake_run_id='',
            )
        )
        self.assertFalse(
            missing_live_wake_run_id(
                'normal', dry_run=False, wake_run_id='normal-2026-08-04-10:00',
            )
        )
        self.assertFalse(
            missing_live_wake_run_id('normal', dry_run=True, wake_run_id='')
        )
        self.assertFalse(
            missing_live_wake_run_id('dream', dry_run=False, wake_run_id='')
        )
        self.assertFalse(
            missing_live_wake_run_id('summarize', dry_run=False, wake_run_id='')
        )

        block = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        self.assertIn('missing_live_wake_run_id', block)
        self.assertIn("'reason': 'missing_wake_run_id'", block)
        self.assertLess(
            block.index('missing_live_wake_run_id'),
            block.index('get_wake_runner'),
        )
        self.assertLess(
            block.index('missing_live_wake_run_id'),
            block.index('_wake_build_system_for_plan'),
        )
        # Must not re-inline the gate without the helper.
        self.assertEqual(block.count('missing_live_wake_run_id'), 2)

    def test_n4_proof_gap_refuses_txn_settlement(self):
        """N4: unresolved proof gap must fail closed inside Action txn."""
        from wake.executor import execute

        conn = store.open_store(self.db_path)
        try:
            shadow.mark_proof_gap(
                conn, failed_message_id=99, error_code='stage_d_n4_gap',
                db_path=self.db_path,
            )
            conn.commit()
        finally:
            conn.close()
        ready = da.check_cutover_ready(self.db_path)
        self.assertFalse(ready.ok)
        self.assertEqual(ready.status, 'proof_gap')

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        before_ver = int(self._v3()['state_version'])
        with self.assertRaises(Exception):
            execute(
                'message', 't', 'gap-blocked', 'normal', self._get_db,
                wake_run_id='n4-gap',
                settle_fired_drive='curiosity',
                settle_provenance_present=True,
                settle_user_idle_hours=1.0,
                settle_outcome_at='2026-08-04 16:00:00',
            )
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='n4-gap'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE content='gap-blocked'"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:n4-gap'"
            ).fetchone()
        )
        conn.close()
        after = self._v3()
        self.assertEqual(before, {k: float(after[k]) for k in DRIVE_KEYS})
        self.assertEqual(before_ver, int(after['state_version']))

    def test_n5_none_with_valid_null_primary_settles(self):
        """N5: frozen provenance + primary=None + Action=none → settle OK."""
        from wake.executor import execute

        before_fat = float(self._v3()['fatigue'])
        out = execute(
            'none', 't', '', 'normal', self._get_db,
            wake_run_id='n5-none-ok',
            settle_fired_drive=None,
            settle_provenance_present=True,
            settle_user_idle_hours=0.5,
            settle_outcome_at='2026-08-04 16:10:00',
        )
        self.assertTrue(out['delivered'])
        self.assertTrue(out['settled'])
        self.assertEqual(out['settle_status'], 'applied')
        self.assertLess(float(self._v3()['fatigue']), before_fat)
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log WHERE wake_run_id='n5-none-ok'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='wake_outcome:n5-none-ok'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_n5_none_without_provenance_object_fails_closed(self):
        """N5: missing provenance object + Action=none → no mutation."""
        from wake.executor import execute

        before = {k: float(self._v3()[k]) for k in DRIVE_KEYS}
        with self.assertRaises(Exception):
            execute(
                'none', 't', '', 'normal', self._get_db,
                wake_run_id='n5-none-missing',
                settle_fired_drive=None,
                settle_provenance_present=False,
                settle_user_idle_hours=0.5,
                settle_outcome_at='2026-08-04 16:15:00',
            )
        conn = sqlite3.connect(self.db_path)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM wake_log "
                "WHERE wake_run_id='n5-none-missing'"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM internal_state_events "
                "WHERE event_key='wake_outcome:n5-none-missing'"
            ).fetchone()
        )
        conn.close()
        self.assertEqual(before, {k: float(self._v3()[k]) for k in DRIVE_KEYS})

    def test_b3_prompt_has_no_second_desire_drive_decision(self):
        """Blocker 3: frozen drive decision only — longing fact has no behavior hint."""
        from wake.builder import inject_snippets
        import desire as real_desire

        decision = {
            'fired': 'curiosity',
            'action': 'explore',
            'hint': 'x',
            'blocked': False,
            'drive': {
                'attachment': 0.2, 'curiosity': 0.9, 'reflection': 0.1,
                'social': 0.1, 'duty': 0.1, 'libido': 0.0, 'stress': 0.1,
                'fatigue': 0.2,
            },
            'contributors': [],
        }
        drive = mock.Mock()
        drive.decide.return_value = decision
        drive.freeze_decision_provenance.return_value = {
            'source': 'drive_engine.decide',
            'captured_at': '2026-08-04 16:00:00',
            'primary_drive': 'curiosity',
            'contributors': [],
            'blocked': False,
            'suggested_action': 'explore',
        }
        drive.get_wake_snippet.return_value = (
            '## 内在需求（驱动条）\n→ 当前最强需求：curiosity，倾向于 explore 行为。'
        )
        # Real longing fact path (protest phase at 48h) — must not carry
        # LONGING_HINT behavior/style directives.
        with mock.patch.dict(sys.modules, {'drive_engine': drive}):
            system, prov = inject_snippets(
                'base', 'normal',
                desire_driven=True, longing_enabled=True,
                t_hours_override=48.0,
            )
        self.assertEqual(prov['primary_drive'], 'curiosity')
        self.assertIn('当前最强需求：curiosity', system)
        self.assertIn('Longing（思念哈娅）', system)
        self.assertIn('阶段=protest', system)
        # Desire Drive→Action second decision must stay out of prompt.
        self.assertNotIn('当前最强驱动', system)
        self.assertNotIn('倾向于 web_browse', system)
        # Longing fact must not carry LONGING_HINT behavior/style directives.
        longing_forbidden = (
            '主动找话题', '凑近', '话少一些', '倾向于',
            '会主动', '安静等着', '防线会崩塌', '偶尔走神',
        )
        for phrase in longing_forbidden:
            self.assertNotIn(phrase, system.split('## Longing', 1)[-1])
        fact = real_desire.get_longing_wake_fact(t_hours_override=48.0)
        self.assertIn('L=', fact)
        self.assertIn('阶段=protest', fact)
        for phrase in longing_forbidden:
            self.assertNotIn(phrase, fact)
        # Sanity: protest LONGING_HINT still exists in module, but fact omits it.
        self.assertIn('主动找话题', real_desire.LONGING_HINT['protest'])


if __name__ == '__main__':
    unittest.main()
