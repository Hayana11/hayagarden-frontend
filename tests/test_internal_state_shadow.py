"""Phase 1A-4a — Shadow 基础设施与生产 bootstrap（临时 SQLite）。

禁止 import emotion_engine / drive_engine / desire / gateway / wake。
禁止从 gateway 调用；本套件只测 adapter。
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
        diagnostics=SimpleNamespace(source_timestamps={
            'legacy.emotion_state.p_updated_at': '2026-07-21 06:00:00',
        }),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _seed_legacy_rows(conn: sqlite3.Connection) -> None:
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
        DELETE FROM emotion_state;
        DELETE FROM drive_state;
        DELETE FROM desire_state;
        INSERT INTO emotion_state VALUES (
            1, 0.55, 0.25, 0.6, 0.4, '平静', 0.1,
            '2026-07-21 11:00:00', '2026-07-21 11:00:00',
            0.5, 0.6, 0.7, '2026-07-21 06:00:00', '2026-07-21 06:00:00');
        INSERT INTO drive_state VALUES (
            1, 0.5, 0.2, 0.3, 0.1, 0.15, 0.1, 0.2, 0.4,
            '2026-07-21 10:00:00');
        INSERT INTO desire_state VALUES (
            1, 0.2, 0.3, 0.15, 0.1, 0.1, 0.2, 0.4,
            '2026-07-21 10:00:00', NULL);
        """
    )


def _insert_score_applied(conn: sqlite3.Connection, message_id: int) -> None:
    shadow.ensure_shadow_schema(conn)
    conn.execute(
        f"""
        INSERT OR REPLACE INTO {shadow.SCORE_APPLIED_TABLE}
            (message_id, applied_at, source)
        VALUES (?, ?, ?)
        """,
        (message_id, T0, 'unit_test'),
    )


class FlagGateTests(unittest.TestCase):
    def test_default_disabled_and_zero_db_ops(self):
        self.assertFalse(shadow.is_shadow_enabled(environ={}))
        self.assertFalse(shadow.is_shadow_enabled(environ=OFF))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'never.db'
            r = shadow.ensure_bootstrapped(db_path=str(path), environ=OFF)
            self.assertEqual(r.status, 'disabled')
            self.assertTrue(r.ok)
            self.assertFalse(path.exists())
            h = shadow.get_shadow_health(db_path=str(path), environ=OFF)
            self.assertFalse(h.enabled)
            self.assertFalse(h.bootstrapped)
            self.assertFalse(path.exists())

    def test_enabled_only_with_one(self):
        self.assertTrue(shadow.is_shadow_enabled(environ=ON))
        self.assertFalse(shadow.is_shadow_enabled(environ={
            shadow.SHADOW_ENABLED_ENV: 'true',
        }))


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
        with self.assertRaises(store.StoreError) as ctx:
            shadow.resolve_scored_watermark(self.conn)
        self.assertIn('empty', str(ctx.exception))
        r = shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot(),
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'failed')
        self.assertIsNone(store.read_state(self.conn))
        self.assertIsNone(store.read_event(self.conn, 'bootstrap:initial'))

    def test_missing_ledger_fail_closed(self):
        store.ensure_schema(self.conn)
        with self.assertRaises(store.StoreError) as ctx:
            shadow.resolve_scored_watermark(self.conn)
        self.assertIn('missing', str(ctx.exception))

    def test_enabled_bootstrap_writes_watermark(self):
        _insert_score_applied(self.conn, 77)
        journal_before = store.get_journal_mode(self.conn)
        r = shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot(),
        )
        self.assertTrue(r.ok)
        self.assertEqual(r.status, 'applied')
        st = store.read_state(self.conn)
        self.assertEqual(st['last_scored_message_id'], 77)
        payload = json.loads(
            store.read_event(self.conn, 'bootstrap:initial')['payload_json'])
        self.assertEqual(payload['last_scored_message_id'], 77)
        self.assertEqual(
            payload['last_scored_message_id_source'],
            shadow.WATERMARK_SOURCE,
        )
        self.assertEqual(store.get_journal_mode(self.conn), journal_before)

    def test_duplicate_bootstrap_no_state_change(self):
        _insert_score_applied(self.conn, 5)
        r1 = shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot())
        self.assertEqual(r1.status, 'applied')
        st1 = dict(store.read_state(self.conn))
        r2 = shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot())
        self.assertEqual(r2.status, 'already_bootstrapped')
        st2 = store.read_state(self.conn)
        self.assertEqual(st1['state_version'], st2['state_version'])
        self.assertEqual(st1['last_scored_message_id'], st2['last_scored_message_id'])

    def test_different_watermark_conflict(self):
        _insert_score_applied(self.conn, 10)
        self.assertEqual(
            shadow.ensure_bootstrapped(
                db_path=self.db_path, environ=ON, snapshot=_snapshot(),
            ).status,
            'applied',
        )
        # 直接用 store 同 key 不同 watermark
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
            'SELECT pa, valence, mood_word FROM emotion_state WHERE id=1'
        ).fetchone()
        drive_before = self.conn.execute(
            'SELECT attachment, fatigue FROM drive_state WHERE id=1'
        ).fetchone()
        shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot())
        after = self.conn.execute(
            'SELECT pa, valence, mood_word FROM emotion_state WHERE id=1'
        ).fetchone()
        drive_after = self.conn.execute(
            'SELECT attachment, fatigue FROM drive_state WHERE id=1'
        ).fetchone()
        self.assertEqual(tuple(before), tuple(after))
        self.assertEqual(tuple(drive_before), tuple(drive_after))


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
                    db_path=db_path, environ=ON, snapshot=_snapshot(),
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=('a',)),
            threading.Thread(target=run, args=('b',)),
            threading.Thread(target=run, args=('c',)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        statuses = {results[k].status for k in results}
        self.assertTrue(
            statuses <= {'applied', 'duplicate', 'already_bootstrapped'})
        self.assertTrue(all(results[k].ok for k in results))
        conn = store.open_store(db_path)
        try:
            n_boot = conn.execute(
                "SELECT COUNT(*) FROM internal_state_events "
                "WHERE event_key='bootstrap:initial'"
            ).fetchone()[0]
            self.assertEqual(n_boot, 1)
            st = store.read_state(conn)
            self.assertEqual(st['last_scored_message_id'], 99)
            self.assertEqual(st['state_version'], 0)
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

    def test_wrapper_disabled_skips(self):
        r = shadow.observe_user_message_shadow(
            message_id=21, text='你好', created_at=T0,
            previous_user_at=None, db_path=self.db_path, environ=OFF,
        )
        self.assertEqual(r.status, 'disabled')
        self.assertTrue(r.ok)

    def test_wrapper_catches_exception_main_flow_ok(self):
        # 开启但 bootstrap 后用非法参数；adapter 不得抛
        shadow.ensure_bootstrapped(
            db_path=self.db_path, environ=ON, snapshot=_snapshot())
        main_ok = True
        try:
            r = shadow.observe_scored_shadow(
                message_id=True,  # type: ignore[arg-type]
                scores={'valence': 0.5, 'arousal': 0.4, 'mood_word': 'x',
                        'passion_delta': 0.0, 'intimacy_delta': 0.0,
                        'source': 't'},
                scored_at=T0,
                db_path=self.db_path,
                environ=ON,
            )
        except Exception:  # noqa: BLE001
            main_ok = False
            raise
        self.assertTrue(main_ok)
        self.assertFalse(r.ok)
        self.assertEqual(r.status, 'failed')
        health = shadow.get_shadow_health(db_path=self.db_path, environ=ON)
        self.assertIsNotNone(health.last_error)

    def test_wrappers_exist_but_no_gateway_imports(self):
        self.assertTrue(callable(shadow.observe_user_message_shadow))
        self.assertTrue(callable(shadow.observe_scored_shadow))
        self.assertTrue(callable(shadow.apply_outcome_shadow))


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
        """gateway / wake / app 不得引用 shadow 事件包装。"""
        for name in ('gateway.py', 'app.py'):
            path = Path(ROOT, name)
            if not path.exists():
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            self.assertNotIn('observe_user_message_shadow', text)
            self.assertNotIn('observe_scored_shadow', text)
            self.assertNotIn('apply_outcome_shadow', text)
            self.assertNotIn('internal_state_shadow', text)
        wake_dir = Path(ROOT, 'wake')
        if wake_dir.is_dir():
            for path in wake_dir.rglob('*.py'):
                text = path.read_text(encoding='utf-8', errors='replace')
                self.assertNotIn('internal_state_shadow', text)
                self.assertNotIn('apply_outcome_shadow', text)


if __name__ == '__main__':
    unittest.main()
