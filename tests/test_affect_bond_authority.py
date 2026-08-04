"""ISV3-1B Stage C｜Affect + Bond Authority — narrow acceptance cases."""

from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
import internal_state_shadow as shadow
import internal_state_store as store
from chat import affect_bond_authority as aba

T_BOOT = datetime.datetime(2026, 8, 4, 9, 0, 0)
T_BOOT_STR = '2026-08-04 09:00:00'
WATERMARK_MID = 20
SHADOW_ON = {shadow.SHADOW_ENABLED_ENV: '1'}


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
            1, 0.1, 0.2, 0.1, 0.1, 0.15, 0.0, 0.1, 0.2,
            '2026-08-04 08:00:00');
        INSERT INTO desire_state VALUES (
            1, 0.1, 0.1, 0.15, 0.1, 0.0, 0.1, 0.2,
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
                source='stage_c_test', score_hash='boot',
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
    ready = aba.check_cutover_ready(db_path)
    assert ready.ok, (ready.status, ready.error)


class AffectBondAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'memories.db')
        self._prev_memories = os.environ.get('MEMORIES_DB')
        self._prev_ee = ee.DB_PATH
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        ee.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)
        self.assertTrue(aba.ensure_authority_ready(self.db_path))

    def tearDown(self):
        self._env.stop()
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
        bond = aba.read_current_bond(
            self.db_path, observed_at='2026-08-04 11:00:00',
        )
        self.assertIsNotNone(bond)
        auth_i = float(bond['intimacy'])
        auth_p = float(bond['passion'])
        self.assertGreater(auth_i, 0.3)

        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE emotion_state SET sternberg_i=0.01, sternberg_p=0.99, "
            "sternberg_c=0.01 WHERE id=1"
        )
        conn.commit()
        conn.close()

        with mock.patch.object(aba, '_now_str', return_value='2026-08-04 11:00:00'):
            desire = ee.get_desire()
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
        mid = self._insert_user('嗯', created_at='2026-08-04 16:00:00')
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.0,
            'arousal': 0.2,
            'mood_word': '平静',
            'passion_delta': 0.0,
            'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(0.9, 0.9)), \
                mock.patch.object(ee, '_now_str', return_value='2026-08-04 16:00:05'):
            ee.score_and_update('excerpt', message_id=mid)
        v3 = aba.read_v3_state(self.db_path)
        self.assertAlmostEqual(float(v3['valence']), 0.62, places=2)
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
        """Bond facade must not invent Drive aliases (Stage D projection ok).

        Stage C originally asserted drive_state was untouched. Stage D projects
        V3 drive bases into drive_state after user_rule; that is one-way
        compatibility, not Bond→Drive product semantics.
        """
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
        desire = ee.get_desire()
        self.assertEqual(set(desire.keys()), {'p', 'i', 'c'})
        v3 = aba.read_v3_state(self.db_path)
        self.assertIsNotNone(v3)
        # No field alias: Bond intimacy/passion ≠ Drive attachment/libido.
        self.assertNotAlmostEqual(
            float(v3['intimacy']), float(v3['attachment']), places=4,
        )
        self.assertNotAlmostEqual(
            float(v3['passion']), float(v3['libido']), places=4,
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            'SELECT attachment, libido FROM drive_state WHERE id=1'
        ).fetchone()
        conn.close()
        self.assertAlmostEqual(float(row[0]), float(v3['attachment']), places=4)
        self.assertAlmostEqual(float(row[1]), float(v3['libido']), places=4)


class CutoverGateTests(unittest.TestCase):
    """Blocker 1 — fail-closed cutover / pure-read getter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'gate.db')
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        self._prev_ee = ee.DB_PATH
        ee.DB_PATH = self.db_path

    def tearDown(self):
        ee.DB_PATH = self._prev_ee
        self._env.stop()
        self.tmp.cleanup()

    def test_untrusted_bootstrap_refused(self):
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy(conn)
            shadow.ensure_shadow_schema(conn)
        finally:
            conn.close()
        snap = SimpleNamespace(
            observed_at=T_BOOT_STR,
            affect=SimpleNamespace(
                pa=0.55, na=0.25, valence=0.6, arousal=0.4, mood_word='平静'),
            bond=SimpleNamespace(intimacy=0.5, passion=0.6, commitment=0.7),
            candidate_unified_drives=SimpleNamespace(
                attachment=0.5, curiosity=0.2, reflection=0.3, social=0.1,
                duty=0.15, libido=0.1, stress=0.2, fatigue=0.4),
            diagnostics=SimpleNamespace(
                source_timestamps={},
                source_health={
                    'clock_reliable': True, 'clock_reason': 'ok',
                    'emotion_state': True, 'drive_state': True,
                    'desire_state': True,
                },
                warnings=(),
            ),
        )
        r = shadow._ensure_bootstrapped_for_test(
            db_path=self.db_path, environ=SHADOW_ON,
            snapshot=snap, last_scored_message_id=WATERMARK_MID,
        )
        self.assertTrue(r.ok)
        ready = aba.check_cutover_ready(self.db_path)
        self.assertFalse(ready.ok)
        self.assertEqual(ready.status, 'bootstrap_provenance_invalid')
        self.assertFalse(aba.ensure_authority_ready(self.db_path))
        # Getter must not bootstrap / wash — and must not treat untrusted as authority.
        before = aba.read_v3_state(self.db_path)
        self.assertIsNotNone(before)
        state = ee.get_state()
        self.assertAlmostEqual(state['pa'], 0.5, places=3)
        after = aba.read_v3_state(self.db_path)
        self.assertEqual(before['state_version'], after['state_version'])

    def test_unresolved_gap_refused(self):
        _production_bootstrap(self.db_path)
        conn = store.open_store(self.db_path)
        try:
            shadow.mark_proof_gap(
                conn, failed_message_id=99, error_code='unit_gap',
                db_path=self.db_path,
            )
            conn.commit()
        finally:
            conn.close()
        ready = aba.check_cutover_ready(self.db_path)
        self.assertFalse(ready.ok)
        self.assertEqual(ready.status, 'proof_gap')
        self.assertFalse(aba.ensure_authority_ready(self.db_path))

    def test_missing_legacy_emotion_no_default_wash(self):
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy(conn)
            shadow.ensure_shadow_schema(conn)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, WATERMARK_MID, applied_at=T_BOOT_STR,
                source='stage_c_test', score_hash='boot',
            )
            conn.execute('COMMIT')
            conn.execute('DELETE FROM emotion_state')
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(shadow, '_now_beijing_dt', return_value=T_BOOT):
            r = shadow.ensure_bootstrapped(db_path=self.db_path, environ=SHADOW_ON)
        self.assertFalse(r.ok)
        self.assertIsNone(aba.read_v3_state(self.db_path))
        self.assertFalse(aba.ensure_authority_ready(self.db_path))
        conn = store.open_store(self.db_path)
        try:
            self.assertIsNone(store.read_state(conn))
            self.assertIsNone(store.read_event(conn, 'bootstrap:initial'))
        finally:
            conn.close()

    def test_chat_history_without_proof_delayed_score_not_resurrected(self):
        # Chat history exists; no score proof → refuse cutover (no greenfield wash).
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy(conn, chat_through=100)
            store.ensure_schema(conn)
        finally:
            conn.close()
        self.assertFalse(aba.ensure_authority_ready(self.db_path))
        self.assertIsNone(aba.read_v3_state(self.db_path))

        # After a proper bootstrap at watermark=100, delayed mid=50 is stale.
        conn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(conn)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 100, applied_at=T_BOOT_STR,
                source='stage_c_test', score_hash='boot100',
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        with mock.patch.object(shadow, '_now_beijing_dt', return_value=T_BOOT):
            r = shadow.ensure_bootstrapped(db_path=self.db_path, environ=SHADOW_ON)
        self.assertTrue(r.ok, r.error)
        stale = aba.apply_scored_observation(
            message_id=50,
            scores={
                'valence': 0.1, 'arousal': 0.1, 'mood_word': '旧',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at='2026-08-04 10:00:00',
            db_path=self.db_path,
        )
        self.assertEqual(stale.status, 'stale_skipped')
        v3 = aba.read_v3_state(self.db_path)
        self.assertEqual(int(v3['last_scored_message_id']), 100)

    def test_read_v3_state_is_pure_read(self):
        self.assertIsNone(aba.read_v3_state(self.db_path))
        # Must not create schema/state as a side effect of get_state / read.
        ee.get_state()
        self.assertIsNone(aba.read_v3_state(self.db_path))
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertNotIn('internal_state_v3', tables)


class UserRuleVersionRetryTests(unittest.TestCase):
    """Blocker 2 — user_rule version_conflict limited retry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'retry.db')
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        ee.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_version_conflict_retries_then_applies_once(self):
        mid = 101
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO chat_messages (id, author, content, created_at) "
            "VALUES (?,?,?,?)",
            (mid, 'hayana', '想你', '2026-08-04 14:00:00'),
        )
        conn.commit()
        conn.close()

        real_observe = events.observe_user_message
        calls = {'n': 0}

        def flaky(conn, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                return store.ApplyResult(
                    status='version_conflict',
                    state_version_before=0,
                    state_version_after=None,
                    event_id=None,
                    error='version conflict: expected 0, current 1',
                )
            return real_observe(conn, **kwargs)

        before = aba.read_v3_state(self.db_path)
        with mock.patch.object(events, 'observe_user_message', side_effect=flaky):
            result = aba.apply_user_rule_observation(
                message_id=mid,
                text='想你',
                created_at='2026-08-04 14:00:00',
                previous_user_at='2026-08-04 08:00:00',
                db_path=self.db_path,
            )
        self.assertEqual(result.status, 'applied')
        self.assertEqual(calls['n'], 2)
        after = aba.read_v3_state(self.db_path)
        self.assertEqual(int(after['state_version']), int(before['state_version']) + 1)
        conn = store.open_store(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key=?",
                (f'user_rule:{mid}',),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 1)


class DesireDelegationTests(unittest.TestCase):
    """Blocker 3 — get_desire delegates canonical Bond materialization."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'desire.db')
        self._env = mock.patch.dict(os.environ, {
            'MEMORIES_DB': self.db_path,
            **SHADOW_ON,
        }, clear=False)
        self._env.start()
        self._prev_ee = ee.DB_PATH
        ee.DB_PATH = self.db_path
        _production_bootstrap(self.db_path)

    def tearDown(self):
        ee.DB_PATH = self._prev_ee
        self._env.stop()
        self.tmp.cleanup()

    def test_get_desire_delegates_to_materialize_bond(self):
        sentinel = {
            'passion': 0.424, 'intimacy': 0.313, 'commitment': 0.707,
        }
        with mock.patch(
            'internal_state_events.materialize_bond',
            return_value=sentinel,
        ) as mat:
            desire = ee.get_desire()
        mat.assert_called()
        self.assertEqual(desire['p'], 0.424)
        self.assertEqual(desire['i'], 0.313)
        self.assertEqual(desire['c'], 0.707)


if __name__ == '__main__':
    unittest.main()
