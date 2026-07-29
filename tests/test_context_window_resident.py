"""Manual context window resident handoff and epoch fencing (#154)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
import uuid
from unittest import mock

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
        '''CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            content TEXT,
            thinking TEXT,
            tool_calls TEXT,
            cache_info TEXT,
            choices TEXT,
            image_url TEXT,
            created_at TEXT
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


def _legacy_ctx(db: str) -> dict:
    return dc.get_or_create_daily_context(
        local_day='2026-07-27',
        db_path=db,
        now=__import__('datetime').datetime(2026, 7, 27, 10, 0, 0),
    )


class ResidentSwitchCloseTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        dr.reset_bindings_for_tests()
        self.resident = mock.MagicMock()
        self.resident._kill = mock.MagicMock()

    def tearDown(self):
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _bind_source(self, ctx: dict) -> None:
        epoch = int(ctx['context_epoch'])
        gen = int(ctx.get('resident_generation') or 1)
        key = dc.make_resident_key(chat_id='default', context_epoch=epoch, resident_generation=gen)
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=key,
            context_id=int(ctx['id']),
            context_epoch=epoch,
            resident_generation=gen,
            bound_cursor_message_id=None,
            process_generation=1,
            tool_profile='daily',
        ))

    def test_switch_closes_matching_resident_binding(self):
        ctx = _legacy_ctx(self.db)
        u = _insert(self.db, 'hayana', 'hello', '2026-07-27 10:00:00')
        _map(self.db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        self._bind_source(ctx)
        out = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        )
        closed = dr.close_local_resident_for_context_switch(
            self.resident,
            source_context_id=int(out['source_context_id']),
            source_context_epoch=int(out['source_context_epoch']),
            source_resident_generation=int(out['source_resident_generation']),
        )
        self.assertTrue(closed)
        self.resident._kill.assert_called_once()
        self.assertIsNone(dr.get_local_binding())

    def test_stale_source_does_not_close_new_binding(self):
        ctx = _legacy_ctx(self.db)
        u = _insert(self.db, 'hayana', 'hello', '2026-07-27 10:00:00')
        _map(self.db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        out = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
        )
        target_epoch = int(out['target_context_epoch'])
        new_key = dc.make_resident_key(
            chat_id='default', context_epoch=target_epoch, resident_generation=1,
        )
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=new_key,
            context_id=int(out['target_context_id']),
            context_epoch=target_epoch,
            resident_generation=1,
            bound_cursor_message_id=None,
            process_generation=2,
            tool_profile='daily',
        ))
        closed = dr.close_local_resident_for_context_switch(
            self.resident,
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            source_resident_generation=int(ctx.get('resident_generation') or 1),
        )
        self.assertFalse(closed)
        self.resident._kill.assert_not_called()
        self.assertIsNotNone(dr.get_local_binding())

    def test_replay_does_not_double_close_new_binding(self):
        ctx = _legacy_ctx(self.db)
        req = str(uuid.uuid4())
        out1 = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        out2 = cw.switch_context_window(
            source_context_id=int(ctx['id']),
            source_context_epoch=int(ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        self.assertEqual(out1['target_context_id'], out2['target_context_id'])
        self._bind_source({'id': out1['target_context_id'], 'context_epoch': out1['target_context_epoch'], 'resident_generation': 1})
        closed = dr.close_local_resident_for_context_switch(
            self.resident,
            source_context_id=int(out2['source_context_id']),
            source_context_epoch=int(out2['source_context_epoch']),
            source_resident_generation=int(out2['source_resident_generation']),
        )
        self.assertFalse(closed)


class EpochTokenContextIdTests(unittest.TestCase):
    def test_make_epoch_token_includes_context_id(self):
        token = dc.make_epoch_token(
            chat_id='default',
            context_id=9,
            context_epoch=3,
            resident_generation=2,
        )
        self.assertEqual(token['context_id'], 9)

    def test_stale_context_id_not_current(self):
        db = _tmp_db()
        try:
            _init_chat_messages(db)
            dc.ensure_schema(db)
            ctx = _legacy_ctx(db)
            token = dc.make_epoch_token(
                chat_id='default',
                context_id=int(ctx['id']),
                context_epoch=int(ctx['context_epoch']),
                resident_generation=1,
            )
            self.assertTrue(dc.is_epoch_current(token, db_path=db))
            u = _insert(db, 'hayana', 'x', '2026-07-27 10:00:00')
            _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
            cw.switch_context_window(
                source_context_id=int(ctx['id']),
                source_context_epoch=int(ctx['context_epoch']),
                count=0,
                request_id=str(uuid.uuid4()),
                db_path=db,
            )
            self.assertFalse(dc.is_epoch_current(token, db_path=db))
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
