"""Step 10 continuity fixes: rewrite cold replay + regen cursor contract."""
from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import config_store
from chat import daily_context as dc
from chat import daily_history as dh
from chat import rewrite_staging as rw

_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)


def _tmp_db() -> str:
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    return path


def _init_chat_messages(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            branches TEXT DEFAULT '',
            branch_idx INTEGER DEFAULT 0,
            cache_info TEXT DEFAULT '',
            choices TEXT DEFAULT '',
            image_url TEXT DEFAULT '',
            file_url TEXT DEFAULT '',
            file_name TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.execute(
        '''CREATE TABLE chat_edit_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fork_msg_id INTEGER,
            original_content TEXT,
            messages_json TEXT
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)
    conn = sqlite3.connect(db_path)
    rw.ensure_schema(conn)
    conn.commit()
    conn.close()


def _insert(db_path: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at) VALUES (?,?,?)',
        (author, content, created_at),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


_real_current_chat_day = dc._current_chat_day


def _pinned_current_chat_day(now=None):
    return _real_current_chat_day(now or _FIXED_NOW)


dc._current_chat_day = _pinned_current_chat_day


class RewriteColdReplayTests(unittest.TestCase):
    def test_edit_finalize_links_messages_and_advances_cursor_for_cold_replay(self):
        db = _tmp_db()
        try:
            with mock.patch.object(config_store, 'get_bool', return_value=True):
                _init_chat_messages(db)
                ctx = dc.get_or_create_daily_context(
                    chat_id='default',
                    local_day='2026-07-27',
                    db_path=db,
                    now=_FIXED_NOW,
                )
                context_id = int(ctx['id'])
                context_epoch = int(ctx['context_epoch'])
                resident_generation = int(ctx['resident_generation'])

                ts_u = '2026-07-27 09:00:00'
                ts_a = '2026-07-27 09:01:00'
                uid = _insert(db, 'hayana', 'original user', ts_u)
                aid = _insert(db, 'fyodor', 'original assistant', ts_a)
                dc.record_daily_message_context(
                    uid,
                    context_id=context_id,
                    context_epoch=context_epoch,
                    resident_generation=resident_generation,
                    role='user',
                    db_path=db,
                )
                dc.record_daily_message_context(
                    aid,
                    context_id=context_id,
                    context_epoch=context_epoch,
                    resident_generation=resident_generation,
                    role='assistant',
                    db_path=db,
                )
                dc.advance_resident_history_cursor(
                    context_id, resident_generation, aid, db_path=db,
                )

                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                prep = rw.prepare_edit(
                    conn, source_message_id=uid, edited_content='edited user',
                )
                rw.store_candidate(
                    conn,
                    prep['rewrite_id'],
                    content='edited assistant',
                )
                result = rw.activate_edit(conn, prep['rewrite_id'])
                conn.close()

                staging = result.get('staging') or {}
                rw.finalize_rewrite_daily_continuity(result, staging, db_path=db)

                new_uid = int(result['message_id'])
                new_aid = int(result['assistant_message_id'])
                cursor = dc.get_resident_history_cursor(
                    context_id, resident_generation, db_path=db,
                )
                self.assertEqual(int(cursor), new_aid)

                built = dh.build_daily_window_context(
                    chat_id='default',
                    daily_context=dc.get_daily_context_by_id(context_id, db_path=db),
                    is_cold=True,
                    db_path=db,
                )
                history_ids = [
                    int(m['message_id']) for m in built.get('current_day_history') or []
                ]
                self.assertIn(new_uid, history_ids)
                self.assertIn(new_aid, history_ids)
                self.assertEqual(
                    int((built.get('manifest') or {}).get('replayed_through_message_id') or 0),
                    new_aid,
                )
                self.assertNotIn(uid, history_ids)
                self.assertNotIn(aid, history_ids)
        finally:
            os.unlink(db)

    def test_regen_finalize_advances_cursor_to_same_assistant(self):
        db = _tmp_db()
        try:
            with mock.patch.object(config_store, 'get_bool', return_value=True):
                _init_chat_messages(db)
                ctx = dc.get_or_create_daily_context(
                    chat_id='default',
                    local_day='2026-07-27',
                    db_path=db,
                    now=_FIXED_NOW,
                )
                context_id = int(ctx['id'])
                context_epoch = int(ctx['context_epoch'])
                resident_generation = int(ctx['resident_generation'])

                uid = _insert(db, 'hayana', 'user', '2026-07-27 09:00:00')
                aid = _insert(db, 'fyodor', 'old assistant', '2026-07-27 09:01:00')
                dc.record_daily_message_context(
                    uid,
                    context_id=context_id,
                    context_epoch=context_epoch,
                    resident_generation=resident_generation,
                    role='user',
                    db_path=db,
                )
                dc.record_daily_message_context(
                    aid,
                    context_id=context_id,
                    context_epoch=context_epoch,
                    resident_generation=resident_generation,
                    role='assistant',
                    db_path=db,
                )
                dc.advance_resident_history_cursor(
                    context_id, resident_generation, uid, db_path=db,
                )

                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                prep = rw.prepare_regen(conn, source_assistant_id=aid)
                rw.store_candidate(conn, prep['rewrite_id'], content='regen assistant')
                result = rw.activate_regen(conn, prep['rewrite_id'])
                conn.close()

                staging = result.get('staging') or {}
                rw.finalize_rewrite_daily_continuity(result, staging, db_path=db)

                cursor = dc.get_resident_history_cursor(
                    context_id, resident_generation, db_path=db,
                )
                self.assertEqual(int(cursor), aid)
                self.assertEqual(int(result['assistant_message_id']), aid)
        finally:
            os.unlink(db)

    def test_edit_finalize_uses_source_mapping_not_predecessor_context(self):
        """Cross-context boundary: source exact dmc wins over predecessor old context."""
        db = _tmp_db()
        try:
            with mock.patch.object(config_store, 'get_bool', return_value=True):
                _init_chat_messages(db)
                start = datetime.datetime(2026, 7, 27, 3, 0, 0)
                finish = datetime.datetime(2026, 7, 27, 10, 0, 0)
                old_ctx = dc.get_or_create_daily_context(
                    chat_id='boundary',
                    local_day='2026-07-26',
                    db_path=db,
                    now=start,
                    allow_backfill=True,
                )
                old_id = int(old_ctx['id'])
                old_epoch = int(old_ctx['context_epoch'])
                prev_id = _insert(
                    db, 'hayana', 'prev in old',
                    start.strftime('%Y-%m-%d %H:%M:%S'),
                )
                dc.record_daily_message_context(
                    prev_id,
                    context_id=old_id,
                    context_epoch=old_epoch,
                    resident_generation=1,
                    role='user',
                    db_path=db,
                )
                dc.advance_resident_history_cursor(old_id, 1, prev_id, db_path=db)

                new_ctx = dc.get_or_create_daily_context(
                    chat_id='boundary',
                    local_day='2026-07-27',
                    db_path=db,
                    now=finish,
                )
                new_id = int(new_ctx['id'])
                new_epoch = int(new_ctx['context_epoch'])
                new_gen = int(new_ctx['resident_generation'])
                source_id = _insert(
                    db, 'hayana', 'source in new',
                    finish.strftime('%Y-%m-%d %H:%M:%S'),
                )
                dc.record_daily_message_context(
                    source_id,
                    context_id=new_id,
                    context_epoch=new_epoch,
                    resident_generation=new_gen,
                    role='user',
                    db_path=db,
                )

                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                prep = rw.prepare_edit(
                    conn,
                    source_message_id=source_id,
                    edited_content='edited user',
                )
                rw.store_candidate(
                    conn, prep['rewrite_id'], content='edited assistant',
                )
                result = rw.activate_edit(conn, prep['rewrite_id'])
                conn.close()

                staging = result.get('staging') or {}
                rw.finalize_rewrite_daily_continuity(result, staging, db_path=db)

                new_uid = int(result['message_id'])
                new_aid = int(result['assistant_message_id'])

                conn = sqlite3.connect(db)
                old_new_rows = conn.execute(
                    'SELECT message_id FROM daily_message_contexts '
                    'WHERE context_id=? AND message_id IN (?, ?)',
                    (old_id, new_uid, new_aid),
                ).fetchall()
                new_uid_row = conn.execute(
                    'SELECT context_id, context_epoch, resident_generation '
                    'FROM daily_message_contexts WHERE message_id=?',
                    (new_uid,),
                ).fetchone()
                new_aid_row = conn.execute(
                    'SELECT context_id, context_epoch, resident_generation '
                    'FROM daily_message_contexts WHERE message_id=?',
                    (new_aid,),
                ).fetchone()
                conn.close()

                self.assertEqual(len(old_new_rows), 0)
                self.assertIsNotNone(new_uid_row)
                self.assertIsNotNone(new_aid_row)
                self.assertEqual(int(new_uid_row[0]), new_id)
                self.assertEqual(int(new_uid_row[1]), new_epoch)
                self.assertEqual(int(new_uid_row[2]), new_gen)
                self.assertEqual(int(new_aid_row[0]), new_id)
                self.assertEqual(int(new_aid_row[1]), new_epoch)
                self.assertEqual(int(new_aid_row[2]), new_gen)

                new_cursor = dc.get_resident_history_cursor(new_id, new_gen, db_path=db)
                old_cursor = dc.get_resident_history_cursor(old_id, 1, db_path=db)
                self.assertEqual(int(new_cursor), new_aid)
                self.assertEqual(int(old_cursor), prev_id)

                built = dh.build_daily_window_context(
                    chat_id='boundary',
                    daily_context=dc.get_daily_context_by_id(new_id, db_path=db),
                    is_cold=True,
                    db_path=db,
                )
                history_ids = [
                    int(m['message_id']) for m in built.get('current_day_history') or []
                ]
                self.assertIn(new_uid, history_ids)
                self.assertIn(new_aid, history_ids)
                self.assertEqual(
                    int((built.get('manifest') or {}).get('replayed_through_message_id') or 0),
                    new_aid,
                )
        finally:
            os.unlink(db)


if __name__ == '__main__':
    unittest.main()
