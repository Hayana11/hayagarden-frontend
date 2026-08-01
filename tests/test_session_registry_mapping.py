"""Session Registry + message-event Mapping — flag-off data layer tests.

Uses only temporary SQLite + temporary JSONL. Never touches production DBs.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_FORBIDDEN = (
    '/opt/frontend/memories.db',
    str(ROOT / 'memories.db'),
)

from chat import daily_context as dc
from chat.claude_event_mapping import (
    MappingConflict,
    MappingPassRequest,
    _insert_mapping_row,
    get_user_canonical_by_event_uuid,
    run_mapping_pass,
)
from chat.claude_transcript_reader import (
    ReaderErrorCode,
    TranscriptReaderError,
    read_transcript,
    read_transcript_range,
)
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_READY,
    SessionRegistryConflict,
    SessionRegistryError,
    cas_advance_scan_offset,
    get_context_claude_session,
    mark_scan_blocked,
    register_context_claude_session,
)

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript'
SESSION = '11111111-2222-3333-4444-555555555555'
SESSION_OTHER = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


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


def _line(
    uuid: str,
    typ: str,
    *,
    session: str,
    parent: Optional[str],
    content: Any,
    omit_session: bool = False,
) -> str:
    obj: dict[str, Any] = {
        'type': typ,
        'uuid': uuid,
        'parentUuid': parent,
        'cwd': '/tmp/synth',
        'message': {'role': 'user' if typ == 'user' else 'assistant', 'content': content},
    }
    if not omit_session:
        obj['sessionId'] = session
    return json.dumps(obj, ensure_ascii=False)


class SessionRegistryMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.db, self.claude_home, self.cwd = _tmp_workspace()
        _init_db(self.db)
        self.ctx = dc.get_or_create_daily_context(
            chat_id='default',
            local_day='2026-07-30',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 30, 12, 0, 0),
        )
        self.context_id = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        self.gen = int(self.ctx['resident_generation'])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, *, scan_offset: int = 0, source: str = 'test_cold', **kwargs):
        return register_context_claude_session(
            context_id=kwargs.get('context_id', self.context_id),
            context_epoch=kwargs.get('context_epoch', self.epoch),
            resident_generation=kwargs.get('resident_generation', self.gen),
            chat_id=kwargs.get('chat_id', 'default'),
            claude_session_id=kwargs.get('claude_session_id', SESSION),
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

    def _seed_pair(self, user_text: str = 'u', asst_text: str = 'a'):
        user_id = _insert_msg(self.db, 'hayana', user_text)
        asst_id = _insert_msg(self.db, 'fyodor', asst_text)
        _bind_msg(
            self.db, user_id, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
        _bind_msg(
            self.db, asst_id, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='assistant',
        )
        return user_id, asst_id

    # ---- 1) core happy path ----
    def test_core_register_map_canonical_query(self) -> None:
        reg = self._register(scan_offset=0)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), 0)

        user_id, asst_id = self._seed_pair('前端用户原文-alpha', '助手回复-alpha')
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u-aaaa-0001', 'user', session=SESSION, parent=None, content='JSONL候选应被忽略'),
            _line(
                'a-aaaa-0001', 'assistant', session=SESSION, parent='u-aaaa-0001',
                content=[{'type': 'text', 'text': 'asst'}],
            ),
        ])
        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertTrue(result.ok, result.error_code)
        canonical = get_user_canonical_by_event_uuid(['u-aaaa-0001'], db_path=self.db)
        self.assertEqual(canonical, {'u-aaaa-0001': '前端用户原文-alpha'})

    def test_mapping_offset_zero_ignores_known_uuidless_metadata(self) -> None:
        """Cold start (scan_offset=0) must share Reader metadata ignore semantics."""
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair('前端用户原文-meta', '助手回复-meta')
        path = self._transcript_path()
        end = _write_jsonl(path, [
            json.dumps(
                {'type': 'file-history-snapshot', 'snapshot': {'trackedFileBackups': {}}},
                ensure_ascii=False,
            ),
            json.dumps({'type': 'queue-operation', 'operation': 'dequeue'}, ensure_ascii=False),
            _line('u-meta-0001', 'user', session=SESSION, parent=None, content='ignored-jsonl'),
            _line(
                'a-meta-0001', 'assistant', session=SESSION, parent='u-meta-0001',
                content=[{'type': 'text', 'text': 'asst'}],
            ),
        ])
        # Formal range reader from offset 0 must not raise MISSING_UUID.
        graph = read_transcript_range(path, 0, end)
        self.assertEqual(len(graph.candidate_rounds), 1)
        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertTrue(result.ok, result.error_code)
        canonical = get_user_canonical_by_event_uuid(['u-meta-0001'], db_path=self.db)
        self.assertEqual(canonical, {'u-meta-0001': '前端用户原文-meta'})

    # ---- 2) complex: tool 1:N + restart resume + idempotent ----
    def test_complex_tool_multimap_restart_resume_replay(self) -> None:
        self._register(scan_offset=0)
        user1, asst1 = self._seed_pair('请读文件', '读完了')
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
        roles = {
            r[0]: r[1]
            for r in conn.execute(
                'SELECT event_uuid, role FROM chat_message_claude_events '
                'WHERE message_id=?',
                (asst1,),
            )
        }
        self.assertEqual(roles['t1'], 'tool_use')
        self.assertEqual(roles['r1'], 'tool_result_user')
        self.assertEqual(roles['a1'], 'assistant')
        count_before = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events',
        ).fetchone()[0]
        conn.close()

        dc._SCHEMA_READY.discard(os.path.abspath(self.db))
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), end1)

        user2, asst2 = self._seed_pair('第二轮', '收到')
        turn2 = [
            _line('u2', 'user', session=SESSION, parent='a1', content='第二轮'),
            _line(
                'a2', 'assistant', session=SESSION, parent='u2',
                content=[{'type': 'text', 'text': '收到'}],
            ),
        ]
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
        # Stale BLOCKED must not overwrite READY/advanced offset
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), end2)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)

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
            count_before + 2,
        )
        conn.close()

    # ---- registry rejects fake window identity ----
    def test_registry_rejects_missing_or_mismatched_daily_context(self) -> None:
        with self.assertRaises(SessionRegistryError) as ctx:
            self._register(context_id=999999)
        self.assertEqual(ctx.exception.error_code, 'daily_context_missing')

        with self.assertRaises(SessionRegistryConflict) as ctx:
            self._register(chat_id='other-chat')
        self.assertEqual(ctx.exception.error_code, 'daily_context_mismatch')

        with self.assertRaises(SessionRegistryConflict) as ctx:
            self._register(context_epoch=self.epoch + 7)
        self.assertEqual(ctx.exception.error_code, 'daily_context_mismatch')

        with self.assertRaises(SessionRegistryConflict) as ctx:
            self._register(resident_generation=self.gen + 3)
        self.assertEqual(ctx.exception.error_code, 'daily_context_mismatch')

    def test_registry_idempotent_and_session_conflict(self) -> None:
        a = self._register(scan_offset=0)
        b = self._register(scan_offset=0)
        self.assertEqual(a['claude_session_id'], b['claude_session_id'])
        with self.assertRaises(SessionRegistryConflict):
            self._register(source='other_source')
        # bump generation on real daily_contexts then bind same session → conflict
        dc.respawn_daily_resident(self.context_id, db_path=self.db)
        refreshed = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        with self.assertRaises(SessionRegistryConflict) as ctx:
            register_context_claude_session(
                context_id=self.context_id,
                context_epoch=int(refreshed['context_epoch']),
                resident_generation=int(refreshed['resident_generation']),
                chat_id='default',
                claude_session_id=SESSION,
                cwd=self.cwd,
                source='test_cold',
                scan_offset=0,
                claude_home=self.claude_home,
                db_path=self.db,
            )
        self.assertEqual(ctx.exception.error_code, 'session_bound_elsewhere')

    # ---- mapping session / role rejects ----
    def test_mapping_rejects_session_mismatch_missing_and_multiple(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair()
        path = self._transcript_path()

        end = _write_jsonl(path, [
            _line('u1', 'user', session=SESSION_OTHER, parent=None, content='x'),
            _line(
                'a1', 'assistant', session=SESSION_OTHER, parent='u1',
                content=[{'type': 'text', 'text': 'y'}],
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
        self.assertEqual(bad.error_code, 'event_session_mismatch')

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE context_claude_sessions SET scan_status='READY', scan_error_code=NULL",
        )
        conn.commit()
        conn.close()

        end = _write_jsonl(path, [
            _line('u2', 'user', session=SESSION, parent=None, content='x', omit_session=True),
            _line(
                'a2', 'assistant', session=SESSION, parent='u2',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])
        bad2 = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(bad2.ok)
        self.assertEqual(bad2.error_code, 'event_session_missing')

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE context_claude_sessions SET scan_status='READY', scan_error_code=NULL",
        )
        conn.commit()
        conn.close()

        end = _write_jsonl(path, [
            _line('u3', 'user', session=SESSION, parent=None, content='x'),
            _line(
                'a3', 'assistant', session=SESSION_OTHER, parent='u3',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])
        bad3 = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(bad3.ok)
        self.assertEqual(bad3.error_code, 'multiple_event_sessions')

    def test_mapping_rejects_swapped_or_wrong_roles(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair()
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u1', 'user', session=SESSION, parent=None, content='x'),
            _line(
                'a1', 'assistant', session=SESSION, parent='u1',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])
        # Swap IDs: user_message_id points at assistant row
        swapped = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=asst_id, assistant_message_id=user_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(swapped.ok)
        self.assertEqual(swapped.error_code, 'message_role_mismatch')
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        self.assertEqual(
            int(conn.execute('SELECT scan_offset FROM context_claude_sessions').fetchone()[0]),
            0,
        )
        conn.close()

    # ---- incomplete rounds ----
    def test_incomplete_round_user_only_and_tool_without_final_text(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair()
        path = self._transcript_path()

        end = _write_jsonl(path, [
            _line('u-only', 'user', session=SESSION, parent=None, content='only'),
        ])
        r = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, 'incomplete_round_no_assistant')
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), 0)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_BLOCKED)

        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE context_claude_sessions SET scan_status='READY', scan_error_code=NULL",
        )
        conn.commit()
        conn.close()

        end = _write_jsonl(path, [
            _line('u-t', 'user', session=SESSION, parent=None, content='tool'),
            _line(
                't-only', 'assistant', session=SESSION, parent='u-t',
                content=[{'type': 'tool_use', 'id': 'toolu_x', 'name': 'Read', 'input': {}}],
            ),
        ])
        r2 = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=user_id, assistant_message_id=asst_id,
                expected_start_offset=0, observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertFalse(r2.ok)
        self.assertEqual(r2.error_code, 'incomplete_round_no_terminal_text')
        conn = sqlite3.connect(self.db)
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM chat_message_claude_events').fetchone()[0],
            0,
        )
        self.assertEqual(
            int(conn.execute('SELECT scan_offset FROM context_claude_sessions').fetchone()[0]),
            0,
        )
        conn.close()

    # ---- stale BLOCKED CAS ----
    def test_stale_blocked_cas_does_not_overwrite_ready(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair()
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u1', 'user', session=SESSION, parent=None, content='x'),
            _line(
                'a1', 'assistant', session=SESSION, parent='u1',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])
        # Mapper B succeeds first
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
        # Mapper A tries stale BLOCKED at old offset
        with self.assertRaises(SessionRegistryConflict) as ctx:
            mark_scan_blocked(
                context_id=self.context_id,
                resident_generation=self.gen,
                error_code='stale_failure',
                expected_offset=0,
                db_path=self.db,
            )
        self.assertEqual(ctx.exception.error_code, 'scan_offset_cas_conflict')
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), end)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertIsNone(reg['scan_error_code'])

    # ---- reader range start mid-line ----
    def test_reader_range_start_mid_line_rejected(self) -> None:
        path = Path(self.tmp) / 'mid.jsonl'
        raw = (
            _line('u1', 'user', session=SESSION, parent=None, content='hello') + '\n'
            + _line(
                'a1', 'assistant', session=SESSION, parent='u1',
                content=[{'type': 'text', 'text': 'hi'}],
            ) + '\n'
        ).encode('utf-8')
        path.write_bytes(raw)
        # full reader still works (line streaming)
        graph = read_transcript(path)
        self.assertEqual(len(graph.events), 2)
        # start inside first line
        with self.assertRaises(TranscriptReaderError) as ctx:
            read_transcript_range(path, 3, len(raw))
        self.assertEqual(ctx.exception.code, ReaderErrorCode.RANGE_MID_LINE)
        # fixture regression via full reader
        g2 = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        self.assertEqual(len(g2.candidate_rounds), 2)

    # ---- retained failure paths ----
    def test_fail_closed_ambiguous_candidates(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair()
        path = self._transcript_path()
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
        self.assertEqual(int(bad.registry['scan_offset']), 0)
        self.assertEqual(bad.registry['scan_status'], SCAN_STATUS_BLOCKED)

    def test_fail_closed_epoch_mismatch_and_event_conflict(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair('canon', 'a')
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u-x', 'user', session=SESSION, parent=None, content='x'),
            _line(
                'a-x', 'assistant', session=SESSION, parent='u-x',
                content=[{'type': 'text', 'text': 'y'}],
            ),
        ])
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

        user2 = _insert_msg(self.db, 'hayana', 'other')
        _bind_msg(
            self.db, user2, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
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

    def test_truncated_file_fail_closed(self) -> None:
        self._register(scan_offset=100)
        path = self._transcript_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * 40)
        user_id, asst_id = self._seed_pair()
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
