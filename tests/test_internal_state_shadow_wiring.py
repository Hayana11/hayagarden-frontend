"""Phase 1A-4b — user_rule / user_scored 生产接线与三开关。"""

from __future__ import annotations

import ast
import json
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

    def test_user_events_requires_master(self):
        self.assertFalse(shadow.is_user_events_enabled(environ=EVENTS_WITHOUT_MASTER))
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

    def test_schema_missing_keeps_legacy_and_marks_gap(self):
        # 新库：有 emotion，无 proof 表
        db2 = str(Path(self.tmp.name) / 'nogap.db')
        conn = sqlite3.connect(db2)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = db2
        # health 表也不存在 — mark_proof_gap_standalone 只打日志
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
                conn, 5, applied_at=T0, source='unit')
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
    def test_insert_user_message_emits_once_when_enabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'user.db')
        calls = []

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

        with mock.patch.object(
            shadow, 'emit_user_rule_if_enabled',
            side_effect=lambda **kw: calls.append(kw) or shadow.ShadowResult(
                ok=True, status='disabled'),
        ), mock.patch.dict(
            __import__('os').environ, ALL_ON, clear=False,
        ):
            # emit 被 mock；验证参数
            turn = moments_turn.insert_user_message(
                get_db, {}, '你好呀',
                memories_db_path=db_path,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['message_id'], turn['user_message_id'])
        self.assertEqual(calls[0]['text'], '你好呀')
        self.assertEqual(calls[0]['previous_user_at'], '2026-07-21 10:00:00')
        self.assertTrue(calls[0]['created_at'])

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
             mock.patch.object(shadow, 'observe_user_message_shadow') as obs:
            moments_turn.insert_user_message(
                get_db, {}, 'hi', memories_db_path=db_path,
            )
            obs.assert_not_called()


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


if __name__ == '__main__':
    unittest.main()
