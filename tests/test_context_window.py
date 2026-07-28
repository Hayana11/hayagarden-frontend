"""Manual context window R0 — schema, resolver, switch, idempotency."""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
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
        carry = conn.execute('SELECT context_id FROM daily_carryover_messages').fetchone()
        self.assertEqual(int(carry[0]), 1)
        indexes = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='daily_contexts'"
            ).fetchall()
        }
        self.assertIn('idx_daily_contexts_legacy_day_unique', indexes)
        self.assertIn('idx_daily_contexts_open_manual_unique', indexes)
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


if __name__ == '__main__':
    unittest.main()
