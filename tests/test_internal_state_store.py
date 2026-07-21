"""Phase 1A-0 — internal_state_store 存储底座测试（仅临时 SQLite）。"""

from __future__ import annotations

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
        bond=SimpleNamespace(intimacy=0.5, passion=0.2, commitment=0.7),
        candidate_unified_drives=SimpleNamespace(
            attachment=0.35, curiosity=0.2, reflection=0.3, social=0.1,
            duty=0.15, libido=0.05, stress=0.2, fatigue=0.25),
        diagnostics=SimpleNamespace(source_timestamps={
            'legacy.emotion_state.p_updated_at': '2026-07-21 11:00:00',
            'legacy.emotion_state.i_updated_at': '2026-07-21 11:00:00',
            'legacy.drive_state.last_updated': '2026-07-21 11:30:00',
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

    def bootstrap(self):
        return store.bootstrap_from_snapshot(self.conn, _snapshot())


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
        """迁移代码不得偷偷改 journal_mode。"""
        before = store.get_journal_mode(self.conn)
        store.ensure_schema(self.conn)
        after = store.get_journal_mode(self.conn)
        self.assertEqual(before, after)
        # 临时库默认 delete；只要我们没写成 wal 即可
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
        # CHECK：越界写入失败
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
    def test_bootstrap_idempotent(self):
        r1 = self.bootstrap()
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r1.state_version_after, 0)
        state1 = store.read_state(self.conn)
        r2 = store.bootstrap_from_snapshot(self.conn, _snapshot(
            affect=SimpleNamespace(
                pa=0.99, na=0.01, valence=0.9, arousal=0.9, mood_word='变了')))
        self.assertEqual(r2.status, 'duplicate')
        state2 = store.read_state(self.conn)
        self.assertEqual(state1['pa'], state2['pa'])
        self.assertEqual(state1['state_version'], state2['state_version'])
        self.assertAlmostEqual(state1['pa'], 0.55)

    def test_bootstrap_does_not_import_legacy_engines(self):
        import ast
        src = Path(ROOT, 'internal_state_store.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split('.')[0])
        for banned in ('emotion_engine', 'drive_engine', 'desire', 'gateway'):
            self.assertNotIn(banned, imported)


class EventIdempotencyTests(StoreBase):
    def test_event_key_unique_and_duplicate_skips_version(self):
        self.bootstrap()
        r1 = store.apply_state_update(
            self.conn,
            event_key='user_rule:1',
            event_type='user_rule',
            source_id='1',
            payload={'message_id': 1},
            mutator=lambda s: {'pa': min(1.0, s['pa'] + 0.05)},
            expected_state_version=0,
        )
        self.assertEqual(r1.status, 'applied')
        self.assertEqual(r1.state_version_before, 0)
        self.assertEqual(r1.state_version_after, 1)
        self.assertEqual(store.read_state(self.conn)['state_version'], 1)

        r2 = store.apply_state_update(
            self.conn,
            event_key='user_rule:1',
            event_type='user_rule',
            source_id='1',
            payload={'message_id': 1, 'retry': True},
            mutator=lambda s: {'pa': 0.99},
            expected_state_version=1,
        )
        self.assertEqual(r2.status, 'duplicate')
        state = store.read_state(self.conn)
        self.assertEqual(state['state_version'], 1)
        self.assertNotAlmostEqual(state['pa'], 0.99)
        # 仍只有一条事件
        n = self.conn.execute(
            'SELECT COUNT(*) FROM internal_state_events WHERE event_key=?',
            ('user_rule:1',),
        ).fetchone()[0]
        self.assertEqual(n, 1)


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
        self.assertEqual(state['state_version'], 1)
        self.assertEqual(event['status'], 'applied')
        self.assertEqual(event['state_version_before'], 0)
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
            expected_state_version=0,  # stale
        )
        self.assertEqual(r.status, 'version_conflict')
        state = store.read_state(self.conn)
        self.assertEqual(state['state_version'], 1)
        self.assertAlmostEqual(state['pa'], pa_before)
        event = store.read_event(self.conn, 'user_rule:11')
        self.assertEqual(event['status'], 'failed')
        self.assertIn('version conflict', event['error'])

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
                    # 拉长临界区，放大竞争窗口
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
        statuses = sorted(r.status for r in results)
        # 一个 applied，另一个 version_conflict（或因 IMMEDIATE 串行后仍版本冲突）
        self.assertIn('applied', statuses)
        self.assertTrue(
            statuses.count('applied') == 1,
            f'expected exactly one applied, got {statuses}',
        )
        self.assertTrue(
            any(s in ('version_conflict',) for s in statuses)
            or statuses.count('applied') == 1 and 'version_conflict' in statuses,
            f'unexpected statuses: {statuses}',
        )

        c = store.open_store(self.db_path)
        try:
            state = store.read_state(c)
            self.assertEqual(state['state_version'], 1)
            # 只有赢家的字段被写入，不会两个都悄悄写上却版本仍像只加一次
            winners = [r for r in results if r.status == 'applied']
            self.assertEqual(len(winners), 1)
            # 版本只 +1
            self.assertEqual(winners[0].state_version_after, 1)
        finally:
            c.close()


class NoProductionHookTests(unittest.TestCase):
    def test_module_has_no_startup_side_effects_or_hooks(self):
        import ast
        src = Path(ROOT, 'internal_state_store.py').read_text(encoding='utf-8')
        # 不得在模块级调用 ensure_schema / bootstrap（无启动副作用）
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.fail(f'module-level call forbidden: line {node.lineno}')
        # 业务钩子与改 WAL 不得出现在可执行语句（允许 docstring 禁令清单）
        body_src = ast.get_source_segment(src, tree) or src
        # 剥掉模块 docstring 后再扫
        if (isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant)):
            start = tree.body[1].lineno - 1 if len(tree.body) > 1 else len(src)
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
