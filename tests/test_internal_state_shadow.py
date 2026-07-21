"""Phase 1A-4a — Shadow 基础设施与生产 bootstrap（临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway / wake。
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state_shadow as shadow
import internal_state_store as store


T0 = '2026-07-21 12:00:00'
OFF = {shadow.SHADOW_ENABLED_ENV: '0'}
ON = {shadow.SHADOW_ENABLED_ENV: '1'}


def _snapshot(**overrides):
    base = SimpleNamespace(
        observed_at=T0,
        affect=SimpleNamespace(
            pa=0.55, na=0.25, valence=0.6, arousal=0.4, mood_word='平静'),
        bond=SimpleNamespace(intimacy=0.50, passion=0.60, commitment=0.70),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.50, curiosity=0.20, reflection=0.30, social=0.10,
            duty=0.15, libido=0.10, stress=0.20, fatigue=0.40),
        diagnostics=SimpleNamespace(
            source_timestamps={
                'legacy.emotion_state.p_updated_at': '2026-07-21 06:00:00',
            },
            source_health={
                'clock_reliable': True,
                'clock_reason': 'ok',
                'emotion_state': True,
                'drive_state': True,
                'desire_state': True,
            },
            warnings=(),
        ),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _seed_legacy_rows(conn: sqlite3.Connection, *, pa: float = 0.55) -> None:
    conn.executescript(
        f"""
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
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            author TEXT, content TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS wake_log (
            id INTEGER PRIMARY KEY,
            action TEXT, woke_at TEXT
        );
        DELETE FROM emotion_state;
        DELETE FROM drive_state;
        DELETE FROM desire_state;
        DELETE FROM chat_messages;
        DELETE FROM wake_log;
        INSERT INTO emotion_state VALUES (
            1, {pa}, 0.25, 0.6, 0.4, '平静', 0.1,
            '2026-07-21 11:00:00', '2026-07-21 11:00:00',
            0.5, 0.6, 0.7, '2026-07-21 06:00:00', '2026-07-21 06:00:00');
        INSERT INTO drive_state VALUES (
            1, 0.5, 0.2, 0.3, 0.1, 0.15, 0.1, 0.2, 0.4,
            '2026-07-21 10:00:00');
        INSERT INTO desire_state VALUES (
            1, 0.2, 0.3, 0.15, 0.1, 0.1, 0.2, 0.4,
            '2026-07-21 10:00:00', NULL);
        INSERT INTO chat_messages VALUES (
            1, 'hayana', 'hi', '2026-07-21 11:00:00');
        """
    )


def _insert_score_applied(
    conn: sqlite3.Connection, message_id: int, *, source: str = 'unit_test',
) -> None:
    shadow.ensure_shadow_schema(conn)
    conn.execute('BEGIN')
    try:
        shadow.record_score_proof_in_txn(
            conn, message_id, applied_at=T0, source=source,
        )
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise


def _boot_test(db_path: str, *, watermark: int = 77) -> shadow.ShadowResult:
    return shadow._ensure_bootstrapped_for_test(
        db_path=db_path,
        environ=ON,
        snapshot=_snapshot(),
        last_scored_message_id=watermark,
    )


class FlagGateTests(unittest.TestCase):
    def test_default_disabled_and_zero_db_ops(self):
        self.assertFalse(shadow.is_shadow_enabled(environ={}))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'never.db'
            r = shadow.ensure_bootstrapped(db_path=str(path), environ=OFF)
            self.assertEqual(r.status, 'disabled')
            self.assertFalse(path.exists())
            h = shadow.get_shadow_health(db_path=str(path), environ=OFF)
            self.assertFalse(h.enabled)
            self.assertFalse(path.exists())

    def test_enabled_only_with_one(self):
        self.assertTrue(shadow.is_shadow_enabled(environ=ON))
        self.assertFalse(shadow.is_shadow_enabled(environ={
            shadow.SHADOW_ENABLED_ENV: 'true',
        }))


class StrictSnapshotTests(unittest.TestCase):
    def test_validate_rejects_missing_affect(self):
        snap = _snapshot(
            affect=SimpleNamespace(
                pa=None, na=0.2, valence=0.6, arousal=0.4, mood_word='平静'),
        )
        with self.assertRaises(store.StoreError) as ctx:
            shadow.validate_bootstrap_snapshot(snap)
        self.assertIn('affect.pa', str(ctx.exception))

    def test_validate_rejects_out_of_range(self):
        snap = _snapshot(
            affect=SimpleNamespace(
                pa=1.5, na=0.2, valence=0.6, arousal=0.4, mood_word='平静'),
        )
        with self.assertRaises(store.StoreError) as ctx:
            shadow.validate_bootstrap_snapshot(snap)
        self.assertIn('out of [0,1]', str(ctx.exception))

    def test_validate_rejects_unreliable_clock(self):
        snap = _snapshot()
        snap.diagnostics.source_health = {
            'clock_reliable': False,
            'clock_reason': 'clock_unreadable',
            'emotion_state': True,
            'drive_state': True,
        }
        with self.assertRaises(store.StoreError) as ctx:
            shadow.validate_bootstrap_snapshot(snap)
        self.assertIn('unreliable interaction clock', str(ctx.exception))

    def test_ensure_bootstrapped_refuses_default_wash(self):
        """emotion 读失败不得用默认人格成功 bootstrap。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'wash.db')
        conn = store.open_store(db_path)
        try:
            _seed_legacy_rows(conn)
            _insert_score_applied(conn, 5)
            conn.execute('DELETE FROM emotion_state')
        finally:
            conn.close()
        r = shadow.ensure_bootstrapped(db_path=db_path, environ=ON)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'failed')
        self.assertIn('emotion_state', r.error or '')
        conn = store.open_store(db_path)
        try:
            self.assertIsNone(store.read_state(conn))
            self.assertIsNone(store.read_event(conn, 'bootstrap:initial'))
        finally:
            conn.close()


class ScoreProofTxnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'proof.db')
        self.conn = store.open_store(self.db_path)
        _seed_legacy_rows(self.conn)
        shadow.ensure_shadow_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_without_transaction_fails_and_writes_nothing(self):
        with self.assertRaises(store.StoreError) as ctx:
            shadow.record_score_proof_in_txn(
                self.conn, 1, applied_at=T0, source='x',
            )
        self.assertIn('active caller-owned transaction', str(ctx.exception))
        n = self.conn.execute(
            f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE}'
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_rollback_hides_emotion_and_proof(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.conn.execute(
            'UPDATE emotion_state SET pa=0.91 WHERE id=1')
        shadow.record_score_proof_in_txn(
            self.conn, 101, applied_at=T0, source='atomic')
        self.conn.execute('ROLLBACK')

        pa = self.conn.execute(
            'SELECT pa FROM emotion_state WHERE id=1').fetchone()[0]
        self.assertAlmostEqual(pa, 0.55, places=4)
        n = self.conn.execute(
            f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE}'
        ).fetchone()[0]
        self.assertEqual(n, 0)

    def test_identical_proof_retry_idempotent(self):
        self.conn.execute('BEGIN')
        shadow.record_score_proof_in_txn(
            self.conn, 7, applied_at=T0, source='same')
        shadow.record_score_proof_in_txn(
            self.conn, 7, applied_at=T0, source='same')
        self.conn.execute('COMMIT')
        n = self.conn.execute(
            f'SELECT COUNT(*) FROM {shadow.SCORE_APPLIED_TABLE}'
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_different_proof_content_conflicts(self):
        self.conn.execute('BEGIN')
        shadow.record_score_proof_in_txn(
            self.conn, 7, applied_at=T0, source='a')
        self.conn.execute('COMMIT')
        self.conn.execute('BEGIN')
        with self.assertRaises(store.StoreError) as ctx:
            shadow.record_score_proof_in_txn(
                self.conn, 7, applied_at=T0, source='b')
        self.assertIn('score proof conflict', str(ctx.exception))
        self.conn.execute('ROLLBACK')


class SameTxnCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'txn.db')
        self.conn = store.open_store(self.db_path)
        _seed_legacy_rows(self.conn, pa=0.55)
        _insert_score_applied(self.conn, 100)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_bundle_reads_watermark_and_emotion_together(self):
        bundle = shadow.capture_bootstrap_bundle(self.db_path)
        self.assertEqual(bundle.watermark, 100)
        self.assertAlmostEqual(bundle.snapshot.affect.pa, 0.55, places=4)
        self.assertTrue(bundle.snapshot.diagnostics.source_health['clock_reliable'])

    def test_atomic_score_proof_visible_together(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.conn.execute(
            'UPDATE emotion_state SET pa=0.91, valence=0.88 WHERE id=1')
        shadow.record_score_proof_in_txn(
            self.conn, 101, applied_at=T0, source='atomic_score')
        self.conn.execute('COMMIT')

        bundle = shadow.capture_bootstrap_bundle(self.db_path)
        self.assertEqual(bundle.watermark, 101)
        self.assertAlmostEqual(bundle.snapshot.affect.pa, 0.91, places=4)
        self.assertEqual(bundle.watermark_row_source, 'atomic_score')

    def test_interleaved_commit_cannot_tear_read_txn(self):
        barrier = threading.Event()
        done = threading.Event()
        bundles: list[shadow.BootstrapBundle] = []
        errors: list[BaseException] = []

        def writer():
            w = store.open_store(self.db_path)
            try:
                w.execute('BEGIN IMMEDIATE')
                w.execute(
                    'UPDATE emotion_state SET pa=0.99 WHERE id=1')
                shadow.record_score_proof_in_txn(
                    w, 101, applied_at=T0, source='race')
                barrier.set()
                done.wait(timeout=5)
                w.execute('COMMIT')
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                w.close()

        def reader():
            try:
                barrier.wait(timeout=5)
                bundles.append(shadow.capture_bootstrap_bundle(self.db_path))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()

        tw = threading.Thread(target=writer)
        tr = threading.Thread(target=reader)
        tw.start()
        tr.start()
        tw.join(timeout=10)
        tr.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].watermark, 100)
        self.assertAlmostEqual(bundles[0].snapshot.affect.pa, 0.55, places=4)

        after = shadow.capture_bootstrap_bundle(self.db_path)
        self.assertEqual(after.watermark, 101)
        self.assertAlmostEqual(after.snapshot.affect.pa, 0.99, places=4)


class LinearizedBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'lin.db')
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_rows(conn, pa=0.55)
            _insert_score_applied(conn, 100)
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_observed_at_taken_after_lock_absorbs_late_writer(self):
        """拿锁后才冻结 observed_at；吸收 writer 的较晚 applied_at 与新状态。

        旧顺序（先 now 再 BEGIN）会在排队期间让 proof.applied_at 跑到未来，
        触发 fail-closed；新顺序必须成功且 observed_at >= applied_at。
        """
        import datetime
        import time

        writer_holding = threading.Event()
        late_applied_at_box: list[str] = []
        results: list[shadow.ShadowResult] = []
        errors: list[BaseException] = []

        def writer():
            w = store.open_store(self.db_path)
            try:
                w.execute('BEGIN IMMEDIATE')
                # 模拟「排队等待期间」评分提交：applied_at 用真实墙钟
                time.sleep(1.1)
                late = (
                    datetime.datetime.utcnow() + datetime.timedelta(hours=8)
                ).replace(microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
                late_applied_at_box.append(late)
                w.execute('UPDATE emotion_state SET pa=0.99 WHERE id=1')
                shadow.record_score_proof_in_txn(
                    w, 101, applied_at=late, source='inflight')
                writer_holding.set()
                # 给 bootstrap 时间在 BEGIN IMMEDIATE 上排队
                time.sleep(0.8)
                w.execute('COMMIT')
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                writer_holding.set()
            finally:
                w.close()

        def boot():
            try:
                # 尽早启动，与 writer 争锁；若旧顺序会先冻结过早的 observed_at
                results.append(
                    shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        tw = threading.Thread(target=writer)
        tb = threading.Thread(target=boot)
        tw.start()
        # bootstrap 稍晚启动，确保先撞上 writer 持有的锁
        time.sleep(0.05)
        tb.start()
        tw.join(timeout=20)
        tb.join(timeout=20)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok, msg=results[0].error)
        self.assertEqual(results[0].status, 'applied')
        self.assertEqual(len(late_applied_at_box), 1)
        late = late_applied_at_box[0]

        conn = store.open_store(self.db_path)
        try:
            st = store.read_state(conn)
            ev = store.read_event(conn, 'bootstrap:initial')
            payload = json.loads(ev['payload_json'])
            observed = payload['observed_at']
            proof_at = payload['watermark_proof']['applied_at']
            self.assertEqual(proof_at, late)
            self.assertGreaterEqual(observed, proof_at)
            self.assertEqual(st['last_scored_message_id'], 101)
            self.assertAlmostEqual(st['pa'], 0.99, places=4)
            self.assertEqual(st['p_updated_at'], observed)
            self.assertEqual(st['i_updated_at'], observed)
            self.assertEqual(st['drives_updated_at'], observed)
            self.assertTrue(shadow._bootstrap_provenance_ok(st, ev))
        finally:
            conn.close()


class WatermarkAndBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'shadow.db')
        self.conn = store.open_store(self.db_path)
        _seed_legacy_rows(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_empty_ledger_fail_closed_no_half_state(self):
        shadow.ensure_shadow_schema(self.conn)
        r = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'failed')
        self.assertIsNone(store.read_state(self.conn))
        self.assertIsNone(store.read_event(self.conn, 'bootstrap:initial'))

    def test_enabled_bootstrap_writes_structured_proof(self):
        _insert_score_applied(self.conn, 77, source='prod_score')
        journal_before = store.get_journal_mode(self.conn)
        r = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertTrue(r.ok, msg=r.error)
        self.assertEqual(r.status, 'applied')
        st = store.read_state(self.conn)
        self.assertEqual(st['last_scored_message_id'], 77)
        ev = store.read_event(self.conn, 'bootstrap:initial')
        payload = json.loads(ev['payload_json'])
        self.assertEqual(payload['last_scored_message_id'], 77)
        self.assertEqual(
            payload['watermark_proof']['resolver'], shadow.WATERMARK_SOURCE)
        self.assertEqual(payload['watermark_proof']['row_source'], 'prod_score')
        self.assertEqual(
            payload['capture_mode'], shadow.CAPTURE_MODE_PRODUCTION)
        self.assertEqual(ev['source_id'], shadow.PRODUCTION_BOOTSTRAP_SOURCE_ID)
        self.assertEqual(store.get_journal_mode(self.conn), journal_before)
        self.assertTrue(shadow._bootstrap_provenance_ok(st, ev))

    def test_production_capture_bootstrap_path(self):
        _insert_score_applied(self.conn, 42, source='prod_score')
        r = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertTrue(r.ok, msg=r.error)
        self.assertEqual(r.status, 'applied')
        st = store.read_state(self.conn)
        self.assertEqual(st['last_scored_message_id'], 42)
        self.assertAlmostEqual(st['pa'], 0.55, places=4)

    def test_duplicate_bootstrap_no_state_change(self):
        _insert_score_applied(self.conn, 5)
        r1 = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertEqual(r1.status, 'applied')
        st1 = dict(store.read_state(self.conn))
        r2 = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertEqual(r2.status, 'already_bootstrapped')
        st2 = store.read_state(self.conn)
        self.assertEqual(st1['state_version'], st2['state_version'])

    def test_test_injection_not_provenance_ok(self):
        _insert_score_applied(self.conn, 10)
        r = _boot_test(self.db_path, watermark=10)
        self.assertEqual(r.status, 'applied')
        st = store.read_state(self.conn)
        ev = store.read_event(self.conn, 'bootstrap:initial')
        self.assertTrue(shadow.is_bootstrapped(self.conn))
        self.assertFalse(shadow._bootstrap_provenance_ok(st, ev))
        h = shadow.get_shadow_health(db_path=self.db_path, environ=ON)
        self.assertTrue(h.bootstrapped)
        self.assertFalse(h.provenance_ok)

    def test_different_watermark_conflict(self):
        _insert_score_applied(self.conn, 10)
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON).status,
            'applied',
        )
        r = store.bootstrap_from_snapshot(
            self.conn, _snapshot(),
            last_scored_message_id=11,
            last_scored_message_id_source=shadow.WATERMARK_SOURCE,
        )
        self.assertEqual(r.status, 'idempotency_conflict')
        self.assertEqual(
            store.read_state(self.conn)['last_scored_message_id'], 10)

    def test_legacy_tables_unchanged_by_bootstrap(self):
        _insert_score_applied(self.conn, 3)
        before = self.conn.execute(
            'SELECT pa, valence FROM emotion_state WHERE id=1').fetchone()
        shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        after = self.conn.execute(
            'SELECT pa, valence FROM emotion_state WHERE id=1').fetchone()
        self.assertEqual(tuple(before), tuple(after))


class PhaseABoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'phase.db')
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_rows(conn)
            _insert_score_applied(conn, 20)
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_event_wrapper_requires_prior_bootstrap(self):
        r = shadow.observe_user_message_shadow(
            message_id=21, text='你好', created_at=T0,
            previous_user_at=None, db_path=self.db_path, environ=ON,
        )
        self.assertEqual(r.status, 'bootstrap_required')
        self.assertFalse(r.ok)
        conn = store.open_store(self.db_path)
        try:
            self.assertIsNone(store.read_state(conn))
        finally:
            conn.close()

    def test_first_event_after_explicit_bootstrap_does_not_double(self):
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON).status,
            'applied',
        )
        r = shadow.observe_scored_shadow(
            message_id=20,
            scores={
                'valence': 0.8, 'arousal': 0.4, 'mood_word': '开心',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at=T0,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(r.status, 'stale_skipped')
        conn = store.open_store(self.db_path)
        try:
            st = store.read_state(conn)
            self.assertEqual(st['last_scored_message_id'], 20)
            self.assertAlmostEqual(st['valence'], 0.6, places=4)
        finally:
            conn.close()


class AdapterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'adapt.db')
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_rows(conn)
            _insert_score_applied(conn, 20)
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_scores_none_no_throw_no_db(self):
        path = Path(self.tmp.name) / 'ghost.db'
        main_ok = True
        try:
            r = shadow.observe_scored_shadow(
                message_id=1,
                scores=None,  # type: ignore[arg-type]
                scored_at=T0,
                db_path=str(path),
                environ=OFF,
            )
        except Exception:  # noqa: BLE001
            main_ok = False
            raise
        self.assertTrue(main_ok)
        self.assertEqual(r.status, 'disabled')
        self.assertFalse(path.exists())

    def test_enabled_scores_none_returns_failed(self):
        shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        r = shadow.observe_scored_shadow(
            message_id=21,
            scores=None,  # type: ignore[arg-type]
            scored_at=T0,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(r.status, 'failed')
        self.assertFalse(r.ok)
        self.assertIn('NoneType', r.error or '')

    def test_wrapper_catches_bad_message_id(self):
        shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        r = shadow.observe_scored_shadow(
            message_id=True,  # type: ignore[arg-type]
            scores={
                'valence': 0.5, 'arousal': 0.4, 'mood_word': 'x',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at=T0,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(r.status, 'failed')


class ProvenanceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'prov.db')
        conn = store.open_store(self.db_path)
        try:
            _seed_legacy_rows(conn)
            _insert_score_applied(conn, 20, source='prod_score')
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_watermark_growth_keeps_provenance_and_wrappers(self):
        """bootstrap 水位 20 → scored 21 applied → provenance 仍 True。"""
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON).status,
            'applied',
        )
        conn = store.open_store(self.db_path)
        try:
            observed = store.read_state(conn)['p_updated_at']
        finally:
            conn.close()
        # 事件时间必须不早于 bootstrap 物化时钟
        import datetime as _dt
        base = _dt.datetime.strptime(observed, '%Y-%m-%d %H:%M:%S')
        scored_at = (base + _dt.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        created_at = (base + _dt.timedelta(seconds=2)).strftime('%Y-%m-%d %H:%M:%S')
        outcome_at = (base + _dt.timedelta(seconds=3)).strftime('%Y-%m-%d %H:%M:%S')

        r = shadow.observe_scored_shadow(
            message_id=21,
            scores={
                'valence': 0.8, 'arousal': 0.4, 'mood_word': '开心',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at=scored_at,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(r.status, 'applied', msg=r.error)
        h = shadow.get_shadow_health(db_path=self.db_path, environ=ON)
        self.assertTrue(h.provenance_ok)
        self.assertEqual(h.last_scored_message_id, 21)

        # 后续 user_rule / wake_outcome 仍可工作
        r_user = shadow.observe_user_message_shadow(
            message_id=22, text='你好', created_at=created_at,
            previous_user_at=scored_at, db_path=self.db_path, environ=ON,
        )
        self.assertEqual(r_user.status, 'applied', msg=r_user.error)
        r_out = shadow.apply_outcome_shadow(
            wake_run_id='wake-prov-1',
            executor_action='none',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=2.0,
            outcome_at=outcome_at,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(r_out.status, 'applied', msg=r_out.error)
        self.assertTrue(
            shadow.get_shadow_health(
                db_path=self.db_path, environ=ON).provenance_ok,
        )

    def test_test_injection_wrappers_rejected(self):
        r = _boot_test(self.db_path, watermark=20)
        self.assertEqual(r.status, 'applied')
        before = self._snapshot_db()
        denied = shadow.observe_user_message_shadow(
            message_id=30, text='x', created_at=T0,
            previous_user_at=None, db_path=self.db_path, environ=ON,
        )
        self.assertEqual(denied.status, 'bootstrap_provenance_invalid')
        self.assertFalse(denied.ok)
        self.assertEqual(self._snapshot_db(), before)

    def test_corrupt_capture_mode_rejects_without_mutation(self):
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON).status,
            'applied',
        )
        conn = store.open_store(self.db_path)
        try:
            ev = store.read_event(conn, 'bootstrap:initial')
            payload = json.loads(ev['payload_json'])
            payload['capture_mode'] = 'tampered'
            conn.execute(
                'UPDATE internal_state_events SET payload_json=?, payload_hash=? '
                'WHERE event_key=?',
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(',', ':')),
                    'deadbeef',
                    'bootstrap:initial',
                ),
            )
        finally:
            conn.close()
        before = self._snapshot_db()
        denied = shadow.observe_scored_shadow(
            message_id=21,
            scores={
                'valence': 0.8, 'arousal': 0.4, 'mood_word': '开心',
                'passion_delta': 0.0, 'intimacy_delta': 0.0, 'source': 't',
            },
            scored_at=T0,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(denied.status, 'bootstrap_provenance_invalid')
        self.assertEqual(self._snapshot_db(), before)

        again = shadow.ensure_bootstrapped(db_path=self.db_path, environ=ON)
        self.assertFalse(again.ok)
        self.assertEqual(again.status, 'bootstrap_provenance_invalid')
        self.assertEqual(self._snapshot_db(), before)

    def test_rewound_watermark_invalidates_provenance(self):
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON).status,
            'applied',
        )
        conn = store.open_store(self.db_path)
        try:
            # 模拟当前水位回拨到初始水位之下
            conn.execute(
                'UPDATE internal_state_v3 SET last_scored_message_id=19 WHERE id=1'
            )
            st = store.read_state(conn)
            ev = store.read_event(conn, 'bootstrap:initial')
            self.assertFalse(shadow._bootstrap_provenance_ok(st, ev))
        finally:
            conn.close()
        before = self._snapshot_db()
        denied = shadow.apply_outcome_shadow(
            wake_run_id='wake-bad',
            executor_action='none',
            desire_action=None,
            fired_drive=None,
            desire_driven=False,
            user_idle_hours=1.0,
            outcome_at=T0,
            db_path=self.db_path,
            environ=ON,
        )
        self.assertEqual(denied.status, 'bootstrap_provenance_invalid')
        self.assertEqual(self._snapshot_db(), before)

    def _snapshot_db(self):
        conn = store.open_store(self.db_path)
        try:
            st = store.read_state(conn)
            n_events = conn.execute(
                'SELECT COUNT(*) FROM internal_state_events'
            ).fetchone()[0]
            return (
                None if st is None else (
                    st['state_version'],
                    st['last_scored_message_id'],
                    st['pa'],
                    st['valence'],
                ),
                n_events,
            )
        finally:
            conn.close()


class ConcurrencyBootstrapTests(unittest.TestCase):
    def test_concurrent_bootstrap_one_applied(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'conc.db')
        setup = store.open_store(db_path)
        try:
            _seed_legacy_rows(setup)
            _insert_score_applied(setup, 99)
        finally:
            setup.close()

        results: dict[str, shadow.ShadowResult] = {}
        errors: list[BaseException] = []

        def run(label: str):
            try:
                results[label] = shadow.ensure_bootstrapped(
                    db_path=db_path, environ=ON,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(x,)) for x in 'abc']
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertTrue(all(results[k].ok for k in results))
        conn = store.open_store(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='bootstrap:initial'"
            ).fetchone()[0]
            self.assertEqual(n, 1)
            self.assertEqual(
                store.read_state(conn)['last_scored_message_id'], 99)
            st = store.read_state(conn)
            ev = store.read_event(conn, 'bootstrap:initial')
            self.assertTrue(shadow._bootstrap_provenance_ok(st, ev))
        finally:
            conn.close()


class GuardTests(unittest.TestCase):
    def test_shadow_module_forbids_legacy_imports(self):
        src = Path(ROOT, 'internal_state_shadow.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        forbidden = {
            'emotion_engine', 'drive_engine', 'desire', 'gateway', 'app',
            'wake', 'config_store',
        }
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split('.')[0])
        self.assertFalse(forbidden & found, msg=f'{forbidden & found}')

    def test_no_production_call_sites(self):
        for name in ('gateway.py', 'app.py'):
            path = Path(ROOT, name)
            if not path.exists():
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('internal_state_shadow', text)
        wake_dir = Path(ROOT, 'wake')
        if wake_dir.is_dir():
            for path in wake_dir.rglob('*.py'):
                text = path.read_text(encoding='utf-8', errors='replace')
                self.assertNotIn('internal_state_shadow', text)

    def test_public_ensure_bootstrapped_has_no_injection_kwargs(self):
        import inspect
        sig = inspect.signature(shadow.ensure_bootstrapped)
        self.assertNotIn('snapshot', sig.parameters)
        self.assertNotIn('last_scored_message_id', sig.parameters)


if __name__ == '__main__':
    unittest.main()
