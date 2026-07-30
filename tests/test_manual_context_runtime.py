"""Manual context window runtime — canonical resolver in prepare_daily_turn."""
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

import config_store
from chat import context_window as cw
from chat import daily_context as dc
from chat import daily_runtime as dr


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db: str) -> None:
    conn = sqlite3.connect(db)
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


def _insert(db: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)',
        (author, content, created_at),
    )
    mid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return mid


def _map(db: str, context_id: int, context_epoch: int, message_id: int, role: str) -> None:
    dc.record_daily_message_context(
        message_id,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=1,
        role=role,
        db_path=db,
    )


def _legacy_ctx(db: str, day: str = '2026-07-27', **kwargs) -> dict:
    return dc.get_or_create_daily_context(
        local_day=day,
        db_path=db,
        now=kwargs.pop('now', datetime.datetime(2026, 7, 27, 10, 0, 0)),
        allow_backfill=kwargs.pop('allow_backfill', day != '2026-07-27'),
        **kwargs,
    )


def _prepare(db: str, uid: int, **kwargs):
    with mock.patch.object(config_store, 'get_bool', return_value=True), \
         mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
        return dr.prepare_daily_turn(
            user_message_id=uid,
            db_path=db,
            now=kwargs.get('now', datetime.datetime(2026, 7, 27, 10, 0, 0)),
            wall_now=kwargs.get('wall_now', kwargs.get('now', datetime.datetime(2026, 7, 27, 10, 0, 0))),
            static_system='S',
            **{k: v for k, v in kwargs.items() if k not in ('now', 'wall_now')},
        )


def _count_contexts(db: str) -> int:
    conn = sqlite3.connect(db)
    n = int(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0])
    conn.close()
    return n


def _seed_formal_rounds(db: str, ctx: dict, count: int) -> list[int]:
    ids: list[int] = []
    base = datetime.datetime(2026, 7, 27, 10, 0, 0)
    for i in range(count):
        t = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
        uid = _insert(db, 'hayana', 'user %d' % i, t)
        aid = _insert(db, 'assistant', 'reply %d' % i, t)
        _map(db, int(ctx['id']), int(ctx['context_epoch']), uid, 'user')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), aid, 'assistant')
        ids.extend([uid, aid])
    return ids



def _offline_hooks():
    import tempfile
    return cw.offline_switch_hooks(tempfile.mkdtemp(prefix='cw-hooks-'))



class ManualContextRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        dr.reset_bindings_for_tests()

    def tearDown(self):
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_manual_switch_next_turn_uses_target_context(self):
        source = _legacy_ctx(self.db)
        _seed_formal_rounds(self.db, source, 5)
        switched = cw.switch_context_window(
            source_context_id=int(source['id']),
            source_context_epoch=int(source['context_epoch']),
            count=3,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        
            hooks=_offline_hooks())
        target_id = int(switched['target_context_id'])
        target_epoch = int(switched['target_context_epoch'])
        expected_carryover = list(switched['selected_message_ids'])

        uid = _insert(self.db, 'hayana', 'after switch', '2026-07-27 12:00:00')
        plan = _prepare(self.db, uid)
        try:
            self.assertEqual(plan.context_id, target_id)
            self.assertEqual(plan.context_epoch, target_epoch)
            self.assertEqual(plan.resident_generation, 1)
            mapping = dc.get_message_context(uid, db_path=self.db)
            self.assertIsNotNone(mapping)
            self.assertEqual(int(mapping['context_id']), target_id)
            carryover_ids = list(plan.assembly.get('manifest', {}).get('carryover_message_ids') or [])
            if not carryover_ids:
                carryover_ids = [
                    int(m['message_id'])
                    for m in (plan.assembly.get('carryover_messages') or [])
                ]
            self.assertEqual(sorted(carryover_ids), sorted(expected_carryover))
            source_only = _insert(self.db, 'hayana', 'only source', '2026-07-27 09:00:00')
            _map(self.db, int(source['id']), int(source['context_epoch']), source_only, 'user')
            hist_ids = [
                int(m['message_id'])
                for m in (plan.assembly.get('current_day_history') or [])
            ]
            self.assertNotIn(source_only, hist_ids)
        finally:
            dr._release_lease(plan)

    def test_manual_multiple_same_day_windows_uses_only_open_target(self):
        first = _legacy_ctx(self.db)
        _seed_formal_rounds(self.db, first, 2)
        out1 = cw.switch_context_window(
            source_context_id=int(first['id']),
            source_context_epoch=int(first['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        
            hooks=_offline_hooks())
        target1_id = int(out1['target_context_id'])
        uid1 = _insert(self.db, 'hayana', 'in manual 1', '2026-07-27 11:00:00')
        _map(self.db, target1_id, int(out1['target_context_epoch']), uid1, 'user')

        current = cw.get_current_context_window(db_path=self.db)
        out2 = cw.switch_context_window(
            source_context_id=int(current['id']),
            source_context_epoch=int(current['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        
            hooks=_offline_hooks())
        target2_id = int(out2['target_context_id'])
        uid = _insert(self.db, 'hayana', 'next send', '2026-07-27 12:00:00')
        plan = _prepare(self.db, uid)
        try:
            self.assertEqual(plan.context_id, target2_id)
            self.assertNotEqual(plan.context_id, target1_id)
        finally:
            dr._release_lease(plan)

    def test_manual_window_survives_0400_without_new_context(self):
        start = datetime.datetime(2026, 7, 27, 3, 59, 59)
        ctx = _legacy_ctx(self.db, day='2026-07-26', now=start, allow_backfill=False)
        before = _count_contexts(self.db)
        uid = _insert(self.db, 'hayana', 'late', start.strftime('%Y-%m-%d %H:%M:%S'))
        plan = _prepare(
            self.db,
            uid,
            now=start,
            wall_now=datetime.datetime(2026, 7, 27, 4, 0, 1),
        )
        try:
            self.assertEqual(plan.context_id, int(ctx['id']))
            self.assertEqual(_count_contexts(self.db), before)
        finally:
            dr._release_lease(plan)

    def test_manual_window_survives_calendar_date_change_without_new_context(self):
        ctx = _legacy_ctx(self.db, day='2026-07-27')
        before = _count_contexts(self.db)
        uid = _insert(self.db, 'hayana', 'next day msg', '2026-07-28 10:00:00')
        plan = _prepare(
            self.db,
            uid,
            now=datetime.datetime(2026, 7, 28, 10, 0, 0),
            wall_now=datetime.datetime(2026, 7, 28, 10, 0, 0),
        )
        try:
            self.assertEqual(plan.context_id, int(ctx['id']))
            self.assertEqual(_count_contexts(self.db), before)
        finally:
            dr._release_lease(plan)

    def test_manual_closed_only_fails_without_bootstrap_or_resurrection(self):
        ctx = _legacy_ctx(self.db)
        cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        
            hooks=_offline_hooks())
        current = cw.get_current_context_window(db_path=self.db)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE daily_contexts SET closed_at='2026-07-28 12:00:00', close_reason='manual' WHERE id=?",
            (int(current['id']),),
        )
        conn.commit()
        conn.close()
        uid = _insert(self.db, 'hayana', 'orphan', '2026-07-28 12:01:00')
        with self.assertRaises(dr.DailyRuntimeError) as ctx_err:
            _prepare(self.db, uid)
        self.assertEqual(ctx_err.exception.error_code, 'no_open_context_window')

    def test_normal_send_does_not_create_context_window(self):
        ctx = _legacy_ctx(self.db)
        before = _count_contexts(self.db)
        uid = _insert(self.db, 'hayana', 'normal', '2026-07-27 10:30:00')
        plan = _prepare(self.db, uid)
        try:
            self.assertEqual(plan.context_id, int(ctx['id']))
            self.assertEqual(_count_contexts(self.db), before)
        finally:
            dr._release_lease(plan)


if __name__ == '__main__':
    unittest.main()
