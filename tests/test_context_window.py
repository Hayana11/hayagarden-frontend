"""Manual context window R0 — schema, resolver, switch, idempotency."""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import context_window as cw
from chat import daily_context as dc


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.commit()
    conn.close()


def _insert(db_path: str, author: str, content: str, created_at: str, **kwargs) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, tool_calls, source_kind, image_url, created_at) '
        'VALUES (?,?,?,?,?,?)',
        (
            author, content,
            kwargs.get('tool_calls', ''),
            kwargs.get('source_kind', 'chat'),
            kwargs.get('image_url', ''),
            created_at,
        ),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


def _map(db_path: str, ctx_id: int, epoch: int, message_id: int, role: str):
    dc.record_daily_message_context(
        message_id,
        context_id=ctx_id,
        context_epoch=epoch,
        resident_generation=1,
        role=role,
        db_path=db_path,
    )


def _legacy_ctx(db_path: str, day: str = '2026-07-27', **kwargs):
    return dc.get_or_create_daily_context(
        local_day=day,
        db_path=db_path,
        now=kwargs.pop('now', datetime.datetime(2026, 7, 27, 10, 0, 0)),
        **kwargs,
    )


def _create_old_schema_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        '''
        CREATE TABLE daily_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            local_day TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            boundary_hour INTEGER NOT NULL DEFAULT 4,
            context_epoch INTEGER NOT NULL,
            boundary_message_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            handoff_id INTEGER NULL,
            carryover_count INTEGER NOT NULL DEFAULT 0,
            selection_finalized_at DATETIME NULL,
            resident_generation INTEGER NOT NULL DEFAULT 1,
            morning_greeting_message_id INTEGER NULL,
            version INTEGER NOT NULL DEFAULT 1,
            lease_owner TEXT NULL,
            lease_expires_at DATETIME NULL,
            created_at DATETIME NOT NULL DEFAULT '2026-07-27 10:00:00',
            updated_at DATETIME NOT NULL DEFAULT '2026-07-27 10:00:00',
            is_backfill INTEGER NOT NULL DEFAULT 0,
            UNIQUE(chat_id, local_day)
        );
        INSERT INTO daily_contexts (
            chat_id, local_day, timezone, boundary_hour, context_epoch,
            boundary_message_id, status, carryover_count, resident_generation,
            version, created_at, updated_at, is_backfill
        ) VALUES (
            'default', '2026-07-26', 'Asia/Shanghai', 4, 1,
            0, 'PROVISIONAL', 0, 1, 1, '2026-07-26 10:00:00', '2026-07-26 10:00:00', 0
        );
        CREATE TABLE daily_carryover_messages (
            context_id INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY(context_id, ordinal)
        );
        INSERT INTO daily_carryover_messages VALUES (1, 0, 99);
        CREATE TABLE daily_resident_cursors (
            context_id INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            history_cursor_message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (context_id, resident_generation)
        );
        INSERT INTO daily_resident_cursors VALUES (1, 1, 50, '2026-07-26 10:00:00');
        CREATE TABLE daily_resident_turn_leases (
            context_id INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            lease_owner TEXT NOT NULL,
            request_message_id INTEGER NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (context_id, resident_generation)
        );
        INSERT INTO daily_resident_turn_leases VALUES (
            1, 1, 'owner', 1, '2026-07-26 10:00:00', '2026-07-26 11:00:00', '2026-07-26 10:00:00'
        );
        CREATE TABLE daily_message_contexts (
            message_id INTEGER PRIMARY KEY,
            context_id INTEGER NOT NULL,
            context_epoch INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO daily_message_contexts VALUES (99, 1, 1, 1, 'user', '2026-07-26 10:00:00');
        CREATE TABLE daily_resident_owners (
            context_id INTEGER NOT NULL,
            resident_generation INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            resident_key TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (context_id, resident_generation)
        );
        INSERT INTO daily_resident_owners VALUES (1, 1, 'w1', 'rk', '2026-07-26 10:00:00');
        '''
    )
    conn.commit()
    conn.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_old_schema_migrates_preserving_ids(self):
        db = _tmp_db()
        _create_old_schema_db(db)
        dc.ensure_schema(db)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(daily_contexts)')}
        self.assertIn('window_mode', cols)
        row = conn.execute('SELECT id, window_mode, opened_at FROM daily_contexts').fetchone()
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 'legacy_daily')
        self.assertTrue(row[2])
        for table in (
            'daily_carryover_messages',
            'daily_resident_cursors',
            'daily_resident_turn_leases',
            'daily_message_contexts',
            'daily_resident_owners',
        ):
            ids = {
                int(r[0])
                for r in conn.execute('SELECT DISTINCT context_id FROM %s' % table)
            }
            self.assertEqual(ids, {1}, msg=table)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_contexts'"
        ).fetchone()[0]
        self.assertIn('AUTOINCREMENT', sql.upper())
        seq = dc._read_sqlite_sequence(conn, 'daily_contexts')
        self.assertEqual(seq, 1)
        indexes = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='daily_contexts'"
            ).fetchall()
        }
        self.assertIn('idx_daily_contexts_legacy_day_unique', indexes)
        self.assertIn('idx_daily_contexts_open_manual_unique', indexes)
        self.assertIn('idx_daily_contexts_switch_idem_unique', indexes)
        conn.close()

    def test_migration_idempotent(self):
        db = _tmp_db()
        _create_old_schema_db(db)
        dc.ensure_schema(db)
        dc.ensure_schema(db)
        conn = sqlite3.connect(db)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0], 1)
        conn.close()

    def test_manual_same_day_multiple_windows_allowed(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db)
        u1 = _insert(db, 'hayana', 'hi', '2026-07-27 10:00:00')
        a1 = _insert(db, 'fyodor', 'hello', '2026-07-27 10:01:00')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u1, 'user')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), a1, 'assistant')
        req1 = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=req1,
            db_path=db,
            now=datetime.datetime(2026, 7, 27, 12, 0, 0),
        )
        current = cw.get_current_context_window(db_path=db, now=datetime.datetime(2026, 7, 27, 13, 0, 0))
        u2 = _insert(db, 'hayana', 'again', '2026-07-27 14:00:00')
        _map(db, int(current['id']), int(current['context_epoch']), u2, 'user')
        req2 = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=int(current['id']),
            source_context_epoch=int(current['context_epoch']),
            count=0,
            request_id=req2,
            db_path=db,
            now=datetime.datetime(2026, 7, 27, 15, 0, 0),
        )
        conn = sqlite3.connect(db)
        manual_same_day = conn.execute(
            "SELECT COUNT(*) FROM daily_contexts WHERE local_day='2026-07-27' AND window_mode='manual'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(manual_same_day, 2)


class CurrentResolverTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)

    def test_bootstrap_once_on_empty_db(self):
        a = cw.get_current_context_window(db_path=self.db)
        b = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(a['id']), int(b['id']))
        self.assertEqual(int(a['context_epoch']), int(b['context_epoch']))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0], 1)
        conn.close()

    def test_no_new_context_across_0400(self):
        ctx = _legacy_ctx(
            self.db,
            day='2026-07-26',
            now=datetime.datetime(2026, 7, 27, 3, 59, 0),
            allow_backfill=True,
        )
        t1 = cw.get_current_context_window(
            db_path=self.db, now=datetime.datetime(2026, 7, 27, 3, 59, 0),
        )
        t2 = cw.get_current_context_window(
            db_path=self.db, now=datetime.datetime(2026, 7, 27, 4, 1, 0),
        )
        t3 = cw.get_current_context_window(
            db_path=self.db, now=datetime.datetime(2026, 7, 27, 12, 0, 0),
        )
        self.assertEqual(int(t1['id']), int(ctx['id']))
        self.assertEqual(int(t2['id']), int(ctx['id']))
        self.assertEqual(int(t3['id']), int(ctx['id']))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0], 1)
        conn.close()

    def test_manual_open_preferred_over_legacy(self):
        ctx = _legacy_ctx(self.db)
        u = _insert(self.db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(self.db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        switched = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        )
        current = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(current['id']), switched['target_context_id'])


class CarryoverCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.ctx = _legacy_ctx(self.db)
        self.cid = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])

    def test_rounds_from_source_context_only(self):
        u1 = _insert(self.db, 'hayana', 'a', '2026-07-27 10:00:00')
        a1 = _insert(self.db, 'fyodor', 'b', '2026-07-27 10:01:00')
        u2 = _insert(self.db, 'hayana', 'c', '2026-07-27 10:02:00')
        _map(self.db, self.cid, self.epoch, u1, 'user')
        _map(self.db, self.cid, self.epoch, a1, 'assistant')
        _map(self.db, self.cid, self.epoch, u2, 'user')
        data = cw.list_context_window_carryover_rounds(
            self.cid, self.epoch, db_path=self.db,
        )
        self.assertEqual(data['available_round_count'], 2)
        self.assertEqual(data['rounds'][-1]['message_ids'], [u2])

    def test_stale_source_409(self):
        with self.assertRaises(cw.StaleSourceContextError):
            cw.list_context_window_carryover_rounds(999, 1, db_path=self.db)

    def test_lease_busy_423(self):
        dc.claim_daily_resident_turn(
            chat_id='default',
            context_id=self.cid,
            expected_context_epoch=self.epoch,
            worker_id='w1',
            request_message_id=1,
            lease_owner='owner',
            resident_key='rk',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        with self.assertRaises(cw.WindowBusyError):
            cw.list_context_window_carryover_rounds(
                self.cid, self.epoch, db_path=self.db,
                now=datetime.datetime(2026, 7, 27, 10, 0, 0),
            )


class SwitchTransactionTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.ctx = _legacy_ctx(self.db)
        self.cid = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        for i in range(6):
            u = _insert(self.db, 'hayana', 'u%d' % i, '2026-07-27 10:%02d:00' % i)
            a = _insert(self.db, 'fyodor', 'a%d' % i, '2026-07-27 10:%02d:30' % i)
            _map(self.db, self.cid, self.epoch, u, 'user')
            _map(self.db, self.cid, self.epoch, a, 'assistant')

    def test_switch_count_three(self):
        req = str(uuid.uuid4())
        out = cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=3,
            request_id=req,
            db_path=self.db,
        )
        self.assertEqual(out['selected_round_count'], 3)
        self.assertEqual(out['requested_round_count'], 3)
        source = dc.get_daily_context_by_id(self.cid, db_path=self.db)
        assert source is not None
        self.assertIsNotNone(source.get('closed_at'))
        self.assertEqual(source.get('close_reason'), cw.CLOSE_REASON_MANUAL)
        target = dc.get_daily_context_by_id(out['target_context_id'], db_path=self.db)
        assert target is not None
        self.assertEqual(target.get('window_mode'), cw.WINDOW_MODE_MANUAL)
        self.assertEqual(int(target['context_epoch']), self.epoch + 1)

    def test_idempotent_replay(self):
        req = str(uuid.uuid4())
        first = cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=0,
            request_id=req,
            db_path=self.db,
        )
        second = cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=0,
            request_id=req,
            db_path=self.db,
        )
        self.assertEqual(first['target_context_id'], second['target_context_id'])
        conn = sqlite3.connect(self.db)
        manual_count = conn.execute(
            "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(manual_count, 1)

    def test_idempotency_mismatch(self):
        req = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=0,
            request_id=req,
            db_path=self.db,
        )
        with self.assertRaises(cw.IdempotencyMismatchError):
            cw.switch_context_window(
                source_context_id=self.cid,
                source_context_epoch=self.epoch,
                count=3,
                request_id=req,
                db_path=self.db,
            )


class CompatibilityTests(unittest.TestCase):
    def test_legacy_resolver_unchanged_flag_off(self):
        db = _tmp_db()
        _init_chat_messages(db)
        with mock.patch.dict(os.environ, {'DAILY_SOFT_WINDOW_ENABLED': '0'}, clear=False):
            import config_store
            config_store._cache_clear() if hasattr(config_store, '_cache_clear') else None
            self.assertFalse(dc.enabled())
        before = dc.resolve_current_daily_context_for_api(
            db_path=db,
            now=datetime.datetime(2026, 7, 27, 3, 59, 0),
        )
        after = dc.resolve_current_daily_context_for_api(
            db_path=db,
            now=datetime.datetime(2026, 7, 27, 4, 1, 0),
        )
        self.assertNotEqual(int(before['id']), 0)
        self.assertNotEqual(int(after['id']), 0)

    def test_send_path_does_not_call_switch(self):
        import inspect
        import chat.daily_runtime as dr
        source = inspect.getsource(dr)
        self.assertNotIn('switch_context_window', source)


class MigrationFaultInjectionTests(unittest.TestCase):
    _FAULT_STAGES = (
        'after_create', 'after_copy', 'before_drop', 'after_rename', 'during_index',
    )

    def _assert_old_schema_intact(self, db: str):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(daily_contexts)')}
        self.assertNotIn('window_mode', cols)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0], 1)
        tables = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertNotIn('daily_contexts__mw_new', tables)
        conn.close()

    def test_fault_injection_rollback(self):
        for stage in self._FAULT_STAGES:
            with self.subTest(stage=stage):
                db = _tmp_db()
                _create_old_schema_db(db)
                dc._MIGRATION_INJECT_FAULT_AT = stage
                try:
                    with self.assertRaises(dc.DailyContextError):
                        dc.ensure_schema(db)
                finally:
                    dc._MIGRATION_INJECT_FAULT_AT = None
                self._assert_old_schema_intact(db)
                dc.ensure_schema(db)
                conn = sqlite3.connect(db)
                self.assertIn('window_mode', {r[1] for r in conn.execute('PRAGMA table_info(daily_contexts)')})
                conn.close()


class MigrationAutoincrementTests(unittest.TestCase):
    def test_autoincrement_preserved_after_migration(self):
        db = _tmp_db()
        _create_old_schema_db(db)
        dc.ensure_schema(db)
        conn = sqlite3.connect(db)
        seq_before = dc._read_sqlite_sequence(conn, 'daily_contexts')
        conn.execute('DELETE FROM daily_contexts WHERE id=1')
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, resident_generation,
                version, created_at, updated_at, window_mode, opened_at
            ) VALUES ('default','2026-07-27','Asia/Shanghai',4,2,0,'PROVISIONAL',0,1,1,
                      '2026-07-27 10:00:00','2026-07-27 10:00:00','legacy_daily','2026-07-27 10:00:00')'''
        )
        new_id = int(cur.lastrowid)
        conn.commit()
        self.assertGreater(new_id, 1)
        seq_after = dc._read_sqlite_sequence(conn, 'daily_contexts')
        self.assertIsNotNone(seq_after)
        if seq_before is not None:
            self.assertGreaterEqual(seq_after, seq_before)
        conn.close()

    def _seed_three_row_old_schema(self, db: str) -> int:
        _create_old_schema_db(db)
        conn = sqlite3.connect(db)
        for day, epoch in (('2026-07-27', 2), ('2026-07-28', 3)):
            conn.execute(
                '''INSERT INTO daily_contexts (
                    chat_id, local_day, timezone, boundary_hour, context_epoch,
                    boundary_message_id, status, carryover_count, resident_generation,
                    version, created_at, updated_at, is_backfill
                ) VALUES ('default', ?, 'Asia/Shanghai', 4, ?, 0, 'PROVISIONAL', 0, 1, 1,
                          '2026-07-27 10:00:00', '2026-07-27 10:00:00', 0)''',
                (day, epoch),
            )
        seq = dc._read_sqlite_sequence(conn, 'daily_contexts')
        conn.commit()
        conn.close()
        return int(seq or 3)

    def test_migration_with_deleted_max_id_preserves_sequence_floor(self):
        db = _tmp_db()
        old_seq = self._seed_three_row_old_schema(db)
        conn = sqlite3.connect(db)
        conn.execute('DELETE FROM daily_contexts WHERE id=3')
        conn.commit()
        conn.close()
        dc.ensure_schema(db)
        conn = sqlite3.connect(db)
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, resident_generation,
                version, created_at, updated_at, window_mode, opened_at
            ) VALUES ('default','2026-07-29','Asia/Shanghai',4,4,0,'PROVISIONAL',0,1,1,
                      '2026-07-29 10:00:00','2026-07-29 10:00:00','legacy_daily','2026-07-29 10:00:00')'''
        )
        new_id = int(cur.lastrowid)
        seq_after = dc._read_sqlite_sequence(conn, 'daily_contexts')
        conn.commit()
        conn.close()
        self.assertGreater(new_id, 2)
        self.assertGreaterEqual(int(seq_after or 0), old_seq)

    def test_migration_on_empty_table_preserves_sequence_floor(self):
        db = _tmp_db()
        old_seq = self._seed_three_row_old_schema(db)
        conn = sqlite3.connect(db)
        for table in (
            'daily_carryover_messages',
            'daily_resident_cursors',
            'daily_resident_turn_leases',
            'daily_message_contexts',
            'daily_resident_owners',
        ):
            conn.execute('DELETE FROM %s' % table)
        conn.execute('DELETE FROM daily_contexts')
        conn.commit()
        conn.close()
        dc.ensure_schema(db)
        conn = sqlite3.connect(db)
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, resident_generation,
                version, created_at, updated_at, window_mode, opened_at
            ) VALUES ('default','2026-07-29','Asia/Shanghai',4,1,0,'PROVISIONAL',0,1,1,
                      '2026-07-29 10:00:00','2026-07-29 10:00:00','legacy_daily','2026-07-29 10:00:00')'''
        )
        new_id = int(cur.lastrowid)
        seq_after = dc._read_sqlite_sequence(conn, 'daily_contexts')
        conn.commit()
        conn.close()
        self.assertGreater(new_id, old_seq)
        self.assertGreaterEqual(int(seq_after or 0), old_seq)


class StrictJsonContractTests(unittest.TestCase):
    def test_reject_non_json_integers(self):
        for bad in ('1', 1.0, 1.9, True, 0.9):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    cw.parse_strict_json_positive_int('source_context_id', bad)
                with self.assertRaises(ValueError):
                    cw.parse_strict_json_carryover_count(bad)


class NaturalCalendarDayTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        # Before 04:00, chat-day is still 2026-07-27; natural calendar day is 2026-07-28.
        self.ctx = _legacy_ctx(
            self.db,
            day='2026-07-27',
            now=datetime.datetime(2026, 7, 28, 1, 0, 0),
        )
        u = _insert(self.db, 'hayana', 'late', '2026-07-28 01:30:00')
        _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), u, 'user')

    def test_switch_at_0200_uses_natural_day(self):
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
            now=datetime.datetime(2026, 7, 28, 2, 0, 0),
        )
        target = dc.get_daily_context_by_id(out['target_context_id'], db_path=self.db)
        assert target is not None
        self.assertEqual(target['local_day'], '2026-07-28')

    def test_0359_and_0401_current_same_context(self):
        t1 = cw.get_current_context_window(
            db_path=self.db, now=datetime.datetime(2026, 7, 28, 3, 59, 0),
        )
        t2 = cw.get_current_context_window(
            db_path=self.db, now=datetime.datetime(2026, 7, 28, 4, 1, 0),
        )
        self.assertEqual(int(t1['id']), int(t2['id']))
        self.assertEqual(int(t1['context_epoch']), int(t2['context_epoch']))

    def test_0359_and_0401_switch_use_natural_day(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db, day='2026-07-27', now=datetime.datetime(2026, 7, 28, 3, 0, 0))
        u = _insert(db, 'hayana', 'pre', '2026-07-28 03:10:00')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        first = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=db,
            now=datetime.datetime(2026, 7, 28, 3, 59, 0),
        )
        t1 = dc.get_daily_context_by_id(first['target_context_id'], db_path=db)
        assert t1 is not None
        self.assertEqual(t1['local_day'], '2026-07-28')
        u2 = _insert(db, 'hayana', 'post', '2026-07-28 04:00:30')
        _map(db, int(t1['id']), int(t1['context_epoch']), u2, 'user')
        second = cw.switch_context_window(
            source_context_id=int(t1['id']),
            source_context_epoch=int(t1['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=db,
            now=datetime.datetime(2026, 7, 28, 4, 1, 0),
        )
        t2 = dc.get_daily_context_by_id(second['target_context_id'], db_path=db)
        assert t2 is not None
        self.assertEqual(t2['local_day'], '2026-07-28')

    def test_window_crossing_0400_keeps_opened_local_day(self):
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
            now=datetime.datetime(2026, 7, 28, 2, 30, 0),
        )
        summary_before = cw.current_window_summary(
            db_path=self.db, now=datetime.datetime(2026, 7, 28, 3, 59, 0),
        )
        summary_after = cw.current_window_summary(
            db_path=self.db, now=datetime.datetime(2026, 7, 28, 4, 1, 0),
        )
        self.assertEqual(summary_before['context_id'], out['target_context_id'])
        self.assertEqual(summary_before['context_id'], summary_after['context_id'])
        self.assertEqual(summary_before['opened_local_day'], '2026-07-28')
        self.assertEqual(summary_after['opened_local_day'], '2026-07-28')
        row = dc.get_daily_context_by_id(summary_before['context_id'], db_path=self.db)
        assert row is not None
        self.assertEqual(row['local_day'], '2026-07-28')


class IdempotencyEpochTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.ctx = _legacy_ctx(self.db)
        self.cid = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        u = _insert(self.db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(self.db, self.cid, self.epoch, u, 'user')

    def test_replay_wrong_source_epoch_409(self):
        req = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=0,
            request_id=req,
            db_path=self.db,
        )
        for wrong in (self.epoch + 1, max(1, self.epoch - 1)):
            if wrong == self.epoch:
                continue
            with self.subTest(epoch=wrong):
                with self.assertRaises(cw.IdempotencyMismatchError):
                    cw.switch_context_window(
                        source_context_id=self.cid,
                        source_context_epoch=wrong,
                        count=0,
                        request_id=req,
                        db_path=self.db,
                    )

    def test_replay_wrong_close_reason_409(self):
        req = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=self.cid,
            source_context_epoch=self.epoch,
            count=0,
            request_id=req,
            close_reason=cw.CLOSE_REASON_MANUAL,
            db_path=self.db,
        )
        with self.assertRaises(cw.IdempotencyMismatchError):
            cw.switch_context_window(
                source_context_id=self.cid,
                source_context_epoch=self.epoch,
                count=0,
                request_id=req,
                close_reason=cw.CLOSE_REASON_CAPACITY_RESCUE,
                db_path=self.db,
            )


class ConcurrencySwitchTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.ctx = _legacy_ctx(self.db)
        self.cid = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        u = _insert(self.db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(self.db, self.cid, self.epoch, u, 'user')

    def test_same_request_concurrent_identical_payload(self):
        req = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=5)
                out = cw.switch_context_window(
                    source_context_id=self.cid,
                    source_context_epoch=self.epoch,
                    count=0,
                    request_id=req,
                    db_path=self.db,
                )
                results.append(out)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['target_context_id'], results[1]['target_context_id'])
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual'"
            ).fetchone()[0],
            1,
        )
        conn.close()

    def test_same_request_concurrent_different_payload(self):
        req = str(uuid.uuid4())
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(count):
            try:
                barrier.wait(timeout=5)
                out = cw.switch_context_window(
                    source_context_id=self.cid,
                    source_context_epoch=self.epoch,
                    count=count,
                    request_id=req,
                    db_path=self.db,
                )
                outcomes.append(('ok', count, out['target_context_id']))
            except cw.IdempotencyMismatchError:
                outcomes.append(('mismatch', count, None))
            except Exception as exc:
                outcomes.append(('err', count, exc))

        t0 = threading.Thread(target=worker, args=(0,))
        t3 = threading.Thread(target=worker, args=(3,))
        t0.start()
        t3.start()
        t0.join(timeout=10)
        t3.join(timeout=10)
        kinds = {o[0] for o in outcomes}
        self.assertIn('ok', kinds)
        self.assertTrue('mismatch' in kinds or kinds == {'ok'})
        # At most one target may exist for this request_id.
        conn = sqlite3.connect(self.db)
        targets = conn.execute(
            'SELECT carryover_requested_count FROM daily_contexts WHERE switch_request_id=?',
            (req,),
        ).fetchall()
        conn.close()
        self.assertEqual(len(targets), 1)
        if 'mismatch' not in kinds:
            # Both saw identical first-writer semantics only if payload raced before insert;
            # require that stored count matches exactly one of the attempted payloads.
            self.assertIn(int(targets[0][0]), (0, 3))

    def test_two_request_ids_only_one_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(req):
            try:
                barrier.wait(timeout=5)
                out = cw.switch_context_window(
                    source_context_id=self.cid,
                    source_context_epoch=self.epoch,
                    count=0,
                    request_id=req,
                    db_path=self.db,
                )
                outcomes.append(('ok', out['target_context_id']))
            except cw.StaleSourceContextError:
                outcomes.append(('stale', None))
            except Exception as exc:
                outcomes.append(('err', exc))

        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        threads = [
            threading.Thread(target=worker, args=(r1,)),
            threading.Thread(target=worker, args=(r2,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        oks = [o for o in outcomes if o[0] == 'ok']
        stales = [o for o in outcomes if o[0] == 'stale']
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(stales), 1)
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual'"
            ).fetchone()[0],
            1,
        )
        # Failed CAS leaves no half target / unused request_id consumption beyond winner.
        self.assertEqual(
            conn.execute(
                'SELECT COUNT(*) FROM daily_contexts WHERE switch_request_id IN (?, ?)',
                (r1, r2),
            ).fetchone()[0],
            1,
        )
        source = conn.execute(
            'SELECT closed_at FROM daily_contexts WHERE id=?', (self.cid,),
        ).fetchone()
        self.assertIsNotNone(source[0])
        conn.close()


class CurrentSnapshotRaceTests(unittest.TestCase):
    def test_summary_not_mixed_across_switch(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db)
        cid = int(ctx['id'])
        epoch = int(ctx['context_epoch'])
        for i in range(2):
            u = _insert(db, 'hayana', 'u%d' % i, '2026-07-27 10:%02d:00' % i)
            a = _insert(db, 'fyodor', 'a%d' % i, '2026-07-27 10:%02d:30' % i)
            _map(db, cid, epoch, u, 'user')
            _map(db, cid, epoch, a, 'assistant')

        barrier = threading.Barrier(2)
        summaries = []
        switched = []

        def reader():
            barrier.wait(timeout=5)
            # Small delay so switch can interleave with a naive multi-conn reader;
            # single-snapshot implementation must still return a coherent row.
            summaries.append(cw.current_window_summary(db_path=db))

        def writer():
            barrier.wait(timeout=5)
            switched.append(cw.switch_context_window(
                source_context_id=cid,
                source_context_epoch=epoch,
                count=0,
                request_id=str(uuid.uuid4()),
                db_path=db,
            ))

        threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(len(switched), 1)
        s = summaries[0]
        # Snapshot must be wholly old or wholly new — never mix ids/epochs.
        if s['context_id'] == cid:
            self.assertEqual(s['context_epoch'], epoch)
            self.assertEqual(s['window_mode'], cw.WINDOW_MODE_LEGACY_DAILY)
        else:
            self.assertEqual(s['context_id'], switched[0]['target_context_id'])
            self.assertEqual(s['context_epoch'], switched[0]['target_context_epoch'])
            self.assertEqual(s['window_mode'], cw.WINDOW_MODE_MANUAL)


class CasRollbackTests(unittest.TestCase):
    def test_stale_version_cas_leaves_no_half_target(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db)
        cid = int(ctx['id'])
        epoch = int(ctx['context_epoch'])
        u = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(db, cid, epoch, u, 'user')
        conn = sqlite3.connect(db)
        conn.execute(
            'UPDATE daily_contexts SET version=version+1 WHERE id=?', (cid,),
        )
        conn.commit()
        conn.close()
        req = str(uuid.uuid4())
        # get_current still returns the row, but CAS uses stale version captured mid-flight.
        # Force by patching resolve to return old version.
        real_resolve = cw.resolve_canonical_context_row_conn

        def stale_resolve(conn, **kwargs):
            row = real_resolve(conn, **kwargs)
            row = dict(row)
            row['version'] = int(row['version']) - 1
            return row

        with mock.patch.object(cw, 'resolve_canonical_context_row_conn', side_effect=stale_resolve):
            with self.assertRaises(cw.StaleSourceContextError):
                cw.switch_context_window(
                    source_context_id=cid,
                    source_context_epoch=epoch,
                    count=0,
                    request_id=req,
                    db_path=db,
                )
        conn = sqlite3.connect(db)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute(
                'SELECT COUNT(*) FROM daily_carryover_messages WHERE context_id!=?',
                (cid,),
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            conn.execute(
                'SELECT 1 FROM daily_contexts WHERE switch_request_id=?', (req,),
            ).fetchone()
        )
        self.assertIsNone(
            conn.execute(
                'SELECT closed_at FROM daily_contexts WHERE id=?', (cid,),
            ).fetchone()[0]
        )
        conn.close()


class ClosedManualRecoveryTests(unittest.TestCase):
    def test_closed_manual_only_fail_closed(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db)
        u = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=db,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE daily_contexts SET closed_at='2026-07-27 12:00:00', close_reason='manual' "
            "WHERE window_mode=? AND closed_at IS NULL",
            (cw.WINDOW_MODE_MANUAL,),
        )
        conn.execute("DELETE FROM daily_contexts WHERE window_mode=?", (cw.WINDOW_MODE_LEGACY_DAILY,))
        conn.commit()
        conn.close()
        with self.assertRaises(cw.NoOpenContextWindowError):
            cw.get_current_context_window(db_path=db)

    def test_closed_manual_with_closed_legacy_fail_closed(self):
        """Must not resurrect the closed legacy source after manuals are closed."""
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = _legacy_ctx(db)
        u = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        out = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=db,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE daily_contexts SET closed_at='2026-07-27 12:00:00', close_reason='manual' "
            "WHERE id=?",
            (out['target_context_id'],),
        )
        conn.commit()
        # Legacy source is already closed by switch; both modes closed.
        rows = conn.execute(
            'SELECT id, window_mode, closed_at FROM daily_contexts ORDER BY id'
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r[2] for r in rows))
        with self.assertRaises(cw.NoOpenContextWindowError):
            cw.get_current_context_window(db_path=db)

    def test_multi_legacy_closed_latest_does_not_resurrect_older(self):
        """Post-migrate multi-day legacy: closing latest must not reopen older epoch."""
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctxs = []
        for i, day in enumerate(('2026-07-25', '2026-07-26', '2026-07-27')):
            ctxs.append(dc.get_or_create_daily_context(
                local_day=day,
                allow_backfill=True,
                db_path=db,
                now=datetime.datetime(2026, 7, 25 + i, 10, 0, 0),
            ))
        latest = max(ctxs, key=lambda c: int(c['context_epoch']))
        u = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
        _map(db, int(latest['id']), int(latest['context_epoch']), u, 'user')
        out = cw.switch_context_window(
            source_context_id=int(latest['id']),
            source_context_epoch=int(latest['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=db,
        )
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE daily_contexts SET closed_at='2026-07-27 12:00:00', close_reason='manual' "
            "WHERE id=?",
            (out['target_context_id'],),
        )
        conn.commit()
        older_open = conn.execute(
            '''SELECT COUNT(*) FROM daily_contexts
               WHERE window_mode=? AND closed_at IS NULL AND context_epoch < ?''',
            (cw.WINDOW_MODE_LEGACY_DAILY, int(latest['context_epoch'])),
        ).fetchone()[0]
        latest_closed = conn.execute(
            'SELECT closed_at FROM daily_contexts WHERE id=?',
            (int(latest['id']),),
        ).fetchone()[0]
        conn.close()
        self.assertGreater(int(older_open), 0)
        self.assertIsNotNone(latest_closed)
        with self.assertRaises(cw.NoOpenContextWindowError):
            cw.get_current_context_window(db_path=db)


class StrictJsonExtraTests(unittest.TestCase):
    def test_reject_null_and_bool_count(self):
        with self.assertRaises(ValueError):
            cw.parse_strict_json_positive_int('source_context_id', None)
        with self.assertRaises(ValueError):
            cw.parse_strict_json_carryover_count(False)
        with self.assertRaises(ValueError):
            cw.parse_strict_json_carryover_count(True)


if __name__ == '__main__':
    unittest.main()
