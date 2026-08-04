"""Phase 1A-4b — user_rule / user_scored 生产接线与三开关。"""

from __future__ import annotations

import ast
import datetime
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


# Stage C retires emotion_engine dual-write (emotion_state + score_proof)
# as Affect/Bond authority. Obsolete score_and_update dual-write cases are
# skipped; authoritative coverage lives in tests.test_affect_bond_authority.
_STAGE_C_LEGACY_SCORE_DUALWRITE_RETIRED = (
    'Stage C: score_and_update Affect/Bond dual-write retired; '
    'see tests.test_affect_bond_authority'
)
_skip_legacy_score_dualwrite = unittest.skip(_STAGE_C_LEGACY_SCORE_DUALWRITE_RETIRED)


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

    @_skip_legacy_score_dualwrite
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

    @_skip_legacy_score_dualwrite
    def test_proof_failure_preserves_legacy_emotion_and_records_gap(self):
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
            self.assertNotAlmostEqual(pa, 0.5, places=4)
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

    @_skip_legacy_score_dualwrite
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

    @_skip_legacy_score_dualwrite
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


class WakeOutcomeWiringTests(unittest.TestCase):
    def test_apply_outcome_shadow_disabled_when_shadow_off(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'wake-off.db')
        r = shadow.apply_outcome_shadow(
            wake_run_id='run-off-1',
            executor_action='none',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=1.5,
            outcome_at=T0,
            db_path=db_path,
            environ=OFF,
        )
        self.assertEqual(r.status, 'disabled')
        self.assertFalse(Path(db_path).exists())

    def test_infer_fired_drive_matches_discharge_selection(self):
        import drive_engine as de
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'drive.db')
        de.DB_PATH = db_path
        now_str = de._now().strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                f"""
                CREATE TABLE drive_state (
                    id INTEGER PRIMARY KEY,
                    attachment REAL, curiosity REAL, reflection REAL, social REAL,
                    duty REAL, libido REAL, stress REAL, fatigue REAL,
                    last_updated TEXT
                );
                INSERT INTO drive_state VALUES (
                    1, 0.1, 0.8, 0.2, 0.3, 0.1, 0.1, 0.1, 0.2, '{now_str}');
                """
            )
            conn.commit()
        finally:
            conn.close()
        # Without Stage D cutover, get_drive fail-closes to defaults; inference
        # still returns a stable key for action='message'. Legacy discharge is
        # a retired no-op and must not mutate production truth.
        fired = de.infer_fired_drive_for_action('message')
        self.assertIsInstance(fired, str)
        before = de.get_drive()
        de.discharge_by_action('message')
        after = de.get_drive()
        self.assertEqual(after, before)

    def test_normal_mode_records_wake_outcome_once(self):
        with mock.patch.object(shadow, 'is_shadow_enabled', return_value=True), \
             mock.patch.object(
                 shadow, 'apply_outcome_shadow',
                 return_value=shadow.ShadowResult(ok=True, status='applied'),
             ) as apply_mock, \
             mock.patch.object(shadow, 'mark_proof_gap_standalone') as gap_mock:
            shadow.record_wake_outcome_shadow_if_enabled(
                wake_run_id='run-normal-1',
                mode='normal',
                action='message',
                fired_drive='curiosity',
                desire_driven=True,
                user_idle_hours=2.5,
                db_path=':memory:',
            )
        apply_mock.assert_called_once()
        gap_mock.assert_not_called()

    def test_dream_and_summarize_skip_wake_outcome(self):
        for mode in ('dream', 'summarize'):
            with self.subTest(mode=mode):
                with mock.patch.object(shadow, 'is_shadow_enabled', return_value=True), \
                     mock.patch.object(shadow, 'apply_outcome_shadow') as apply_mock:
                    shadow.record_wake_outcome_shadow_if_enabled(
                        wake_run_id=f'run-{mode}',
                        mode=mode,
                        action='message',
                        fired_drive='curiosity',
                        desire_driven=False,
                        user_idle_hours=1.0,
                        db_path=':memory:',
                    )
                apply_mock.assert_not_called()

    def test_outcome_at_uses_fresh_clock_not_wake_start_now(self):
        helper = Path(ROOT, 'internal_state_shadow.py').read_text(encoding='utf-8')
        helper = helper.split('def record_wake_outcome_shadow_if_enabled', 1)[1].split(
            '\ndef ', 1,
        )[0]
        self.assertIn('_now_beijing()', helper)
        self.assertNotIn('now.strftime', helper)
        decide = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        decide = decide.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertNotIn('outcome_at=now.strftime', decide)

    def test_desire_driven_frozen_before_executor_and_reused(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        block = src.split('def _wake_decide_locked', 1)[1].split('\ndef ', 1)[0]
        self.assertEqual(block.count('desire_driven = _get_desire_driven()'), 1)
        self.assertIn('desire_driven=desire_driven', block)
        self.assertNotIn('desire_driven=_get_desire_driven()', block)

    def test_shadow_failure_records_gap_without_blocking_mark(self):
        with mock.patch.object(shadow, 'is_shadow_enabled', return_value=True), \
             mock.patch.object(
                 shadow, 'apply_outcome_shadow',
                 return_value=shadow.ShadowResult(
                     ok=False, status='failed', error='boom',
                 ),
             ), mock.patch.object(shadow, 'mark_proof_gap_standalone') as gap_mock:
            shadow.record_wake_outcome_shadow_if_enabled(
                wake_run_id='run-fail-1',
                mode='normal',
                action='message',
                fired_drive='curiosity',
                desire_driven=True,
                user_idle_hours=1.0,
                db_path=':memory:',
            )
        gap_mock.assert_called_once()
        self.assertEqual(
            gap_mock.call_args.kwargs['error_code'],
            'wake_outcome_capture_failed',
        )
        block = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        record_idx = block.index('record_wake_outcome_shadow_if_enabled')
        self.assertIn('_wake_run_id_mark', block[record_idx:])

    def test_duplicate_wake_run_id_skips_shadow_outcome(self):
        block = Path(ROOT, 'gateway.py').read_text(encoding='utf-8').split(
            'def _wake_decide_locked', 1,
        )[1].split('\ndef ', 1)[0]
        seen_idx = block.index('_wake_run_id_seen')
        record_idx = block.index('record_wake_outcome_shadow_if_enabled')
        self.assertLess(seen_idx, record_idx)


class GatewayGuardTests(unittest.TestCase):
    def test_gateway_passes_message_id_to_trigger_turn_scoring(self):
        src = Path(ROOT, 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('trigger_turn_scoring', src)
        self.assertIn("message_id=_turn_data.get('user_message_id')", src)
        self.assertNotIn('_ee.score_async(', src)

    def test_wake_outcome_wired_only_in_gateway_not_wake_package(self):
        for name in ('app.py',):
            text = Path(ROOT, name).read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('apply_outcome_shadow', text)
        wake_dir = Path(ROOT, 'wake')
        if wake_dir.is_dir():
            for path in wake_dir.rglob('*.py'):
                text = path.read_text(encoding='utf-8', errors='replace')
                self.assertNotIn('apply_outcome_shadow', text)
        gateway = Path(ROOT, 'gateway.py').read_text(encoding='utf-8', errors='replace')
        self.assertIn('record_wake_outcome_shadow_if_enabled', gateway)
        self.assertNotIn('apply_outcome_shadow', gateway)

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
    @_skip_legacy_score_dualwrite
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
            shadow, 'observe_planned_user_message_shadow',
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
            self.assertNotIn('规则消息', row[1])
            self.assertNotIn('text', payload)
            envelope = payload['envelope']
            self.assertEqual(envelope['observation']['message_id'], mid)
            self.assertIn('text_hash', envelope['observation'])
            self.assertIn('text_length', envelope['observation'])
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
    @_skip_legacy_score_dualwrite
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
    @_skip_legacy_score_dualwrite
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
    @_skip_legacy_score_dualwrite
    def test_sidecar_open_failure_preserves_legacy_emotion(self):
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
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    @_skip_legacy_score_dualwrite
    def test_invalid_message_id_sidecar_failure_preserves_legacy_emotion(self):
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
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    @_skip_legacy_score_dualwrite
    def test_sidecar_fsync_failure_preserves_legacy_emotion(self):
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
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()

    @_skip_legacy_score_dualwrite
    def test_shadow_import_failure_and_sidecar_unwritable_preserves_legacy_emotion(self):
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
            self.assertNotAlmostEqual(pa, 0.5, places=4)
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
        # Hook rename：claim *.json → *.processing 之后再写新文件
        real_rename = os.rename
        appended = {}

        def rename_and_append(src, dst):
            real_rename(src, dst)
            if str(src).endswith('.json') and str(dst).endswith('.json.processing'):
                if 'shadow_gap_incidents' in str(src):
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
            self.assertIn(1, mids)
            self.assertGreaterEqual(shadow.count_gap_sidecar_pending(db_path), 1)
            shadow.migrate_proof_gap_sidecar(conn, db_path=db_path)
            mids2 = {
                i['message_id']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertEqual(mids2, {1, 2})
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
        finally:
            conn.close()

    def test_open_tmp_before_migrate_survives(self):
        """writer 已 open(.tmp) 尚未 write 时 migrator 跑完，最终 incident 仍在。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'fd_race.db')
        opened = threading.Event()
        release_write = threading.Event()
        real_open = open
        done = {}

        def gated_open(file, mode='r', *args, **kwargs):
            path_s = str(file)
            if path_s.endswith('.tmp') and isinstance(mode, str) and 'w' in mode:
                fh = real_open(file, mode, *args, **kwargs)
                opened.set()
                self.assertTrue(release_write.wait(timeout=5))
                return fh
            return real_open(file, mode, *args, **kwargs)

        def writer():
            with mock.patch('builtins.open', side_effect=gated_open):
                shadow.append_gap_incident_sidecar(
                    db_path, failed_message_id=9, error_code='fd_race',
                )
            done['ok'] = True

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        self.assertTrue(opened.wait(timeout=5))
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            # migrator 时只有 .tmp，不应吞掉尚未 rename 的证据
            release_write.set()
            t.join(timeout=5)
            self.assertTrue(done.get('ok'))
            shadow.migrate_proof_gap_sidecar(conn, db_path=db_path)
            mids = {
                i['message_id']
                for i in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertIn(9, mids)
        finally:
            conn.close()

    def test_corrupt_quarantine_blocks_bootstrap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'corrupt.db')
        d = shadow.gap_incidents_dir(db_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / 'bad.json').write_text('{not-json\n', encoding='utf-8')
        conn = store.open_store(db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
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


class SerializedTransitionTests(unittest.TestCase):
    @_skip_legacy_score_dualwrite
    def test_dual_score_cumulative_matches_shadow(self):
        """101/102 都先取得 raw scores（barrier），再写入；legacy 累计须与 Shadow 一致。

        旧实现会在写锁外双读 S0，最终只保留 transition(S0,102)；
        正确实现在 BEGIN IMMEDIATE 内读当前行，得到累计 transition。
        """
        import time as _time
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'cumul.db')
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
            p0 = float(conn.execute(
                'SELECT sternberg_p FROM emotion_state WHERE id=1'
            ).fetchone()[0])
        finally:
            conn.close()
        self.assertTrue(shadow.ensure_bootstrapped(
            db_path=db_path, environ={shadow.SHADOW_ENABLED_ENV: '1'},
        ).ok)

        ee.DB_PATH = db_path
        scores_ready = threading.Barrier(2)
        scores_101 = {
            'valence': 0.8, 'arousal': 0.5, 'mood_word': '一',
            'passion_delta': 0.1, 'intimacy_delta': 0.05,
        }
        scores_102 = {
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '二',
            'passion_delta': 0.1, 'intimacy_delta': 0.05,
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

        def deepseek_dispatch(_text):
            mid = getattr(threading.current_thread(), 'score_mid', None)
            scores_ready.wait(timeout=5)
            if mid == 102:
                # 双方 scores 已就绪；略延迟让 101 先拿到写锁
                _time.sleep(0.05)
                return scores_102
            return scores_101

        def now_str_dispatch():
            mid = getattr(threading.current_thread(), 'score_mid', None)
            return at_102 if mid == 102 else at_101

        def run_101():
            threading.current_thread().score_mid = 101  # type: ignore[attr-defined]
            ee.score_and_update('turn-101', message_id=101)

        def run_102():
            threading.current_thread().score_mid = 102  # type: ignore[attr-defined]
            ee.score_and_update('turn-102', message_id=102)

        with mock.patch.object(ee, '_deepseek_score', side_effect=deepseek_dispatch), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, '_now_str', side_effect=now_str_dispatch):
            t101 = threading.Thread(target=run_101, daemon=True)
            t102 = threading.Thread(target=run_102, daemon=True)
            t101.start()
            t102.start()
            t101.join(timeout=8)
            t102.join(timeout=8)

        shadow.drain_shadow_outbox(db_path=db_path, environ=ALL_ON)
        # 再 drain 一次，避免 102 因 101 尚未 apply 而 rewind 后重放
        shadow.drain_shadow_outbox(db_path=db_path, environ=ALL_ON)
        conn = store.open_store(db_path)
        try:
            leg = conn.execute(
                'SELECT pa, na, valence, arousal, mood_word, '
                'sternberg_p, sternberg_i FROM emotion_state WHERE id=1'
            ).fetchone()
            st = store.read_state(conn)
            self.assertEqual(shadow.max_score_proof_message_id(conn), 102)
            self.assertIsNotNone(shadow.lookup_score_proof(conn, 101))
            self.assertIsNotNone(shadow.lookup_score_proof(conn, 102))
            self.assertEqual(int(st['last_scored_message_id']), 102)
            self.assertAlmostEqual(float(leg[0]), float(st['pa']), places=4)
            self.assertAlmostEqual(float(leg[1]), float(st['na']), places=4)
            self.assertAlmostEqual(float(leg[2]), float(st['valence']), places=4)
            self.assertAlmostEqual(float(leg[3]), float(st['arousal']), places=4)
            self.assertEqual(leg[4], st['mood_word'])
            self.assertAlmostEqual(float(leg[5]), float(st['passion']), places=4)
            self.assertAlmostEqual(float(leg[6]), float(st['intimacy']), places=4)
            # 累计：双读 S0 时 P≈p0+0.1；串行应为 p0+0.2
            self.assertGreater(float(leg[5]), p0 + 0.15)
            h = shadow.get_shadow_health(db_path=db_path, environ=ALL_ON)
            self.assertFalse(h.watermark_lag)
        finally:
            conn.close()


class QuarantineReconcileTests(unittest.TestCase):
    def test_reconcile_archives_and_unlocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'qr.db')
        d = shadow.gap_incidents_dir(db_path)
        d.mkdir(parents=True, exist_ok=True)
        bad = d / 'x.json'
        bad.write_text('{bad\n', encoding='utf-8')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            self.assertGreaterEqual(shadow.count_quarantine_pending(db_path), 1)
            items = shadow.inspect_quarantine(db_path)
            self.assertEqual(len(items), 1)
            item = items[0]
            # ack alone 不得清 quarantine
            iid = shadow.list_unresolved_gap_incidents(conn)[0]['incident_id']
            shadow.ack_proof_gap(
                conn, incident_id=iid, reason='acked corrupt without file',
                db_path=db_path,
            )
            self.assertGreaterEqual(shadow.count_quarantine_pending(db_path), 1)
            self.assertTrue(shadow.has_unresolved_proof_gap(conn, db_path=db_path))
            # 再制造一条 sidecar_corrupt（ack 已解决前一条）——直接再 quarantine 流程
            # reconcile 当前 pending 文件；若 incident 已被 ack，需再有一条
            # ensure 时已有 quarantine；ack 清了 incident 但文件仍在 → has_gap True
            # 为 reconcile 再插一条 matching incident
            shadow.mark_proof_gap(
                conn, failed_message_id=None, error_code='sidecar_corrupt',
                db_path=db_path, write_sidecar_if_missing=False,
            )
            result = shadow.reconcile_quarantine(
                conn,
                path=item['path'],
                sha256=item['sha256'],
                reason='operator reviewed corrupt gap',
                db_path=db_path,
            )
            self.assertTrue(result['reconciled'])
            self.assertEqual(shadow.count_quarantine_pending(db_path), 0)
            self.assertTrue(Path(result['resolved_archive']).is_file())
            self.assertFalse(
                shadow.has_unresolved_proof_gap(conn, db_path=db_path)
            )
        finally:
            conn.close()

    def test_ack_null_message_id_incident_without_forged_mid(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'nullmid.db')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            shadow.mark_proof_gap(
                conn, failed_message_id=None, error_code='sidecar_corrupt',
                db_path=db_path, write_sidecar_if_missing=False,
            )
            iid = shadow.list_unresolved_gap_incidents(conn)[0]['incident_id']
            with self.assertRaises(store.StoreError):
                shadow.ack_proof_gap(
                    conn, message_id=999, incident_id=iid, reason='no forge',
                    db_path=db_path,
                )
            result = shadow.ack_proof_gap(
                conn, incident_id=iid, reason='null mid ok',
                db_path=db_path,
            )
            self.assertIsNone(result['message_id'])
            self.assertEqual(result['remaining_unresolved'], 0)
            row = conn.execute(
                f'SELECT message_id FROM {shadow.GAP_ACK_TABLE} '
                f'WHERE incident_id=?',
                (iid,),
            ).fetchone()
            self.assertIsNone(row[0])
        finally:
            conn.close()

    def test_prepared_intent_retries_after_rename_failure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'intent.db')
        d = shadow.gap_incidents_dir(db_path)
        d.mkdir(parents=True, exist_ok=True)
        (d / 'bad.json').write_text('{bad\n', encoding='utf-8')
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            item = shadow.inspect_quarantine(db_path)[0]
            iid = shadow.list_unresolved_gap_incidents(conn)[0]['incident_id']
            with mock.patch.object(os, 'rename', side_effect=OSError('crash after intent')):
                with self.assertRaises(OSError):
                    shadow.reconcile_quarantine(
                        conn, path=item['path'], sha256=item['sha256'],
                        reason='reviewed', db_path=db_path, incident_id=iid,
                    )
            self.assertEqual(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.QUARANTINE_INTENT_TABLE} '
                    'WHERE completed_at IS NULL'
                ).fetchone()[0],
                1,
            )
            result = shadow.reconcile_quarantine(
                conn, path=item['path'], sha256=item['sha256'],
                reason='reviewed', db_path=db_path, incident_id=iid,
            )
            self.assertTrue(result['reconciled'])
            self.assertEqual(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.QUARANTINE_RECONCILE_TABLE}'
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()


class CaptureAlertTests(unittest.TestCase):
    def test_total_gap_persistence_failure_writes_cross_process_alert(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'alert.db')
        alert_path = str(Path(tmp.name) / 'independent-alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ), mock.patch.object(
            shadow, 'open_shadow_connection', side_effect=OSError('db unavailable'),
        ), mock.patch.object(
            shadow, 'write_proof_gap_sidecar', side_effect=OSError('sidecar unavailable'),
        ):
            result = shadow.mark_proof_gap_standalone(
                db_path=db_path, failed_message_id=7, error_code='outbox_capture_gap',
            )
            self.assertEqual(result.status, 'failed')
            self.assertTrue(result.alert_recorded)
            self.assertTrue(Path(alert_path).is_file())
            self.assertTrue(shadow.has_capture_alert(db_path))

        import subprocess
        env = dict(os.environ)
        env[shadow.CAPTURE_ALERT_PATH_ENV] = alert_path
        out = subprocess.run(
            [sys.executable, 'tools/internal_state_shadow_admin.py',
             '--db', db_path, 'status'],
            cwd=ROOT, env=env, text=True, capture_output=True,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('"capture_alert_pending": true', out.stdout)

    def test_capture_alert_ack_archives_marker(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'ack-alert.db')
        alert_path = str(Path(tmp.name) / 'alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ):
            self.assertTrue(shadow.note_capture_evidence_failure(
                'operator test', db_path=db_path,
            ))
            conn = store.open_store(db_path)
            try:
                result = shadow.ack_capture_alert(
                    conn, sha256=shadow._sha256_file(Path(alert_path)),
                    reason='reviewed', db_path=db_path,
                )
                self.assertTrue(result['acked'])
                self.assertFalse(Path(alert_path).exists())
                self.assertTrue(Path(result['archive_path']).is_file())
            finally:
                conn.close()

    def test_ack_claim_does_not_archive_concurrent_new_alert(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'alert-race.db')
        alert_path = str(Path(tmp.name) / 'alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ):
            self.assertTrue(shadow.note_capture_evidence_failure('A', db_path=db_path))
            hash_a = shadow._sha256_file(Path(alert_path))
            real_hash = shadow._sha256_file
            wrote_b = {}

            def hash_then_publish_b(path):
                if '.processing.' in str(path) and not wrote_b:
                    wrote_b['yes'] = shadow.note_capture_evidence_failure(
                        'B', db_path=db_path,
                    )
                return real_hash(path)

            conn = store.open_store(db_path)
            try:
                with mock.patch.object(shadow, '_sha256_file', side_effect=hash_then_publish_b):
                    result = shadow.ack_capture_alert(
                        conn, sha256=hash_a, reason='ack A', db_path=db_path,
                    )
                self.assertTrue(result['acked'])
                self.assertTrue(wrote_b.get('yes'))
                self.assertTrue(Path(alert_path).is_file())
                self.assertNotEqual(shadow._sha256_file(Path(alert_path)), hash_a)
                self.assertEqual(
                    shadow._sha256_file(Path(result['archive_path'])), hash_a,
                )
            finally:
                conn.close()

    def test_capture_ack_recovers_prepared_before_claim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'ack-crash.db')
        alert_path = str(Path(tmp.name) / 'alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ):
            shadow.note_capture_evidence_failure('A', db_path=db_path)
            digest = shadow._sha256_file(Path(alert_path))
            conn = store.open_store(db_path)
            try:
                with mock.patch.object(os, 'rename', side_effect=OSError('crash before claim')):
                    with self.assertRaises(OSError):
                        shadow.ack_capture_alert(
                            conn, sha256=digest, reason='review', db_path=db_path,
                        )
                self.assertTrue(Path(alert_path).is_file())
                prepared = shadow.inspect_capture_alert_acks(conn)
                self.assertEqual(prepared, [])
                # Claim 尚未发生时 canonical 仍是完整 pending；重试可安全继续。
                result = shadow.ack_capture_alert(
                    conn, sha256=digest, reason='review', db_path=db_path,
                )
                self.assertTrue(result['acked'])
                self.assertFalse(Path(alert_path).exists())
            finally:
                conn.close()

    def test_capture_ack_recovers_claimed_processing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'ack-processing.db')
        alert_path = str(Path(tmp.name) / 'alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ):
            shadow.note_capture_evidence_failure('A', db_path=db_path)
            digest = shadow._sha256_file(Path(alert_path))
            conn = store.open_store(db_path)
            try:
                with mock.patch.object(
                    shadow, '_complete_capture_alert_intent',
                    side_effect=OSError('crash after claim'),
                ):
                    with self.assertRaises(OSError):
                        shadow.ack_capture_alert(
                            conn, sha256=digest, reason='review', db_path=db_path,
                        )
                self.assertFalse(Path(alert_path).exists())
                self.assertTrue(shadow.has_capture_alert(db_path))
                recovered = shadow.recover_capture_alert_acks(conn)
                self.assertTrue(recovered[0]['acked'])
                self.assertFalse(shadow.has_capture_alert(db_path))
            finally:
                conn.close()

    def test_orphan_processing_awaits_explicit_review(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'orphan.db')
        alert_path = str(Path(tmp.name) / 'alerts' / 'capture.json')
        with mock.patch.dict(
            os.environ, {shadow.CAPTURE_ALERT_PATH_ENV: alert_path}, clear=False,
        ):
            shadow.note_capture_evidence_failure('B', db_path=db_path)
            proc = Path(alert_path + '.processing.orphan')
            os.rename(alert_path, proc)
            conn = store.open_store(db_path)
            try:
                recovered = shadow.recover_capture_alert_acks(conn)
                self.assertEqual(recovered[0]['status'], 'awaiting_review')
                self.assertEqual(
                    conn.execute(
                        f'SELECT COUNT(*) FROM {shadow.CAPTURE_ALERT_ACK_TABLE}'
                    ).fetchone()[0],
                    0,
                )
                result = shadow.ack_capture_alert_orphan(
                    conn, path=str(proc), sha256=shadow._sha256_file(proc),
                    reason='operator reviewed B',
                )
                self.assertTrue(result['acked'])
            finally:
                conn.close()


class PendingIncidentTmpTests(unittest.TestCase):
    def test_promote_stale_complete_tmp_is_audited_and_migrates(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'tmp.db')
        d = shadow.gap_incidents_dir(db_path)
        d.mkdir(parents=True, exist_ok=True)
        pending = d / 'complete.tmp'
        pending.write_text(
            json.dumps({
                'gap_detected': True, 'failed_message_id': 77,
                'error_code': 'crash_before_rename', 'failed_at': T0,
            }) + '\n',
            encoding='utf-8',
        )
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            result = shadow.recover_pending_incident_tmp(
                conn, path=str(pending), action='promote', reason='verified complete',
                stale_after_seconds=0, db_path=db_path,
            )
            self.assertEqual(result['action'], 'promote')
            shadow.migrate_proof_gap_sidecar(conn, db_path=db_path)
            mids = {
                x['message_id'] for x in shadow.list_unresolved_gap_incidents(conn)
            }
            self.assertIn(77, mids)
            self.assertEqual(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.PENDING_INCIDENT_ACTION_TABLE}'
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_prepared_before_rename_recovers_and_audits_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'tmp-crash.db')
        d = shadow.gap_incidents_dir(db_path)
        d.mkdir(parents=True, exist_ok=True)
        pending = d / 'complete.tmp'
        pending.write_text(
            json.dumps({
                'gap_detected': True, 'failed_message_id': 88,
                'error_code': 'crash_before_rename', 'failed_at': T0,
            }) + '\n', encoding='utf-8',
        )
        conn = store.open_store(db_path)
        try:
            _seed_emotion(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            with mock.patch.object(os, 'rename', side_effect=OSError('crash before rename')):
                with self.assertRaises(OSError):
                    shadow.recover_pending_incident_tmp(
                        conn, path=str(pending), action='promote', reason='verified',
                        stale_after_seconds=0, db_path=db_path,
                    )
            self.assertTrue(pending.is_file())
            recovered = shadow.recover_pending_incident_intents(conn, db_path=db_path)
            self.assertEqual(recovered[0]['status'], 'completed')
            self.assertEqual(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.PENDING_INCIDENT_ACTION_TABLE}'
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()


class AppliedAtLinearizationTests(unittest.TestCase):
    @_skip_legacy_score_dualwrite
    def test_locked_applied_at_is_shared_by_legacy_proof_and_outbox(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'applied.db')
        patch = mock.patch.dict(os.environ, ALL_ON, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        conn = store.open_store(db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=db_path)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 20, applied_at=T0, source='unit', score_hash='boot',
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        self.assertTrue(shadow.ensure_bootstrapped(
            db_path=db_path, environ={shadow.SHADOW_ENABLED_ENV: '1'},
        ).ok)
        ee.DB_PATH = db_path
        import datetime as _dt
        conn = store.open_store(db_path)
        try:
            anchor = store.read_state(conn)['p_updated_at']
        finally:
            conn.close()
        applied = (
            _dt.datetime.strptime(anchor, '%Y-%m-%d %H:%M:%S')
            + _dt.timedelta(seconds=1)
        ).strftime('%Y-%m-%d %H:%M:%S')
        with mock.patch.object(ee, '_deepseek_score', return_value={
            'valence': 0.2, 'arousal': 0.4, 'mood_word': '线性化',
            'passion_delta': 0.1, 'intimacy_delta': 0.05,
        }), mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, '_now_str', return_value=applied):
            ee.score_and_update('hello', message_id=101)
        conn = store.open_store(db_path)
        try:
            emotion = conn.execute(
                'SELECT p_updated_at, i_updated_at, last_interaction, updated_at '
                'FROM emotion_state WHERE id=1'
            ).fetchone()
            proof = shadow.lookup_score_proof(conn, 101)
            outbox = conn.execute(
                f'SELECT payload_json FROM {shadow.OUTBOX_TABLE} '
                "WHERE event_key='user_scored:101'"
            ).fetchone()
            self.assertEqual(tuple(emotion), (applied,) * 4)
            self.assertEqual(proof['applied_at'], applied)
            self.assertEqual(json.loads(outbox[0])['scored_at'], applied)
        finally:
            conn.close()


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


def _deepseek_scores() -> dict:
    return {
        'valence': 0.2, 'arousal': 0.4, 'mood_word': '测试',
        'passion_delta': 0.05, 'intimacy_delta': 0.02,
    }


def _history_count(db_path: str) -> int:
    import emotion_history
    emotion_history.ensure_schema(db_path)
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute('SELECT COUNT(*) FROM emotion_history').fetchone()[0])
    finally:
        conn.close()


def _enqueue_user_scored(
    db_path: str,
    *,
    message_id: int,
    created_at: str,
    scores: dict | None = None,
) -> None:
    body = scores or _deepseek_scores()
    conn = store.open_store(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        shadow.enqueue_user_scored_in_txn(
            conn,
            message_id=message_id,
            scores=body,
            scored_at=created_at,
            environ=ALL_ON,
        )
        conn.execute('COMMIT')
    finally:
        conn.close()


class OutboxFifoQueueIdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / 'fifo.db')
        self.patch = mock.patch.dict(os.environ, ALL_ON, clear=False)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_for_bootstrap(conn)
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.execute('BEGIN')
            shadow.record_score_proof_in_txn(
                conn, 20, applied_at=T0, source='unit', score_hash='boot',
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        self.assertTrue(shadow.ensure_bootstrapped(
            db_path=self.db_path, environ={shadow.SHADOW_ENABLED_ENV: '1'},
        ).ok)

    def test_same_second_enqueue_order_follows_queue_id_not_event_key(self):
        same_ts = '2026-07-21 12:00:00'
        _enqueue_user_scored(
            self.db_path, message_id=99, created_at=same_ts,
        )
        _enqueue_user_scored(
            self.db_path, message_id=100, created_at=same_ts,
        )
        conn = store.open_store(self.db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT queue_id, event_key FROM {shadow.OUTBOX_TABLE}
                WHERE delivered_at IS NULL
                ORDER BY queue_id ASC
                """
            ).fetchall()
            self.assertEqual([r[1] for r in rows], ['user_scored:99', 'user_scored:100'])
        finally:
            conn.close()

        order: list[int] = []

        def track(item, **kwargs):
            payload = json.loads(item['payload_json'])
            order.append(int(payload['message_id']))
            return shadow.ShadowResult(ok=True, status='applied')

        with mock.patch.object(shadow, '_deliver_outbox_row', side_effect=track):
            summary = shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        self.assertEqual(summary['delivered'], 2)
        self.assertEqual(order, [99, 100])

    def test_head_failure_stops_drain_without_touching_tail(self):
        same_ts = '2026-07-21 12:00:01'
        _enqueue_user_scored(self.db_path, message_id=201, created_at=same_ts)
        _enqueue_user_scored(self.db_path, message_id=202, created_at=same_ts)
        delivered: list[str] = []
        real = shadow._deliver_outbox_row

        def gated(item, **kwargs):
            delivered.append(item['event_key'])
            if item['event_key'] == 'user_scored:201':
                return shadow.ShadowResult(
                    ok=False, status='failed', error='head blocked',
                )
            return real(item, **kwargs)

        with mock.patch.object(shadow, '_deliver_outbox_row', side_effect=gated):
            summary = shadow.drain_shadow_outbox(db_path=self.db_path, environ=ALL_ON)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(delivered, ['user_scored:201'])
        conn = store.open_store(self.db_path)
        try:
            pending = conn.execute(
                f"""
                SELECT event_key FROM {shadow.OUTBOX_TABLE}
                WHERE delivered_at IS NULL ORDER BY queue_id ASC
                """
            ).fetchall()
            self.assertEqual([r[0] for r in pending], ['user_scored:201', 'user_scored:202'])
        finally:
            conn.close()


class FallbackHistoryFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / 'fallback.db')
        self.patch = mock.patch.dict(os.environ, PROOF_ONLY, clear=False)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        conn = sqlite3.connect(self.db_path)
        try:
            _seed_emotion(conn)
            conn.commit()
        finally:
            conn.close()
        ee.DB_PATH = self.db_path

    @_skip_legacy_score_dualwrite
    def test_invalid_message_id_incident_fail_updates_emotion_and_history_once(self):
        before = _history_count(self.db_path)
        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'append_gap_incident_sidecar',
                 side_effect=OSError('no sidecar'),
             ):
            ee.score_and_update('hello', message_id=True)  # type: ignore[arg-type]
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()
        self.assertEqual(_history_count(self.db_path), before + 1)

    @_skip_legacy_score_dualwrite
    def test_proof_schema_missing_sidecar_fail_updates_emotion_and_history_once(self):
        before = _history_count(self.db_path)
        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'append_gap_incident_sidecar',
                 side_effect=OSError('disk full'),
             ):
            ee.score_and_update('hello', message_id=11)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()
        self.assertEqual(_history_count(self.db_path), before + 1)

    @_skip_legacy_score_dualwrite
    def test_proof_schema_missing_emotion_txn_fail_updates_emotion_and_history_once(self):
        conn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.execute(f'DROP TABLE {shadow.SCORE_APPLIED_TABLE}')
            conn.commit()
        finally:
            conn.close()
        before = _history_count(self.db_path)
        real_transition = ee._transition_from_state
        fail_once = {'pending': True}

        def flaky_transition(*args, **kwargs):
            if fail_once['pending']:
                fail_once['pending'] = False
                raise RuntimeError('txn boom')
            return real_transition(*args, **kwargs)

        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(ee, '_transition_from_state', side_effect=flaky_transition):
            ee.score_and_update('hello', message_id=12)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()
        self.assertEqual(_history_count(self.db_path), before + 1)

    @_skip_legacy_score_dualwrite
    def test_compute_score_hash_failure_still_legacy_and_history_once(self):
        before = _history_count(self.db_path)
        conn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.commit()
        finally:
            conn.close()
        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'compute_score_hash',
                 side_effect=RuntimeError('hash boom'),
             ):
            ee.score_and_update('hello', message_id=13)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()
        self.assertEqual(_history_count(self.db_path), before + 1)
        conn = store.open_store(self.db_path)
        try:
            self.assertTrue(
                shadow.has_unresolved_proof_gap(conn, db_path=self.db_path)
                or shadow.count_gap_sidecar_pending(self.db_path) > 0
                or shadow.has_capture_alert(self.db_path),
            )
            h = shadow.get_shadow_health(
                db_path=self.db_path,
                environ={
                    shadow.SHADOW_ENABLED_ENV: '1',
                    shadow.SCORE_PROOF_ENABLED_ENV: '1',
                    shadow.USER_EVENTS_ENABLED_ENV: '0',
                },
            )
            self.assertTrue(h.proof_gap or (h.gap_incidents_unresolved or 0) > 0)
        finally:
            conn.close()

    @_skip_legacy_score_dualwrite
    def test_score_proof_schema_ready_probe_failure_legacy_and_fail_closed(self):
        conn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.commit()
        finally:
            conn.close()
        before = _history_count(self.db_path)
        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1), \
             mock.patch.object(
                 shadow, 'score_proof_schema_ready',
                 side_effect=RuntimeError('schema probe boom'),
             ):
            ee.score_and_update('hello', message_id=14)
        conn = sqlite3.connect(self.db_path)
        try:
            pa = conn.execute('SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
            self.assertNotAlmostEqual(pa, 0.5, places=4)
        finally:
            conn.close()
        self.assertEqual(_history_count(self.db_path), before + 1)
        conn = store.open_store(self.db_path)
        try:
            self.assertTrue(
                shadow.has_unresolved_proof_gap(conn, db_path=self.db_path)
                or shadow.count_gap_sidecar_pending(self.db_path) > 0
                or shadow.has_capture_alert(self.db_path),
            )
            h = shadow.get_shadow_health(
                db_path=self.db_path,
                environ={
                    shadow.SHADOW_ENABLED_ENV: '1',
                    shadow.SCORE_PROOF_ENABLED_ENV: '1',
                    shadow.USER_EVENTS_ENABLED_ENV: '0',
                },
            )
            self.assertTrue(h.proof_gap or (h.gap_incidents_unresolved or 0) > 0)
        finally:
            conn.close()

    @_skip_legacy_score_dualwrite
    def test_duplicate_stale_conflict_skip_history(self):
        conn = store.open_store(self.db_path)
        try:
            shadow.ensure_shadow_schema(conn, db_path=self.db_path)
            conn.commit()
        finally:
            conn.close()
        before = _history_count(self.db_path)
        with mock.patch.object(ee, '_deepseek_score', return_value=_deepseek_scores()), \
             mock.patch.object(ee, '_get_ombre_va', return_value=(None, None)), \
             mock.patch.object(ee, 'get_longing', return_value=0.1):
            ee.score_and_update('first', message_id=50)
            pa_after_first = sqlite3.connect(self.db_path).execute(
                'SELECT pa FROM emotion_state WHERE id=1',
            ).fetchone()[0]
            conn = store.open_store(self.db_path)
            try:
                conn.execute('BEGIN')
                shadow.record_score_proof_in_txn(
                    conn, 60, applied_at=T0, source='unit', score_hash='other',
                )
                conn.execute('COMMIT')
            finally:
                conn.close()
            ee.score_and_update('dup', message_id=50)
            pa_after_dup = sqlite3.connect(self.db_path).execute(
                'SELECT pa FROM emotion_state WHERE id=1',
            ).fetchone()[0]
            self.assertAlmostEqual(pa_after_first, pa_after_dup, places=4)
            ee.score_and_update('stale', message_id=40)
            ee.score_and_update('conflict', message_id=60)
        self.assertEqual(_history_count(self.db_path), before + 1)


_LEGACY_OUTBOX_ROWS = (
    {
        'event_key': 'user_scored:99',
        'event_type': 'user_scored',
        'payload_json': '{"message_id": 99}',
        'payload_hash': 'hash99',
        'created_at': '2026-07-21 12:00:00',
        'attempts': 2,
        'last_error': 'boom',
        'delivered_at': None,
    },
    {
        'event_key': 'user_scored:100',
        'event_type': 'user_scored',
        'payload_json': '{"message_id": 100}',
        'payload_hash': 'hash100',
        'created_at': '2026-07-21 12:00:00',
        'attempts': 0,
        'last_error': None,
        'delivered_at': '2026-07-21 12:01:00',
    },
)


def _create_legacy_event_key_outbox(
    conn: sqlite3.Connection,
    rows: tuple[dict, ...] = _LEGACY_OUTBOX_ROWS,
) -> None:
    if shadow.outbox_schema_ready(conn):
        conn.execute(f'DROP TABLE {shadow.OUTBOX_TABLE}')
    conn.execute(
        f"""
        CREATE TABLE {shadow.OUTBOX_TABLE} (
            event_key TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            delivered_at TEXT
        )
        """
    )
    for row in rows:
        conn.execute(
            f"""
            INSERT INTO {shadow.OUTBOX_TABLE}
                (event_key, event_type, payload_json, payload_hash, created_at,
                 attempts, last_error, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row['event_key'], row['event_type'], row['payload_json'],
                row['payload_hash'], row['created_at'], row['attempts'],
                row['last_error'], row['delivered_at'],
            ),
        )


def _outbox_payload_snapshot(conn: sqlite3.Connection) -> list[tuple]:
    cols = shadow._outbox_table_columns(conn)
    if 'queue_id' in cols:
        order = 'queue_id ASC'
    else:
        order = 'created_at ASC, event_key ASC'
    return conn.execute(
        f"""
        SELECT event_key, event_type, payload_json, payload_hash, created_at,
               attempts, last_error, delivered_at
        FROM {shadow.OUTBOX_TABLE}
        ORDER BY {order}
        """
    ).fetchall()


class _FaultInjectConnection:
    """Proxy sqlite3.Connection for migration fault-injection tests."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fail_at: str,
        fail_state: dict,
    ) -> None:
        self._conn = conn
        self._fail_at = fail_at
        self._fail_state = fail_state

    def execute(self, sql, *args, **kwargs):
        sql_u = ' '.join(str(sql).upper().split())
        mig = shadow.OUTBOX_QUEUE_MIG_TABLE.upper()
        if not self._fail_state['fired']:
            if self._fail_at == 'create' and 'CREATE TABLE' in sql_u and mig in sql_u:
                self._fail_state['fired'] = True
                raise RuntimeError(f'fault at {self._fail_at}')
            if self._fail_at == 'copy' and 'INSERT INTO' in sql_u and mig in sql_u:
                self._fail_state['fired'] = True
                raise RuntimeError(f'fault at {self._fail_at}')
            if (
                self._fail_at == 'drop'
                and 'DROP TABLE' in sql_u
                and shadow.OUTBOX_TABLE.upper() in sql_u
            ):
                self._fail_state['fired'] = True
                raise RuntimeError(f'fault at {self._fail_at}')
            if self._fail_at == 'rename' and 'RENAME TO' in sql_u and mig in sql_u:
                self._fail_state['fired'] = True
                raise RuntimeError(f'fault at {self._fail_at}')
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


class OutboxQueueMigrationAtomicityTests(unittest.TestCase):
    def _open_legacy_db(self) -> tuple[tempfile.TemporaryDirectory, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'mig.db')
        conn = store.open_store(db_path)
        try:
            _create_legacy_event_key_outbox(conn)
        finally:
            conn.close()
        return tmp, db_path

    def _migrate_with_fault(self, db_path: str, fail_at: str) -> list[tuple]:
        conn = store.open_store(db_path)
        before = _outbox_payload_snapshot(conn)
        fail_state = {'fired': False}
        proxy = _FaultInjectConnection(conn, fail_at=fail_at, fail_state=fail_state)
        try:
            with self.assertRaises(RuntimeError):
                shadow._ensure_outbox_queue_id_schema(proxy)
        finally:
            conn.close()
        self.assertTrue(fail_state['fired'], msg=f'fault not triggered for {fail_at}')
        return before

    def _finish_migration(self, db_path: str) -> list[tuple]:
        conn = store.open_store(db_path)
        try:
            shadow._ensure_outbox_queue_id_schema(conn)
            shadow._ensure_outbox_queue_id_schema(conn)
            after = _outbox_payload_snapshot(conn)
            cols = shadow._outbox_table_columns(conn)
            self.assertIn('queue_id', cols)
            self.assertFalse(
                shadow._shadow_table_exists(conn, shadow.OUTBOX_QUEUE_MIG_TABLE),
            )
            return after
        finally:
            conn.close()

    def test_migration_idempotent_preserves_rows(self):
        _, db_path = self._open_legacy_db()
        before = self._finish_migration(db_path)
        self.assertEqual(len(before), len(_LEGACY_OUTBOX_ROWS))

    def test_fault_injection_then_retry_preserves_rows(self):
        for fail_at in ('create', 'copy', 'drop', 'rename'):
            with self.subTest(fail_at=fail_at):
                _, db_path = self._open_legacy_db()
                before = self._migrate_with_fault(db_path, fail_at)
                after = self._finish_migration(db_path)
                self.assertEqual(before, after)

    def test_recovers_orphan_queue_mig_after_empty_recreate(self):
        _, db_path = self._open_legacy_db()
        conn = store.open_store(db_path)
        try:
            before = _outbox_payload_snapshot(conn)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                f"""
                CREATE TABLE {shadow.OUTBOX_QUEUE_MIG_TABLE} (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {shadow.OUTBOX_QUEUE_MIG_TABLE}
                    (event_key, event_type, payload_json, payload_hash, created_at,
                     attempts, last_error, delivered_at)
                SELECT event_key, event_type, payload_json, payload_hash, created_at,
                       attempts, last_error, delivered_at
                FROM {shadow.OUTBOX_TABLE}
                ORDER BY created_at ASC, event_key ASC
                """
            )
            conn.execute(f'DROP TABLE {shadow.OUTBOX_TABLE}')
            conn.execute(
                f"""
                CREATE TABLE {shadow.OUTBOX_TABLE} (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT
                )
                """
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        after = self._finish_migration(db_path)
        self.assertEqual(before, after)

    def test_create_interrupt_legacy_full_mig_empty_preserves_rows(self):
        _, db_path = self._open_legacy_db()
        conn = store.open_store(db_path)
        try:
            before = _outbox_payload_snapshot(conn)
            conn.execute(
                f"""
                CREATE TABLE {shadow.OUTBOX_QUEUE_MIG_TABLE} (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT
                )
                """
            )
        finally:
            conn.close()
        conn = store.open_store(db_path)
        try:
            shadow._recover_orphan_outbox_queue_mig(conn)
            self.assertFalse(
                shadow._shadow_table_exists(conn, shadow.OUTBOX_QUEUE_MIG_TABLE),
            )
            self.assertNotIn('queue_id', shadow._outbox_table_columns(conn))
            self.assertEqual(_outbox_payload_snapshot(conn), before)
        finally:
            conn.close()
        after = self._finish_migration(db_path)
        self.assertEqual(before, after)

    def test_copy_interrupt_identical_tables_recovers_safely(self):
        _, db_path = self._open_legacy_db()
        conn = store.open_store(db_path)
        try:
            before = _outbox_payload_snapshot(conn)
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                f"""
                CREATE TABLE {shadow.OUTBOX_QUEUE_MIG_TABLE} (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {shadow.OUTBOX_QUEUE_MIG_TABLE}
                    (event_key, event_type, payload_json, payload_hash, created_at,
                     attempts, last_error, delivered_at)
                SELECT event_key, event_type, payload_json, payload_hash, created_at,
                       attempts, last_error, delivered_at
                FROM {shadow.OUTBOX_TABLE}
                ORDER BY created_at ASC, event_key ASC
                """
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        conn = store.open_store(db_path)
        try:
            shadow._recover_orphan_outbox_queue_mig(conn)
            self.assertFalse(
                shadow._shadow_table_exists(conn, shadow.OUTBOX_QUEUE_MIG_TABLE),
            )
            self.assertIn('queue_id', shadow._outbox_table_columns(conn))
            self.assertEqual(_outbox_payload_snapshot(conn), before)
            shadow._recover_orphan_outbox_queue_mig(conn)
            self.assertFalse(
                shadow._shadow_table_exists(conn, shadow.OUTBOX_QUEUE_MIG_TABLE),
            )
        finally:
            conn.close()

    def test_both_nonempty_divergent_fail_closed_keeps_both_tables(self):
        _, db_path = self._open_legacy_db()
        conn = store.open_store(db_path)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                f"""
                CREATE TABLE {shadow.OUTBOX_QUEUE_MIG_TABLE} (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    delivered_at TEXT
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {shadow.OUTBOX_QUEUE_MIG_TABLE}
                    (event_key, event_type, payload_json, payload_hash, created_at,
                     attempts, last_error, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    'user_scored:199', 'user_scored', '{"message_id": 199}',
                    'different', '2026-07-21 12:00:00', 0, None, None,
                ),
            )
            conn.execute('COMMIT')
        finally:
            conn.close()
        conn = store.open_store(db_path)
        try:
            with self.assertRaises(store.StoreError) as ctx:
                shadow._recover_orphan_outbox_queue_mig(conn)
            self.assertIn('reconciliation_conflict', str(ctx.exception))
            self.assertTrue(shadow.outbox_schema_ready(conn))
            self.assertTrue(
                shadow._shadow_table_exists(conn, shadow.OUTBOX_QUEUE_MIG_TABLE),
            )
            self.assertGreater(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.OUTBOX_TABLE}'
                ).fetchone()[0],
                0,
            )
            self.assertGreater(
                conn.execute(
                    f'SELECT COUNT(*) FROM {shadow.OUTBOX_QUEUE_MIG_TABLE}'
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
