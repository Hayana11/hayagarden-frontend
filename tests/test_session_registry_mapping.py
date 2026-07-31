"""Session Registry + message-event Mapping — flag-off data layer tests.

Uses only temporary SQLite + temporary JSONL. Never touches production DBs.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Hard refuse production / repo root memories.db
_FORBIDDEN = (
    '/opt/frontend/memories.db',
    str(ROOT / 'memories.db'),
)

from chat import daily_context as dc
from chat.claude_event_mapping import (
    MappingPassRequest,
    get_user_canonical_by_event_uuid,
    run_mapping_pass,
)
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_READY,
    SessionRegistryConflict,
    cas_advance_scan_offset,
    get_context_claude_session,
    register_context_claude_session,
)


def _assert_temp_db(path: str) -> None:
    resolved = os.path.abspath(path)
    for bad in _FORBIDDEN:
        if resolved == os.path.abspath(bad):
            raise AssertionError(f'refused production db path: {resolved}')
    if resolved.startswith('/opt/frontend/'):
        raise AssertionError(f'refused /opt/frontend path: {resolved}')


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='sr-map-')
    db = os.path.join(tmp, 'test.db')
    _assert_temp_db(db)
    claude_home = os.path.join(tmp, '.claude')
    cwd = os.path.join(tmp, 'proj')
    os.makedirs(cwd, exist_ok=True)
    return tmp, db, claude_home, cwd


def _init_db(db: str) -> None:
    _assert_temp_db(db)
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db)


def _insert_msg(db: str, author: str, content: str) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content) VALUES (?,?)',
        (author, content),
    )
    conn.commit()
    mid = int(cur.lastrowid)
    conn.close()
    return mid


def _bind_msg(
    db: str, message_id: int, *, context_id: int, epoch: int, gen: int, role: str,
) -> None:
    dc.record_daily_message_context(
        int(message_id),
        context_id=int(context_id),
        context_epoch=int(epoch),
        resident_generation=int(gen),
        role=role,
        db_path=db,
    )


def _write_jsonl(path: Path, lines: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ''.join(line if line.endswith('\n') else line + '\n' for line in lines)
    raw = data.encode('utf-8')
    path.write_bytes(raw)
    return len(raw)


def _line(uuid: str, typ: str, *, session: str, parent: Optional[str], content: Any) -> str:
    import json
    obj = {
        'type': typ,
        'uuid': uuid,
        'parentUuid': parent,
        'sessionId': session,
        'cwd': '/tmp/synth',
        'message': {'role': 'user' if typ == 'user' else 'assistant', 'content': content},
    }
    return json.dumps(obj, ensure_ascii=False)


SESSION = '11111111-2222-3333-4444-555555555555'


class SessionRegistryMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.db, self.claude_home, self.cwd = _tmp_workspace()
        _init_db(self.db)
        self.ctx = dc.get_or_create_daily_context(
            chat_id='default',
            local_day='2026-07-30',
            db_path=self.db,
            now=__import__('datetime').datetime(2026, 7, 30, 12, 0, 0),
        )
        self.context_id = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        self.gen = int(self.ctx['resident_generation'])

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, *, scan_offset: int = 0, source: str = 'test_cold'):
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            claude_session_id=SESSION,
            cwd=self.cwd,
            source=source,
            scan_offset=scan_offset,
            claude_home=self.claude_home,
            db_path=self.db,
        )

    def _transcript_path(self) -> Path:
        row = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        assert row is not None
        return Path(row['transcript_path'])

    # ---- 1) core happy path ----
    def test_core_register_map_canonical_query(self) -> None:
        reg = self._register(scan_offset=0)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), 0)
        self.assertTrue(str(reg['transcript_path']).endswith(SESSION + '.jsonl'))

        user_id = _insert_msg(self.db, 'hayana', '前端用户原文-alpha')
        asst_id = _insert_msg(self.db, 'fyodor', '助手回复-alpha')
        _bind_msg(self.db, user_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')

        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u-aaaa-0001', 'user', session=SESSION, parent=None, content='JSONL候选用户正文应被忽略'),
            _line(
                'a-aaaa-0001', 'assistant', session=SESSION, parent='u-aaaa-0001',
                content=[{'type': 'text', 'text': 'asst'}],
            ),
        ])

        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id,
                context_epoch=self.epoch,
                resident_generation=self.gen,
                chat_id='default',
                user_message_id=user_id,
                assistant_message_id=asst_id,
                expected_start_offset=0,
                observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertTrue(result.ok, result.error_code)
        self.assertEqual(result.scan_offset, end)
        self.assertIn('u-aaaa-0001', result.mapped_event_uuids)
        self.assertIn('a-aaaa-0001', result.mapped_event_uuids)

        canonical = get_user_canonical_by_event_uuid(['u-aaaa-0001', 'missing'], db_path=self.db)
        self.assertEqual(canonical, {'u-aaaa-0001': '前端用户原文-alpha'})

        row = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(row['scan_offset']), end)
        self.assertEqual(int(row['last_mapped_message_id']), asst_id)
        self.assertEqual(row['scan_status'], SCAN_STATUS_READY)

    # ---- 2) complex: tool 1:N + restart resume + idempotent replay ----
    def test_complex_tool_multimap_restart_resume_replay(self) -> None:
        self._register(scan_offset=0)
        user1 = _insert_msg(self.db, 'hayana', '请读文件')
        asst1 = _insert_msg(self.db, 'fyodor', '读完了')
        _bind_msg(self.db, user1, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst1, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')

        path = self._transcript_path()
        turn1 = [
            _line('u1', 'user', session=SESSION, parent=None, content='请读文件'),
            _line(
                't1', 'assistant', session=SESSION, parent='u1',
                content=[{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {}}],
            ),
            _line(
                'r1', 'user', session=SESSION, parent='t1',
                content=[{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok', 'is_error': False}],
            ),
            _line(
                'a1', 'assistant', session=SESSION, parent='r1',
                content=[{'type': 'text', 'text': '读完了'}],
            ),
        ]
        end1 = _write_jsonl(path, turn1)
        r1 = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user1, assistant_message_id=asst1,
                expected_start_offset=0, observed_end_offset=end1,
            ),
            db_path=self.db,
        )
        self.assertTrue(r1.ok, r1.error_code)
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            'SELECT event_uuid, role FROM chat_message_claude_events '
            'WHERE message_id=? ORDER BY event_uuid',
            (asst1,),
        ).fetchall()
        roles = {r[0]: r[1] for r in rows}
        self.assertEqual(roles['t1'], 'tool_use')
        self.assertEqual(roles['r1'], 'tool_result_user')
        self.assertEqual(roles['a1'], 'assistant')
        count_before = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events',
        ).fetchone()[0]
        conn.close()

        # Simulate process restart: reopen DB, continue from persisted offset.
        dc._SCHEMA_READY.discard(os.path.abspath(self.db))
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), end1)

        user2 = _insert_msg(self.db, 'hayana', '第二轮')
        asst2 = _insert_msg(self.db, 'fyodor', '收到')
        _bind_msg(self.db, user2, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst2, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')

        turn2 = [
            _line('u2', 'user', session=SESSION, parent='a1', content='第二轮'),
            _line(
                'a2', 'assistant', session=SESSION, parent='u2',
                content=[{'type': 'text', 'text': '收到'}],
            ),
        ]
        # Append second turn bytes
        extra = ''.join(l + '\n' for l in turn2).encode('utf-8')
        with open(path, 'ab') as fh:
            fh.write(extra)
        end2 = end1 + len(extra)

        r2 = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user2, assistant_message_id=asst2,
                expected_start_offset=end1, observed_end_offset=end2,
            ),
            db_path=self.db,
        )
        self.assertTrue(r2.ok, r2.error_code)

        # Replay old delta: idempotent, no duplicate rows, offset stays.
        replay = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user1, assistant_message_id=asst1,
                expected_start_offset=0, observed_end_offset=end1,
            ),
            db_path=self.db,
        )
        self.assertFalse(replay.ok)
        self.assertEqual(replay.error_code, 'scan_offset_cas_conflict')
        conn = sqlite3.connect(self.db)
        count_after = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events',
        ).fetchone()[0]
        self.assertEqual(count_after, count_before + 2)  # u2 + a2 only
        off = conn.execute(
            'SELECT scan_offset FROM context_claude_sessions '
            'WHERE context_id=? AND resident_generation=?',
            (self.context_id, self.gen),
        ).fetchone()[0]
        self.assertEqual(int(off), end2)
        conn.close()

        # True idempotent replay at current cursor would need same planned rows;
        # re-run turn2 with matching expected offset after manually resetting is
        # out of scope — instead verify same-event re-insert via direct pass
        # after CAS restore is refused. Explicit idempotent event rewrite:
        from chat.claude_event_mapping import _insert_mapping_row
        conn = dc._connect(self.db)
        conn.execute('BEGIN')
        uid = _insert_mapping_row(conn, {
            'event_uuid': 'u1',
            'message_id': user1,
            'role': 'user',
            'claude_session_id': SESSION,
            'context_id': self.context_id,
            'context_epoch': self.epoch,
            'resident_generation': self.gen,
            'jsonl_byte_offset': 0,
        })
        self.assertEqual(uid, 'u1')
        conn.commit()
        conn.close()
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            count_after,
        )
        conn.close()

    # ---- 3) critical failures ----
    def test_fail_closed_ambiguous_candidates_and_cas(self) -> None:
        self._register(scan_offset=0)
        user_id = _insert_msg(self.db, 'hayana', 'x')
        asst_id = _insert_msg(self.db, 'fyodor', 'y')
        _bind_msg(self.db, user_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')

        path = self._transcript_path()
        # Two candidate users in one range → ambiguous
        end = _write_jsonl(path, [
            _line('u-a', 'user', session=SESSION, parent=None, content='one'),
            _line(
                'a-a', 'assistant', session=SESSION, parent='u-a',
                content=[{'type': 'text', 'text': 'a'}],
            ),
            _line('u-b', 'user', session=SESSION, parent='a-a', content='two'),
            _line(
                'a-b', 'assistant', session=SESSION, parent='u-b',
                content=[{'type': 'text', 'text': 'b'}],
            ),
        ])
        bad = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(bad.ok)
        self.assertEqual(bad.error_code, 'candidate_user_ambiguous')
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        row = conn.execute(
            'SELECT scan_offset, scan_status, scan_error_code FROM context_claude_sessions',
        ).fetchone()
        self.assertEqual(int(row[0]), 0)
        self.assertEqual(row[1], SCAN_STATUS_BLOCKED)
        self.assertEqual(row[2], 'candidate_user_ambiguous')
        conn.close()

    def test_fail_closed_epoch_mismatch_and_event_conflict(self) -> None:
        self._register(scan_offset=0)
        user_id = _insert_msg(self.db, 'hayana', 'canon')
        asst_id = _insert_msg(self.db, 'fyodor', 'a')
        _bind_msg(self.db, user_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u-x', 'user', session=SESSION, parent=None, content='x'),
            _line(
                'a-x', 'assistant', session=SESSION, parent='u-x',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])

        # Wrong epoch
        wrong = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch + 99,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(wrong.ok)
        self.assertEqual(wrong.error_code, 'epoch_mismatch')

        # Reset BLOCKED → READY for next case (test-only direct SQL on temp db)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE context_claude_sessions SET scan_status='READY', scan_error_code=NULL",
        )
        conn.commit()
        conn.close()

        ok = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertTrue(ok.ok, ok.error_code)

        # Conflict: same event_uuid → different message
        user2 = _insert_msg(self.db, 'hayana', 'other')
        _bind_msg(self.db, user2, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        from chat.claude_event_mapping import MappingConflict, _insert_mapping_row
        conn = dc._connect(self.db)
        conn.execute('BEGIN')
        with self.assertRaises(MappingConflict):
            _insert_mapping_row(conn, {
                'event_uuid': 'u-x',
                'message_id': user2,
                'role': 'user',
                'claude_session_id': SESSION,
                'context_id': self.context_id,
                'context_epoch': self.epoch,
                'resident_generation': self.gen,
                'jsonl_byte_offset': 0,
            })
        conn.rollback()
        conn.close()

    def test_registry_idempotent_and_session_conflict(self) -> None:
        a = self._register(scan_offset=0)
        b = self._register(scan_offset=0)
        self.assertEqual(a['claude_session_id'], b['claude_session_id'])
        # Different source on same key → conflict
        with self.assertRaises(SessionRegistryConflict):
            register_context_claude_session(
                context_id=self.context_id,
                context_epoch=self.epoch,
                resident_generation=self.gen,
                chat_id='default',
                claude_session_id=SESSION,
                cwd=self.cwd,
                source='other_source',
                scan_offset=0,
                claude_home=self.claude_home,
                db_path=self.db,
            )
        # Same session on another generation → conflict
        with self.assertRaises(SessionRegistryConflict):
            register_context_claude_session(
                context_id=self.context_id,
                context_epoch=self.epoch,
                resident_generation=self.gen + 1,
                chat_id='default',
                claude_session_id=SESSION,
                cwd=self.cwd,
                source='test_cold',
                scan_offset=0,
                claude_home=self.claude_home,
                db_path=self.db,
            )

    def test_cas_offset_conflict(self) -> None:
        self._register(scan_offset=10)
        with self.assertRaises(SessionRegistryConflict) as ctx:
            cas_advance_scan_offset(
                context_id=self.context_id,
                resident_generation=self.gen,
                expected_offset=0,
                new_offset=20,
                db_path=self.db,
            )
        self.assertEqual(ctx.exception.error_code, 'scan_offset_cas_conflict')
        row = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(row['scan_offset']), 10)

    def test_truncated_file_fail_closed(self) -> None:
        self._register(scan_offset=100)
        path = self._transcript_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * 40)  # smaller than registered offset
        user_id = _insert_msg(self.db, 'hayana', 'x')
        asst_id = _insert_msg(self.db, 'fyodor', 'y')
        _bind_msg(self.db, user_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='user')
        _bind_msg(self.db, asst_id, context_id=self.context_id, epoch=self.epoch, gen=self.gen, role='assistant')
        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=100, observed_end_offset=100,
            ),
            db_path=self.db,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'transcript_truncated')
        row = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(row['scan_offset']), 100)


if __name__ == '__main__':
    unittest.main()
