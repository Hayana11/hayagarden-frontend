"""Phase 1A-4b — user_rule / user_scored 生产接线与三开关。"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import emotion_engine as ee
import internal_state_shadow as shadow
import internal_state_store as store
import moments_turn


T0 = '2026-07-21 12:00:00'
OFF = {
    shadow.SHADOW_ENABLED_ENV: '0',
    shadow.SCORE_PROOF_ENABLED_ENV: '0',
    shadow.USER_EVENTS_ENABLED_ENV: '0',
}
PROOF_ONLY = {
    shadow.SHADOW_ENABLED_ENV: '0',
    shadow.SCORE_PROOF_ENABLED_ENV: '1',
    shadow.USER_EVENTS_ENABLED_ENV: '0',
}
EVENTS_WITHOUT_MASTER = {
    shadow.SHADOW_ENABLED_ENV: '0',
    shadow.SCORE_PROOF_ENABLED_ENV: '0',
    shadow.USER_EVENTS_ENABLED_ENV: '1',
}
EVENTS_WITHOUT_PROOF = {
    shadow.SHADOW_ENABLED_ENV: '1',
    shadow.SCORE_PROOF_ENABLED_ENV: '0',
    shadow.USER_EVENTS_ENABLED_ENV: '1',
}
ALL_ON = {
    shadow.SHADOW_ENABLED_ENV: '1',
    shadow.SCORE_PROOF_ENABLED_ENV: '1',
    shadow.USER_EVENTS_ENABLED_ENV: '1',
}


def _seed_emotion(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS emotion_state (
            id INTEGER PRIMARY KEY,
            pa REAL, na REAL, valence REAL, arousal REAL,
            mood_word TEXT, longing REAL,
            last_interaction TEXT, updated_at TEXT,
            sternberg_i REAL, sternberg_p REAL, sternberg_c REAL,
            p_updated_at TEXT, i_updated_at TEXT
        );
        DELETE FROM emotion_state;
        INSERT INTO emotion_state VALUES (
            1, 0.5, 0.2, 0.6, 0.3, '平静', 0.1,
            '2026-07-21 11:00:00', '2026-07-21 11:00:00',
            0.3, 0.0, 0.7, '2026-07-21 11:00:00', '2026-07-21 11:00:00');
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            author TEXT, content TEXT,
            created_at TEXT DEFAULT (datetime('now', '+8 hours'))
        );
        """
    )


class FlagTruthTableTests(unittest.TestCase):
    def test_defaults_off(self):
        self.assertFalse(shadow.is_shadow_enabled(environ={}))
        self.assertFalse(shadow.is_score_proof_enabled(environ={}))
        self.assertFalse(shadow.is_user_events_enabled(environ={}))

    def test_user_events_requires_master_and_proof(self):
        self.assertFalse(shadow.is_user_events_enabled(environ=EVENTS_WITHOUT_MASTER))
        self.assertFalse(shadow.is_user_events_enabled(environ=EVENTS_WITHOUT_PROOF))
        self.assertTrue(shadow.is_user_events_enabled(environ=ALL_ON))

    def test_all_off_emit_zero_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ghost.db'
            r = shadow.emit_user_rule_if_enabled(
                message_id=1, text='hi', created_at=T0,
                previous_user_at=None, db_path=str(path), environ=OFF,
            )
            self.assertEqual(r.status, 'disabled')
            self.assertFalse(path.exists())
            r2 = shadow.emit_user_scored_if_enabled(
                message_id=1,
                scores={
                    'valence': 0.5, 'arousal': 0.4, 'mood_word': 'x',
                    'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
                },
                scored_at=T0, db_path=str(path), environ=OFF,
            )
            self.assertEqual(r2.status, 'disabled')
            self.assertFalse(path.exists())


class PrepareSchemaTests(unittest.TestCase):
    def test_prepare_schema_idempotent_no_bootstrap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'admin.db')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            before = store.get_journal_mode(conn)
            shadow.ensure_shadow_schema(conn)
            shadow.ensure_shadow_schema(conn)
            self.assertEqual(store.get_journal_mode(conn), before)
            self.assertTrue(shadow.score_proof_schema_ready(conn))
            self.assertIsNone(store.read_state(conn))
            self.assertIsNone(store.read_event(conn, 'bootstrap:initial'))
            # 旧表仍在
            self.assertEqual(
                conn.execute('SELECT COUNT(*) FROM emotion_state').fetchone()[0],
                1,
            )
        finally:
            conn.close()


class ProofAtomicityTests(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'proof.db')
        self._patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        self._patch.start()
        conn = store.open_store(self.db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn)
        finally:
            conn.close()
        ee.DB_PATH = self.db_path

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_emotion_and_proof_commit_together(self):
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.01, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1):
            ee.score_and_update('hello', message_id=7)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5)
            n = conn.execute(
                f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE} WHERE message_id=7'
            ).fetchone()[0]
            self.assertEqual(n, 1)
            # proof-only：不得写任何 shadow 事件
            n_ev = conn.execute(
                'SELECT COUNT(*) FROM internal_state_events'
            ).fetchone()[0]
            self.assertEqual(n_ev, 0)
        finally:
            conn.close()

    def test_proof_failure_rolls_back_emotion(self):
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.01, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'record_score_proof_in_txn',
                 side_effect=store.StoreError('boom'),
             ):
            ee.score_and_update('hello', message_id=8)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertAlmostEqual(pa, 0.5, places=4)
            n = conn.execute(
                f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE}'
            ).fetchone()[0]
            self.assertEqual(n, 0)
            health = shadow.read_proof_health(store.open_store(self.db_path))
        finally:
            conn.close()
        conn = store.open_store(self.db_path)
        try:
            health = shadow.read_proof_health(conn)
            self.assertTrue(health.gap_detected)
            self.assertEqual(health.error_code, 'proof_txn_failed')
        finally:
            conn.close()

    def test_schema_missing_keeps_legacy_and_durable_sidecar_gap(self):
        # 新库：有 emotion，无 proof 表 — gap 必须进 sidecar，prepare 后迁入
        db2 = str(Path(self.tmp.name) / 'nogap.db')
        conn = sqlite3.connect(db2)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db2
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1):
            ee.score_and_update('hello', message_id=9)
        conn = sqlite3.connect(db2)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?",
                (shadow.SCORE_APPLIED_TABLE,),
            ).fetchone())
        finally:
            conn.close()
        side = shadow.read_proof_gap_sidecar(db2)
        self.assertIsNotNone(side)
        self.assertTrue(side.get('gap_detected'))
        self.assertEqual(side.get('error_code'), 'proof_schema_missing')
        self.assertGreaterEqual(shadow.count_gap_sidecar_pending(db2), 1)
        # prepare-schema 不得洗白
        conn = store.open_store(db2)
        try:
            shadow.ensure_shadow_schema(conn, db_path=db2)
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db2))
            self.assertEqual(shadow.count_unresolved_gap_incidents(conn), 1)
            health = shadow.read_proof_health(conn)
            self.assertTrue(health.gap_detected)
            self.assertEqual(health.error_code, 'proof_schema_missing')
        finally:
            conn.close()
        self.assertEqual(shadow.count_gap_sidecar_pending(db2), 0)


class GapBootstrapTests(unittest.TestCase):
    def test_gap_blocks_bootstrap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'gap.db')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn)
            # 最小 legacy 供 bootstrap
            conn.executescript(
                """
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
                DELETE FROM drive_state; DELETE FROM desire_state;
                INSERT INTO drive_state VALUES (
                    1, 0.5, 0.2, 0.3, 0.1, 0.15, 0.1, 0.2, 0.4, '2026-07-21 10:00:00');
                INSERT INTO desire_state VALUES (
                    1, 0.2, 0.3, 0.15, 0.1, 0.1, 0.2, 0.4, '2026-07-21 10:00:00', NULL);
                INSERT INTO chat_messages (author, content, created_at)
                VALUES ('hayana', 'hi', '2026-07-21 11:00:00');
                """
            )
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 5, applied_at=T0, source='unit', score_hash='unit')
            conn.execute('COMMIT')
            shadow.mark_proof_gap(
                conn, failed_message_id=6, error_code='unit_gap')
        finally:
            conn.close()
        r = shadow.ensure_bootstrapped(
            db_path=db_path,
            environ={shadow.SHADOW_ENABLED_ENV: '1'},
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'proof_gap')


class MessageIdThreadTests(unittest.TestCase):
    def test_score_async_freezes_message_id(self):
        seen = {}

        def fake_update(excerpt, *, message_id=None):
            seen['mid'] = message_id
            seen['excerpt'] = excerpt

        with mock.patch.object(ee, 'score_and_update', side_effect=fake_update):
            ee.score_async('abc', message_id=42)
            # 线程可能尚未跑完
            for _ in range(50):
                if 'mid' in seen:
                    break
                threading.Event().wait(0.02)
        self.assertEqual(seen.get('mid'), 42)
        self.assertEqual(seen.get('excerpt'), 'abc')

    def test_reject_bool_message_id_marks_gap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'bool.db')
        import os
        patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn)
        finally:
            conn.close()
        ee.DB_PATH = db_path
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.0, 'arousal': 0.3, 'mood_word': '平',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1):
            ee.score_and_update('x', message_id=True)  # type: ignore[arg-type]
        conn = store.open_store(db_path)
        try:
            health = shadow.read_proof_health(conn)
            self.assertTrue(health.gap_detected)
            self.assertEqual(health.error_code, 'missing_or_invalid_message_id')
            n = conn.execute(
                f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE}'
            ).fetchone()[0]
            self.assertEqual(n, 0)
        finally:
            conn.close()


class UserRulePathTests(unittest.TestCase):
    def test_insert_user_message_enqueues_outbox_when_enabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'user.db')
        enqueued = []

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        conn = get_db()
        try:
            conn.execute(
                "CREATE TABLE chat_messages ("
                "id INTEGER PRIMARY KEY, author TEXT, content TEXT, "
                "created_at TEXT DEFAULT (datetime('now','+8 hours')))"
            )
            conn.execute(
                "INSERT INTO chat_messages (author, content, created_at) "
                "VALUES ('hayana', 'prev', '2026-07-21 10:00:00')"
            )
            conn.commit()
        finally:
            conn.close()
        sconn = store.open_store(db_path)
        try:
            shadow.ensure_shadow_schema(sconn, db_path=db_path)
        finally:
            sconn.close()

        with mock.patch.object(
            shadow, 'enqueue_user_rule_in_txn',
            side_effect=lambda conn, **kw: enqueued.append(kw) or True,
        ), mock.patch.object(
            shadow, 'drain_shadow_outbox_best_effort',
        ) as drain, mock.patch.dict(
            __import__('os').environ, ALL_ON, clear=False,
        ):
            turn = moments_turn.insert_user_message(
                get_db, {}, '你好呀',
                memories_db_path=db_path,
            )
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]['message_id'], turn['user_message_id'])
        self.assertEqual(enqueued[0]['text'], '你好呀')
        self.assertEqual(enqueued[0]['previous_user_at'], '2026-07-21 10:00:00')
        self.assertTrue(enqueued[0]['created_at'])
        drain.assert_called_once()

    def test_insert_user_message_disabled_no_emit_db_for_shadow(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'user2.db')

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        conn = get_db()
        try:
            conn.execute(
                "CREATE TABLE chat_messages ("
                "id INTEGER PRIMARY KEY, author TEXT, content TEXT, "
                "created_at TEXT DEFAULT (datetime('now','+8 hours')))"
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.dict(__import__('os').environ, OFF, clear=False), \
             mock.patch.object(shadow, 'observe_user_message_shadow') as obs, \
             mock.patch.object(shadow, 'enqueue_user_rule_in_txn') as enq:
            moments_turn.insert_user_message(
                get_db, {}, 'hi', memories_db_path=db_path,
            )
            obs.assert_not_called()
            enq.assert_not_called()


class GatewayGuardTests(unittest.TestCase):
    def test_gateway_passes_message_id_to_score_async(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('message_id=_turn_data.get(', src)
        self.assertEqual(src.count('score_async('), 2)

    def test_no_wake_outcome_wiring(self):
        for name in ('gateway.py', 'app.py'):
            text = Path(ROOT, name).read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('apply_outcome_shadow', text)
        wake_dir = Path(ROOT, 'wake')
        if wake_dir.is_dir():
            for path in wake_dir.rglob('*.py'):
                text = path.read_text(encoding='utf-8', errors='replace')
                self.assertNotIn('apply_outcome_shadow', text)

    def test_score_txn_has_no_ensure_schema_call(self):
        src = Path(ROOT, 'emotion_engine.py').read_text(encoding='utf-8')
        # score_and_update 函数体不得出现 ensure_shadow_schema / ensure_schema
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == 'score_and_update':
                body_src = ast.get_source_segment(src, node) or ''
                self.assertNotIn('ensure_shadow_schema', body_src)
                self.assertNotIn('ensure_schema', body_src)


class EndToEndShadowEventTests(unittest.TestCase):
    def test_proof_then_bootstrap_then_events(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'e2e.db')
        import os
        # prepare schema
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            conn.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS wake_log (
                    id INTEGER PRIMARY KEY,
                    action TEXT, woke_at TEXT
                );
                INSERT INTO drive_state VALUES (
                    1, 0.5, 0.2, 0.3, 0.1, 0.15, 0.1, 0.2, 0.4, '2026-07-21 10:00:00');
                INSERT INTO desire_state VALUES (
                    1, 0.2, 0.3, 0.15, 0.1, 0.1, 0.2, 0.4, '2026-07-21 10:00:00', NULL);
                INSERT INTO chat_messages (author, content, created_at)
                VALUES ('hayana', 'hi', '2026-07-21 11:00:00');
                """
            )
            shadow.ensure_shadow_schema(conn)
        finally:
            conn.close()

        ee.DB_PATH = db_path
        with mock.patch.dict(os.environ, PROOF_ONLY, clear=False), \
             mock.patch.object(ee, '_deepseek_score', return_value={
                 'valence': 0.4, 'arousal': 0.5, 'mood_word': '喜',
                 'passion_delta': 0.0, 'intimacy_delta': 0.0,
             }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1):
            ee.score_and_update('turn', message_id=20)

        # bootstrap with master
        r = shadow.ensure_bootstrapped(
            db_path=db_path,
            environ={shadow.SHADOW_ENABLED_ENV: '1'},
        )
        self.assertTrue(r.ok, msg=r.error)
        h = shadow.get_shadow_health(
            db_path=db_path,
            environ={shadow.SHADOW_ENABLED_ENV: '1'},
        )
        self.assertTrue(h.provenance_ok)
        self.assertEqual(h.last_scored_message_id, 20)

        # user events on → scored 21 advances watermark; provenance stays
        conn = store.open_store(db_path)
        try:
            observed = store.read_state(conn)['p_updated_at']
        finally:
            conn.close()
        import datetime as _dt
        base = _dt.datetime.strptime(observed, '%Y-%m-%d %H:%M:%S')
        scored_at = (base + _dt.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        r2 = shadow.emit_user_scored_if_enabled(
            message_id=21,
            scores={
                'valence': 0.7, 'arousal': 0.4, 'mood_word': '开心',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at=scored_at,
            db_path=db_path,
            environ=ALL_ON,
        )
        self.assertEqual(r2.status, 'applied', msg=r2.error)
        h2 = shadow.get_shadow_health(db_path=db_path, environ=ALL_ON)
        self.assertTrue(h2.provenance_ok)
        self.assertEqual(h2.last_scored_message_id, 21)


def _seed_legacy_for_bootstrap(conn: sqlite3.Connection) -> None:
    _seed_emotion(conn)
    conn.executescript(
        """
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
        CREATE TABLE IF NOT EXISTS wake_log (
            id INTEGER PRIMARY KEY,
            action TEXT, woke_at TEXT
        );
        DELETE FROM drive_state; DELETE FROM desire_state;
        INSERT INTO drive_state VALUES (
            1, 0.5, 0.2, 0.3, 0.1, 0.15, 0.1, 0.2, 0.4, '2026-07-21 10:00:00');
        INSERT INTO desire_state VALUES (
            1, 0.2, 0.3, 0.15, 0.1, 0.1, 0.2, 0.4, '2026-07-21 10:00:00', NULL);
        INSERT INTO chat_messages (author, content, created_at)
        VALUES ('hayana', 'hi', '2026-07-21 11:00:00');
        """
    )


class OutboxReliabilityTests(unittest.TestCase):
    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'outbox.db')
        self._patch = mock.patch.dict(os.environ, ALL_ON, clear=False)
        self._patch.start()
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 20, applied_at=T0, source='unit', score_hash='unit')
            conn.execute('COMMIT')
        finally:
            conn.close()
        r = shadow.ensure_bootstrapped(
            db_path=self.db_path,
            environ={shadow.SHADOW_ENABLED_ENV: '1'},
        )
        self.assertTrue(r.ok, msg=r.error)

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()

    def test_failed_emit_keeps_outbox_and_replay_is_idempotent(self):
        import datetime as _dt
        scores = {
            'valence': 0.7, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.01, 'intimacy_delta': 0.0, 'source': 't',
        }
        conn = store.open_store(self.db_path)
        try:
            observed = store.read_state(conn)['p_updated_at']
        finally:
            conn.close()
        base = _dt.datetime.strptime(observed, '%Y-%m-%d %H:%M:%S')
        scored_at = (base + _dt.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        conn = store.open_store(self.db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            shadow.enqueue_user_scored_in_txn(
                conn,
                message_id=101,
                scores=scores,
                scored_at=scored_at,
                environ=ALL_ON,
            )
            # 模拟同事务权威 proof（完整 scores 已在 outbox）
            shadow.record_score_proof_in_txn(
                conn, 101, applied_at=scored_at, source='unit', score_hash='unit')
            conn.execute('COMMIT')
        finally:
            conn.close()

        with mock.patch.object(
            shadow, 'observe_scored_shadow',
            return_value=shadow.ShadowResult(
                ok=False, status='failed', error='simulated'),
        ):
            summary = shadow.drain_shadow_outbox(
                db_path=self.db_path, environ=ALL_ON,
            )
        self.assertEqual(summary['failed'], 1)
        conn = store.open_store(self.db_path)
        try:
            pending = shadow.count_pending_outbox(conn)
            self.assertEqual(pending, 1)
            row = conn.execute(
                f"SELECT payload_json FROM {shadow.OUTBOX_TABLE} "
                "WHERE event_key='user_scored:101'"
            ).fetchone()
            payload = json.loads(row[0])
            self.assertEqual(payload['scores']['valence'], 0.7)
            self.assertEqual(payload['scores']['passion_delta'], 0.01)
        finally:
            conn.close()

        # 进程重启后重放 → 一条 v3 事件；重复 drain 仍一条
        s1 = shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        self.assertEqual(s1['delivered'], 1, msg=s1)
        s2 = shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        self.assertEqual(s2['delivered'], 0)
        conn = store.open_store(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='user_scored:101'"
            ).fetchone()[0]
            self.assertEqual(n, 1)
            self.assertEqual(shadow.count_pending_outbox(conn), 0)
        finally:
            conn.close()

    def test_health_shows_watermark_lag(self):
        conn = store.open_store(self.db_path)
        try:
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 200, applied_at='2026-07-21 12:01:00', source='unit', score_hash='unit')
            conn.execute('COMMIT')
        finally:
            conn.close()
        h = shadow.get_shadow_health(db_path=self.db_path, environ=ALL_ON)
        self.assertEqual(h.proof_max_message_id, 200)
        self.assertEqual(h.last_scored_message_id, 20)
        self.assertTrue(h.watermark_lag)
        self.assertEqual(h.last_status, 'watermark_lag')

    def test_user_rule_outbox_same_txn_survives_failed_drain(self):
        def get_db():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c

        with mock.patch.object(
            shadow, 'observe_user_message_shadow',
            return_value=shadow.ShadowResult(
                ok=False, status='failed', error='boom'),
        ):
            turn = moments_turn.insert_user_message(
                get_db, {}, '规则消息',
                memories_db_path=self.db_path,
            )
        mid = turn['user_message_id']
        conn = store.open_store(self.db_path)
        try:
            self.assertEqual(shadow.count_pending_outbox(conn), 1)
            row = conn.execute(
                f"SELECT event_type, payload_json FROM {shadow.OUTBOX_TABLE} "
                "WHERE delivered_at IS NULL"
            ).fetchone()
            self.assertEqual(row[0], 'user_rule')
            payload = json.loads(row[1])
            self.assertEqual(payload['message_id'], mid)
            self.assertEqual(payload['text'], '规则消息')
        finally:
            conn.close()
        # 重放成功且幂等
        shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        conn = store.open_store(self.db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                f"WHERE event_key='user_rule:{mid}'"
            ).fetchone()[0]
            self.assertEqual(n, 1)
        finally:
            conn.close()


class AckGapTests(unittest.TestCase):
    def test_ack_gap_resolves_only_matching_message_incidents(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'ack.db')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            shadow.mark_proof_gap(
                conn, failed_message_id=101, error_code='gap_101',
            )
            shadow.mark_proof_gap(
                conn, failed_message_id=102, error_code='gap_102',
            )
            self.assertEqual(shadow.count_unresolved_gap_incidents(conn), 2)
            with self.assertRaises(store.StoreError):
                shadow.ack_proof_gap(
                    conn, message_id=8, reason='wrong id', db_path=db_path,
                )
            with self.assertRaises(store.StoreError):
                shadow.ack_proof_gap(
                    conn, message_id=102, reason='  ', db_path=db_path,
                )
            result = shadow.ack_proof_gap(
                conn, message_id=102, reason='reconciled 102 only',
                db_path=db_path,
            )
            self.assertTrue(result['acked'])
            self.assertEqual(result['remaining_unresolved'], 1)
            # 101 仍在
            unresolved = shadow.list_unresolved_gap_incidents(conn)
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]['message_id'], 101)
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
        finally:
            conn.close()


class ScoreHashIdempotencyTests(unittest.TestCase):
    def test_same_message_same_second_different_scores_no_silent_split(self):
        """两线程同 message_id、同秒、不同 scores：第二次不得改 legacy，也不得静默留不同 outbox。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'hash.db')
        import os
        patch = mock.patch.dict(os.environ, ALL_ON, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = store.open_store(db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 20, applied_at=T0, source='unit', score_hash='boot')
            conn.execute('COMMIT')
        finally:
            conn.close()
        self.assertTrue(shadow.ensure_bootstrapped(
            db_path=db_path, environ={shadow.SHADOW_ENABLED_ENV: '1'},
        ).ok)

        ee.DB_PATH = db_path
        scores_a = {
            'valence': 0.2, 'arousal': 0.4, 'mood_word': 'A',
            'passion_delta': 0.01, 'intimacy_delta': 0.0,
        }
        scores_b = {
            'valence': 0.9, 'arousal': 0.1, 'mood_word': 'B',
            'passion_delta': 0.05, 'intimacy_delta': 0.02,
        }
        frozen_at = '2026-07-21 15:00:00'

        with mock.patch.object(ee, '_deepseek_score', return_value=scores_a), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(ee, '_now_str', return_value=frozen_at):
            ee.score_and_update('first', message_id=50)

        conn = store.open_store(db_path)
        try:
            pa_after_a = conn.execute(
                'SELECT pa FROM emotion_state WHERE id=1'
            ).fetchone()[0]
            proof = shadow.lookup_score_proof(conn, 50)
            self.assertIsNotNone(proof)
            hash_a = proof['score_hash']
        finally:
            conn.close()

        with mock.patch.object(ee, '_deepseek_score', return_value=scores_b), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(ee, '_now_str', return_value=frozen_at):
            ee.score_and_update('second', message_id=50)

        conn = store.open_store(db_path)
        try:
            pa_after_b = conn.execute(
                'SELECT pa FROM emotion_state WHERE id=1'
            ).fetchone()[0]
            self.assertAlmostEqual(pa_after_a, pa_after_b, places=4)
            proof2 = shadow.lookup_score_proof(conn, 50)
            self.assertEqual(proof2['score_hash'], hash_a)
            # outbox 若存在，只能是 A
            row = conn.execute(
                f"SELECT payload_json FROM {shadow.OUTBOX_TABLE} "
                "WHERE event_key='user_scored:50'"
            ).fetchone()
            if row is not None:
                payload = json.loads(row[0])
                self.assertEqual(payload['scores']['mood_word'], 'A')
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
            codes = [
                i['error_code']
                for i in shadow.list_unresolved_gap_incidents(conn)
            ]
            self.assertIn('score_proof_payload_conflict', codes)
        finally:
            conn.close()


class MultiGapIncidentTests(unittest.TestCase):
    def test_sidecar_append_does_not_overwrite(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'multi.db')
        shadow.append_gap_incident_sidecar(
            db_path, failed_message_id=101, error_code='first',
        )
        shadow.append_gap_incident_sidecar(
            db_path, failed_message_id=102, error_code='second',
        )
        self.assertEqual(shadow.count_gap_sidecar_pending(db_path), 2)
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            self.assertEqual(shadow.count_unresolved_gap_incidents(conn), 2)
            mids = {
                i['message_id']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertEqual(mids, {101, 102})
        finally:
            conn.close()
        self.assertEqual(shadow.count_gap_sidecar_pending(db_path), 0)


class MonotonicScoreGuardTests(unittest.TestCase):
    def test_late_lower_message_does_not_rewrite_legacy(self):
        """101 在写库前阻塞；102 先完成；101 后完成不得覆盖 legacy。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'mono.db')
        patch = mock.patch.dict(os.environ, ALL_ON, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = store.open_store(db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 20, applied_at=T0, source='unit', score_hash='boot')
            conn.execute('COMMIT')
        finally:
            conn.close()
        self.assertTrue(shadow.ensure_bootstrapped(
            db_path=db_path, environ={shadow.SHADOW_ENABLED_ENV: '1'},
        ).ok)

        ee.DB_PATH = db_path
        release_101 = threading.Event()
        entered_101 = threading.Event()
        done = {}

        scores_101 = {
            'valence': 0.1, 'arousal': 0.2, 'mood_word': '慢',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }
        scores_102 = {
            'valence': 0.9, 'arousal': 0.8, 'mood_word': '快',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }
        import datetime as _dt
        conn = store.open_store(db_path)
        try:
            base = store.read_state(conn)['p_updated_at']
        finally:
            conn.close()
        base_dt = _dt.datetime.strptime(base, '%Y-%m-%d %H:%M:%S')
        at_101 = (base_dt + _dt.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        at_102 = (base_dt + _dt.timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S')

        def deepseek_101(_text):
            entered_101.set()
            release_101.wait(timeout=5)
            return scores_101

        def run_101():
            with mock.patch.object(ee, '_deepseek_score', side_effect=deepseek_101), \
                 mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
                 mock.patch.object(ee, 'get_longing', return_value=0.1), \
                 mock.patch.object(ee, '_now_str', return_value=at_101):
                ee.score_and_update('turn-101', message_id=101)
            done['101'] = True

        t101 = threading.Thread(target=run_101, daemon=True)
        t101.start()
        self.assertTrue(entered_101.wait(timeout=5))

        with mock.patch.object(ee, '_deepseek_score', return_value=scores_102), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(ee, '_now_str', return_value=at_102):
            ee.score_and_update('turn-102', message_id=102)
        done['102'] = True

        release_101.set()
        t101.join(timeout=5)
        self.assertTrue(done.get('101'))

        conn = store.open_store(db_path)
        try:
            mood = conn.execute(
                'SELECT mood_word FROM emotion_state WHERE id=1'
            ).fetchone()[0]
            self.assertEqual(mood, '快')
            self.assertEqual(shadow.max_score_proof_message_id(conn), 102)
            self.assertIsNone(shadow.lookup_score_proof(conn, 101))
            shadow.drain_shadow_outbox(db_path=db_path, environ=ALL_ON)
            h2 = shadow.get_shadow_health(db_path=db_path, environ=ALL_ON)
            self.assertEqual(h2.proof_max_message_id, 102)
            self.assertFalse(h2.watermark_lag)
            st = store.read_state(conn)
            self.assertEqual(int(st['last_scored_message_id']), 102)
        finally:
            conn.close()


class SidecarFailClosedTests(unittest.TestCase):
    def test_sidecar_open_failure_keeps_emotion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'fail.db')
        import os
        patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = sqlite3.connect(db_path)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db_path
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'append_gap_incident_sidecar',
                 side_effect=OSError('disk full'),
             ):
            ee.score_and_update('hello', message_id=9)
        conn = sqlite3.connect(db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    def test_invalid_message_id_sidecar_failure_keeps_emotion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'badid.db')
        import os
        patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = sqlite3.connect(db_path)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db_path
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': 'x',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'append_gap_incident_sidecar',
                 side_effect=OSError('no perm'),
             ):
            ee.score_and_update('hello', message_id=True)  # type: ignore[arg-type]
        conn = sqlite3.connect(db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    def test_sidecar_fsync_failure_keeps_emotion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'fsync.db')
        patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = sqlite3.connect(db_path)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db_path
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(os, 'fsync', side_effect=OSError('fsync failed')):
            ee.score_and_update('hello', message_id=9)
        conn = sqlite3.connect(db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    def test_shadow_import_failure_and_sidecar_unwritable_keeps_emotion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'importfail.db')
        patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = sqlite3.connect(db_path)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db_path
        real_import = __import__

        def boom_import(name, *args, **kwargs):
            if name == 'internal_state_shadow':
                raise ImportError('shadow unavailable')
            return real_import(name, *args, **kwargs)

        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '开心',
            'passion_delta': 0.0, 'intimacy_delta': 0.0,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch('builtins.__import__', side_effect=boom_import), \
             mock.patch('builtins.open', side_effect=OSError('no perm')):
            ee.score_and_update('hello', message_id=9)
        conn = sqlite3.connect(db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()


class SidecarMigrateRaceTests(unittest.TestCase):
    def test_append_during_migrate_survives(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'race.db')
        shadow.append_gap_incident_sidecar(
            db_path, failed_message_id=1, error_code='old',
        )
        # Hook rename：在 rename 之后、处理之前追加新行到新的 canonical
        real_rename = os.rename
        appended = {}

        def rename_and_append(src, dst):
            real_rename(src, dst)
            if str(src).endswith('.shadow_proof_gap.jsonl') and 'processing' in str(dst):
                shadow.append_gap_incident_sidecar(
                    db_path, failed_message_id=2, error_code='new_during_migrate',
                )
                appended['ok'] = True

        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            with mock.patch.object(os, 'rename', side_effect=rename_and_append):
                shadow.ensure_shadow_schema(conn, db_path=db_path)
            self.assertTrue(appended.get('ok'))
            mids = {
                i['message_id']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            # old 已迁入；new 仍在 canonical sidecar 或也已可见
            self.assertIn(1, mids)
            # 新 append 的 canonical 仍应 pending
            self.assertGreaterEqual(shadow.count_gap_sidecar_pending(db_path), 1)
            # 再 migrate 一次把新行迁入
            shadow.migrate_proof_gap_sidecar(conn, db_path=db_path)
            mids2 = {
                i['message_id']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertEqual(mids2, {1, 2})
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
        finally:
            conn.close()

    def test_corrupt_quarantine_blocks_bootstrap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'corrupt.db')
        side = Path(shadow.proof_gap_sidecar_path(db_path))
        side.write_text('{not-json\n', encoding='utf-8')
        conn = store.open_store(db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            # 需要至少一条合法 proof 才能 bootstrap；先准备
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            # ensure 已 migrate corrupt → sidecar_corrupt incident + quarantine
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
            self.assertGreaterEqual(shadow.count_quarantine_pending(db_path), 1)
            codes = {
                i['error_code']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertIn('sidecar_corrupt', codes)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 5, applied_at=T0, source='unit', score_hash='x')
            conn.execute('COMMIT')
        finally:
            conn.close()
        r = shadow.ensure_bootstrapped(
            db_path=db_path,
            environ={shadow.SHADOW_ENABLED_ENV: '1'},
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'proof_gap')


class LegacyHashUnknownTests(unittest.TestCase):
    def test_null_score_hash_refuses_backfill(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'nullhash.db')
        conn = store.open_store(db_path)
        try:
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            conn.execute(
                f"""
                INSERT INTO {shadow.SCORE_APPLIED_TABLE}
                    (message_id, applied_at, source, score_hash)
                VALUES (7, ?, 'old', NULL)
                """,
                (T0,),
            )
            conn.execute('BEGIN')
            with self.assertRaises(store.StoreError) as ctx:
                shadow.record_score_proof_in_txn(
                    conn, 7, applied_at=T0, source='old', score_hash='newhash',
                )
            self.assertIn('legacy_hash_unknown', str(ctx.exception))
            conn.execute('ROLLBACK')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
