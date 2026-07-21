"""Phase 1A-0 — internal_state_store 存储底座测试（仅临时 SQLite）。"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import internal_state_store as store


def _snapshot(**overrides):
    base = SimpleNamespace(
        observed_at='2026-07-21 12:00:00',
        affect=SimpleNamespace(
            pa=0.55, na=0.25, valence=0.6, arousal=0.4, mood_word='平静'),
        bond=SimpleNamespace(intimacy=0.5, passion=0.294, commitment=0.7),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.35, curiosity=0.2, reflection=0.3, social=0.1,
            duty=0.15, libido=0.05, stress=0.2, fatigue=0.25),
        diagnostics=SimpleNamespace(source_timestamps={
            'legacy.emotion_state.p_updated_at': '2026-07-21 06:00:00',
            'legacy.emotion_state.i_updated_at': '2026-07-21 06:00:00',
            'legacy.drive_state.last_updated': '2026-07-21 10:00:00',
        }),
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'store.db')
        self.conn = store.open_store(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def bootstrap(self, snap=None):
        return store.bootstrap_from_snapshot(self.conn, snap or _snapshot())


class SchemaTests(StoreBase):
    def test_ensure_schema_idempotent(self):
        store.ensure_schema(self.conn)
        store.ensure_schema(self.conn)
        tables = {
            r[0] for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn('internal_state_v3', tables)
        self.assertIn('internal_state_events', tables)

    def test_does_not_force_wal(self):
        before = store.get_journal_mode(self.conn)
        store.ensure_schema(self.conn)
        after = store.get_journal_mode(self.conn)
        self.assertEqual(before, after)
        self.assertNotEqual(after, 'wal')

    def test_busy_timeout_set(self):
        self.assertEqual(
            store.get_busy_timeout_ms(self.conn),
            store.DEFAULT_BUSY_TIMEOUT_MS)

    def test_default_values_and_check_ranges(self):
        self.bootstrap()
        row = store.read_state(self.conn)
        self.assertEqual(row['id'], 1)
        self.assertEqual(row['state_version'], 0)
        for key in ('pa', 'na', 'attachment', 'fatigue'):
            self.assertGreaterEqual(row[key], 0.0)
            self.assertLessEqual(row[key], 1.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('BEGIN IMMEDIATE')
            self.conn.execute(
                'UPDATE internal_state_v3 SET pa=1.5 WHERE id=1')
            self.conn.execute('COMMIT')
        try:
            self.conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass


class BootstrapTests(StoreBase):
    def test_bootstrap_same_snapshot_is_duplicate(self):
        snap = _snapshot()
        r1 = store.bootstrap_from_snapshot(self.conn, snap)
        self.assertEqual(r1.status, 'applied')
        state1 = store.read_state(self.conn)
        r2 = store.bootstrap_from_snapshot(self.conn, snap)
        self.assertEqual(r2.status, 'duplicate')
        state2 = store.read_state(self.conn)
        self.assertAlmostEqual(state1['pa'], state2['pa'])
        self.assertEqual(state1['state_version'], state2['state_version'])

    def test_bootstrap_same_key_different_snapshot_is_idempotency_conflict(self):
        r1 = self.bootstrap()
        self.assertEqual(r1.status, 'applied')
        pa_before = store.read_state(self.conn)['pa']
        r2 = store.bootstrap_from_snapshot(self.conn, _snapshot(
            affect=SimpleNamespace(
                pa=0.99, na=0.01, valence=0.9, arousal=0.9, mood_word='变了')))
        self.assertEqual(r2.status, 'idempotency_conflict')
        state = store.read_state(self.conn)
        self.assertAlmostEqual(state['pa'], pa_before)
        self.assertEqual(state['state_version'], 0)

    def test_bootstrap_resets_materialized_timestamps_to_observed_at(self):
        snap = _snapshot()
        self.bootstrap(snap)
        state = store.read_state(self.conn)
        self.assertEqual(state['p_updated_at'], snap.observed_at)
        self.assertEqual(state['i_updated_at'], snap.observed_at)
        self.assertEqual(state['drives_updated_at'], snap.observed_at)
        self.assertEqual(state['updated_at'], snap.observed_at)
        event = store.read_event(self.conn, 'bootstrap:initial')
        payload = json.loads(event['payload_json'])
        self.assertEqual(
            payload['legacy_source_timestamps'][
                'legacy.emotion_state.p_updated_at'],
            '2026-07-21 06:00:00')
        self.assertIn('seed', payload)

    def test_post_bootstrap_decay_only_counts_time_after_observed_at(self):
        """物化值不应再按旧时间戳二次衰减。"""
        passion_at_obs = 0.294
        self.bootstrap(_snapshot(
            bond=SimpleNamespace(
                intimacy=0.5, passion=passion_at_obs, commitment=0.7)))
        state = store.read_state(self.conn)
        # 若错误保留旧 p_updated_at=06:00，相对 observed 12:00 会再衰 6h
        wrong_double = passion_at_obs * math.exp(-6.0 / 6.0)
        # 正确：以 observed_at 为基准，再过 0 小时仍为原值
        self.assertAlmostEqual(state['passion'], passion_at_obs, places=4)
        self.assertNotAlmostEqual(state['passion'], wrong_double, places=3)
        self.assertEqual(state['p_updated_at'], '2026-07-21 12:00:00')

    def test_bootstrap_does_not_import_legacy_engines(self):
        import ast
        src = Path(ROOT, 'internal_state_store.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        for banned in ('emotion_engine', 'drive_engine', 'desire', 'gateway'):
            self.assertNotIn(banned, imported)


class EventIdempotencyTests(StoreBase):
    def test_event_key_unique_and_duplicate_skips_version(self):
        self.bootstrap()
        payload = {'message_id': 1}
        r1 = store.apply_state_update(
            self.conn,
            event_key='user_rule:1',
            event_type='user_rule',
            source_id='1',
            payload=payload,
            mutator=lambda s: {'pa': min(1.0, s['pa'] + 0.05)},
            expected_state_version=0,
        )
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r1.state_version_after, 1)

        r2 = store.apply_state_update(
            self.conn,
            event_key='user_rule:1',
            event_type='user_rule',
            source_id='1',
            payload=payload,  # 同语义
            mutator=lambda s: {'pa': 0.99},
            expected_state_version=1,
        )
        self.assertEqual(r2.status, 'duplicate')
        self.assertEqual(store.read_state(self.conn)['state_version'], 1)
        n = self.conn.execute(
            'SELECT COUNT(*) FROM internal_state_events WHERE event_key=?',
            ('user_rule:1',),
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_same_key_different_payload_is_idempotency_conflict(self):
        self.bootstrap()
        store.apply_state_update(
            self.conn,
            event_key='user_rule:123',
            event_type='user_rule',
            source_id='123',
            payload={'message_id': 123, 'delta': 'A'},
            mutator=lambda s: {'pa': 0.6},
            expected_state_version=0,
        )
        pa_after = store.read_state(self.conn)['pa']
        r = store.apply_state_update(
            self.conn,
            event_key='user_rule:123',
            event_type='user_rule',
            source_id='123',
            payload={'message_id': 123, 'delta': 'B'},
            mutator=lambda s: {'pa': 0.99},
            expected_state_version=1,
        )
        self.assertEqual(r.status, 'idempotency_conflict')
        self.assertAlmostEqual(store.read_state(self.conn)['pa'], pa_after)
        self.assertEqual(store.read_state(self.conn)['state_version'], 1)


class AtomicityTests(StoreBase):
    def test_exception_rolls_back_state_and_event(self):
        self.bootstrap()
        before_events = self.conn.execute(
            'SELECT COUNT(*) FROM internal_state_events').fetchone()[0]
        before_ver = store.read_state(self.conn)['state_version']

        def boom(_state):
            raise RuntimeError('mid-flight failure')

        with self.assertRaises(RuntimeError):
            store.apply_state_update(
                self.conn,
                event_key='user_rule:boom',
                event_type='user_rule',
                source_id='9',
                payload={},
                mutator=boom,
                expected_state_version=0,
            )
        self.assertEqual(
            store.read_state(self.conn)['state_version'], before_ver)
        after_events = self.conn.execute(
            'SELECT COUNT(*) FROM internal_state_events').fetchone()[0]
        self.assertEqual(after_events, before_events)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:boom'))

    def test_state_and_event_commit_together(self):
        self.bootstrap()
        r = store.apply_state_update(
            self.conn,
            event_key='user_rule:2',
            event_type='user_rule',
            source_id='2',
            payload={'x': 1},
            mutator=lambda s: {'na': 0.33},
            expected_state_version=0,
        )
        self.assertTrue(r.applied)
        state = store.read_state(self.conn)
        event = store.read_event(self.conn, 'user_rule:2')
        self.assertAlmostEqual(state['na'], 0.33)
        self.assertEqual(event['status'], 'applied')
        self.assertEqual(event['state_version_after'], 1)


class VersionTests(StoreBase):
    def test_expected_version_conflict_no_overwrite(self):
        self.bootstrap()
        store.apply_state_update(
            self.conn,
            event_key='user_rule:10',
            event_type='user_rule',
            source_id='10',
            payload={},
            mutator=lambda s: {'curiosity': 0.4},
            expected_state_version=0,
        )
        pa_before = store.read_state(self.conn)['pa']
        r = store.apply_state_update(
            self.conn,
            event_key='user_rule:11',
            event_type='user_rule',
            source_id='11',
            payload={},
            mutator=lambda s: {'pa': 0.11},
            expected_state_version=0,
        )
        self.assertEqual(r.status, 'version_conflict')
        self.assertIsNone(r.event_id)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:11'))
        state = store.read_state(self.conn)
        self.assertEqual(state['state_version'], 1)
        self.assertAlmostEqual(state['pa'], pa_before)

    def test_version_conflict_same_event_key_can_retry(self):
        self.bootstrap()
        store.apply_state_update(
            self.conn,
            event_key='user_rule:other',
            event_type='user_rule',
            source_id='other',
            payload={},
            mutator=lambda s: {'curiosity': 0.45},
            expected_state_version=0,
        )
        # 用过期版本尝试 A → 冲突且不消费 key
        r1 = store.apply_state_update(
            self.conn,
            event_key='user_rule:A',
            event_type='user_rule',
            source_id='A',
            payload={'message_id': 'A'},
            mutator=lambda s: {'pa': 0.77},
            expected_state_version=0,
        )
        self.assertEqual(r1.status, 'version_conflict')
        self.assertIsNone(store.read_event(self.conn, 'user_rule:A'))
        # 重读最新版本后同 key 重试
        cur_ver = store.read_state(self.conn)['state_version']
        r2 = store.apply_state_update(
            self.conn,
            event_key='user_rule:A',
            event_type='user_rule',
            source_id='A',
            payload={'message_id': 'A'},
            mutator=lambda s: {'pa': 0.77},
            expected_state_version=cur_ver,
        )
        self.assertEqual(r2.status, 'applied')
        self.assertEqual(r2.state_version_after, cur_ver + 1)
        self.assertAlmostEqual(store.read_state(self.conn)['pa'], 0.77)

    def test_missing_state_event_can_retry_after_bootstrap(self):
        store.ensure_schema(self.conn)
        r1 = store.apply_state_update(
            self.conn,
            event_key='user_rule:early',
            event_type='user_rule',
            source_id='early',
            payload={'message_id': 'early'},
            mutator=lambda s: {'pa': 0.66},
            expected_state_version=0,
        )
        self.assertEqual(r1.status, 'failed')
        self.assertIn('missing', r1.error)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:early'))
        self.bootstrap()
        r2 = store.apply_state_update(
            self.conn,
            event_key='user_rule:early',
            event_type='user_rule',
            source_id='early',
            payload={'message_id': 'early'},
            mutator=lambda s: {'pa': 0.66},
            expected_state_version=0,
        )
        self.assertEqual(r2.status, 'applied')
        self.assertAlmostEqual(store.read_state(self.conn)['pa'], 0.66)

    def test_mark_stale_consumes_key_as_terminal(self):
        self.bootstrap()
        store.apply_state_update(
            self.conn,
            event_key='user_scored:1',
            event_type='user_scored',
            source_id='1',
            payload={'v': 1},
            mutator=lambda s: {'last_scored_message_id': 1},
            expected_state_version=0,
        )
        r = store.apply_state_update(
            self.conn,
            event_key='user_scored:0',
            event_type='user_scored',
            source_id='0',
            payload={'v': 0},
            mutator=lambda s: {'last_scored_message_id': 0},
            expected_state_version=0,
            mark_stale=True,
        )
        self.assertEqual(r.status, 'stale_skipped')
        event = store.read_event(self.conn, 'user_scored:0')
        self.assertEqual(event['status'], 'stale_skipped')
        # 终态后再同 key → duplicate
        r2 = store.apply_state_update(
            self.conn,
            event_key='user_scored:0',
            event_type='user_scored',
            source_id='0',
            payload={'v': 0},
            mutator=lambda s: {'last_scored_message_id': 0},
            expected_state_version=1,
        )
        self.assertEqual(r2.status, 'duplicate')

    def test_successful_update_increments_version_by_one(self):
        self.bootstrap()
        for i in range(3):
            r = store.apply_state_update(
                self.conn,
                event_key=f'user_rule:{100+i}',
                event_type='user_rule',
                source_id=str(100 + i),
                payload={},
                mutator=lambda s: {'duty': s['duty']},
                expected_state_version=i,
            )
            self.assertEqual(r.status, 'applied')
            self.assertEqual(r.state_version_after, i + 1)
        self.assertEqual(store.read_state(self.conn)['state_version'], 3)


class MutatorValidationTests(StoreBase):
    def test_unknown_mutator_field_rolls_back(self):
        self.bootstrap()
        with self.assertRaises(store.StoreError) as ctx:
            store.apply_state_update(
                self.conn,
                event_key='user_rule:typo',
                event_type='user_rule',
                source_id='typo',
                payload={},
                mutator=lambda s: {'curiosty': 0.8},
                expected_state_version=0,
            )
        self.assertIn('unknown', str(ctx.exception))
        self.assertEqual(store.read_state(self.conn)['state_version'], 0)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:typo'))

    def test_empty_mutator_rolls_back(self):
        self.bootstrap()
        with self.assertRaises(store.StoreError):
            store.apply_state_update(
                self.conn,
                event_key='user_rule:empty',
                event_type='user_rule',
                source_id='empty',
                payload={},
                mutator=lambda s: {},
                expected_state_version=0,
            )
        self.assertEqual(store.read_state(self.conn)['state_version'], 0)
        self.assertIsNone(store.read_event(self.conn, 'user_rule:empty'))

    def test_system_managed_field_rejected(self):
        self.bootstrap()
        with self.assertRaises(store.StoreError) as ctx:
            store.apply_state_update(
                self.conn,
                event_key='user_rule:sys',
                event_type='user_rule',
                source_id='sys',
                payload={},
                mutator=lambda s: {'state_version': 99, 'pa': 0.5},
                expected_state_version=0,
            )
        self.assertIn('system-managed', str(ctx.exception))
        self.assertEqual(store.read_state(self.conn)['state_version'], 0)


class ConnectionContractTests(unittest.TestCase):
    def test_plain_sqlite_connection_is_supported(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'plain.db')
        conn = sqlite3.connect(db_path)  # 默认 row_factory=None
        try:
            r = store.bootstrap_from_snapshot(conn, _snapshot())
            self.assertEqual(r.status, 'applied')
            self.assertIsNotNone(r.event_id)
            state = store.read_state(conn)
            self.assertEqual(state['p_updated_at'], '2026-07-21 12:00:00')
            r2 = store.apply_state_update(
                conn,
                event_key='user_rule:plain',
                event_type='user_rule',
                source_id='plain',
                payload={'ok': True},
                mutator=lambda s: {'na': 0.31},
                expected_state_version=0,
            )
            self.assertEqual(r2.status, 'applied')
            self.assertAlmostEqual(store.read_state(conn)['na'], 0.31)
        finally:
            conn.close()

    def test_plain_connection_isolation_level_is_preserved(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'iso.db')
        conn = sqlite3.connect(db_path)
        try:
            before = conn.isolation_level
            store.bootstrap_from_snapshot(conn, _snapshot())
            self.assertEqual(conn.isolation_level, before)
            store.apply_state_update(
                conn,
                event_key='user_rule:iso',
                event_type='user_rule',
                source_id='iso',
                payload={},
                mutator=lambda s: {'duty': 0.2},
                expected_state_version=0,
            )
            self.assertEqual(conn.isolation_level, before)
        finally:
            conn.close()

    def test_write_api_does_not_commit_caller_transaction(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'caller_tx.db')
        setup = sqlite3.connect(db_path)
        setup.execute('CREATE TABLE unrelated (x TEXT)')
        setup.commit()
        setup.close()

        conn = sqlite3.connect(db_path)
        try:
            conn.execute("INSERT INTO unrelated VALUES ('尚未决定提交')")
            self.assertTrue(conn.in_transaction)
            with self.assertRaises(store.StoreError) as ctx:
                store.bootstrap_from_snapshot(conn, _snapshot())
            self.assertIn('no active transaction', str(ctx.exception))
            conn.rollback()
        finally:
            conn.close()

        verify = sqlite3.connect(db_path)
        try:
            n = verify.execute('SELECT COUNT(*) FROM unrelated').fetchone()[0]
            self.assertEqual(n, 0)
            # store 也未偷偷建权威表（因入口直接拒绝）
            tables = {
                r[0] for r in verify.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertNotIn('internal_state_v3', tables)
        finally:
            verify.close()


class ConcurrencyTests(StoreBase):
    def test_two_connections_no_silent_lost_update(self):
        self.bootstrap()
        self.conn.close()

        results = []
        barrier = threading.Barrier(2)

        def worker(event_key: str, field: str, value: float):
            c = store.open_store(self.db_path)
            try:
                barrier.wait(timeout=5)

                def mutator(state, _field=field, _value=value):
                    time.sleep(0.05)
                    return {_field: _value}

                r = store.apply_state_update(
                    c,
                    event_key=event_key,
                    event_type='user_rule',
                    source_id=event_key,
                    payload={'field': field},
                    mutator=mutator,
                    expected_state_version=0,
                )
                results.append(r)
            finally:
                c.close()

        t1 = threading.Thread(target=worker, args=('user_rule:a', 'pa', 0.81))
        t2 = threading.Thread(target=worker, args=('user_rule:b', 'na', 0.41))
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        self.assertEqual(len(results), 2)
        statuses = [r.status for r in results]
        self.assertEqual(statuses.count('applied'), 1)
        self.assertEqual(statuses.count('version_conflict'), 1)

        c = store.open_store(self.db_path)
        try:
            state = store.read_state(c)
            self.assertEqual(state['state_version'], 1)
            # 失败者未消费 key：可同 key 用新版本重试
            loser = next(r for r in results if r.status == 'version_conflict')
            # 找出未 applied 的 key
            applied_keys = {
                'user_rule:a' if store.read_event(c, 'user_rule:a') else None,
                'user_rule:b' if store.read_event(c, 'user_rule:b') else None,
            }
            missing = [k for k in ('user_rule:a', 'user_rule:b')
                       if store.read_event(c, k) is None]
            self.assertEqual(len(missing), 1, applied_keys)
            retry = store.apply_state_update(
                c,
                event_key=missing[0],
                event_type='user_rule',
                source_id=missing[0],
                payload={'field': 'retry'},
                mutator=lambda s: {'social': 0.22},
                expected_state_version=1,
            )
            self.assertEqual(retry.status, 'applied')
            self.assertEqual(store.read_state(c)['state_version'], 2)
        finally:
            c.close()


class NoProductionHookTests(unittest.TestCase):
    def test_module_has_no_startup_side_effects_or_hooks(self):
        import ast
        src = Path(ROOT, 'internal_state_store.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail(f'module-level call forbidden: line {node.lineno}')
        if (isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)):
            start = tree.body[1].lineno - 1 if len(tree.body) > 1 else 0
            code_only = '\n'.join(src.splitlines()[start:])
        else:
            code_only = src
        for needle in (
            'observe_user_message',
            'observe_scored',
            'apply_outcome(',
            'PRAGMA journal_mode=',
            'RELATIONSHIP_CONTEXT',
        ):
            self.assertNotIn(needle, code_only)


if __name__ == '__main__':
    unittest.main()
