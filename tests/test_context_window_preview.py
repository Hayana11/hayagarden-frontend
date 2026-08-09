"""Focused Preview / dry-run tests — temp SQLite + temp JSONL only."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import daily_context as dc
from chat.claude_event_mapping import MappingPassRequest, run_mapping_pass
from chat.claude_transcript_model import ThinkingPolicy
from chat.context_window_preview import (
    PREVIEW_STATUS_BLOCKED,
    PREVIEW_STATUS_NATIVE_COLD,
    PREVIEW_STATUS_READY,
    _open_preview_db_readonly,
    preview_context_window,
)
from chat.claude_transcript_reader import read_transcript_range as _real_read_range
from chat.session_registry import (
    SCAN_STATUS_READY,
    get_context_claude_session,
    register_context_claude_session,
)
from tools.cc_jsonl_usage import session_jsonl_path

SESSION_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
SESSION_B = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
NOW = datetime.datetime(2026, 7, 31, 12, 0, 0)


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='cw-preview-')
    db = os.path.join(tmp, 'test.db')
    home = os.path.join(tmp, '.claude')
    cwd = os.path.join(tmp, 'proj')
    os.makedirs(cwd, exist_ok=True)
    return tmp, db, home, cwd


def _init_db(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            source_kind TEXT NOT NULL DEFAULT 'chat',
            image_url TEXT DEFAULT '',
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


def _insert_msg_with_image(
    db: str, author: str, content: str, image_url: str,
) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, image_url) VALUES (?,?,?)',
        (author, content, image_url),
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


def _line(
    uid: str,
    typ: str,
    *,
    session: str,
    parent: Optional[str],
    content: Any,
    cwd: str = '/tmp/preview-cwd',
    extra: Optional[dict[str, Any]] = None,
) -> str:
    obj: dict[str, Any] = {
        'type': typ,
        'uuid': uid,
        'parentUuid': parent,
        'cwd': cwd,
        'sessionId': session,
        'message': {
            'role': 'user' if typ == 'user' else 'assistant',
            'content': content,
        },
    }
    if extra:
        obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


def _write_jsonl(path: Path, lines: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    path.write_bytes(raw)
    return len(raw)


def _append_jsonl(path: Path, lines: list[str]) -> int:
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    with path.open('ab') as fh:
        fh.write(raw)
    return path.stat().st_size


def _db_fingerprint(db: str) -> str:
    h = hashlib.sha256()
    conn = sqlite3.connect(db)
    try:
        for table in (
            'daily_contexts',
            'daily_message_contexts',
            'context_claude_sessions',
            'chat_message_claude_events',
            'context_switch_intents',
            'chat_messages',
        ):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            rows = conn.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
            h.update(table.encode())
            h.update(repr(rows).encode())
    finally:
        conn.close()
    return h.hexdigest()


class ContextWindowPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.db, self.home, self.cwd = _tmp_workspace()
        _init_db(self.db)
        self._home_patch = mock.patch.dict(os.environ, {'HOME': self.home})
        self._home_patch.start()
        self.ctx = dc.get_or_create_daily_context(
            chat_id='default',
            local_day='2026-07-31',
            db_path=self.db,
            now=NOW,
        )
        self.context_id = int(self.ctx['id'])
        self.epoch = int(self.ctx['context_epoch'])
        self.gen = int(self.ctx['resident_generation'])
        self.preview_id = str(uuid.uuid4())

    def tearDown(self) -> None:
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jsonl_path(self, session: str = SESSION_A) -> Path:
        path = session_jsonl_path(self.cwd, session)
        assert path is not None
        return Path(path)

    def _register(self, *, session: str, scan_offset: int, gen: Optional[int] = None):
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=int(gen if gen is not None else self.gen),
            chat_id='default',
            claude_session_id=session,
            cwd=self.cwd,
            source='test_preview',
            scan_offset=scan_offset,
            db_path=self.db,
        )

    def _map_turn(
        self, *, user_id: int, asst_id: int, start: int, end: int, gen: Optional[int] = None,
    ):
        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id,
                context_epoch=self.epoch,
                resident_generation=int(gen if gen is not None else self.gen),
                chat_id='default',
                user_message_id=user_id,
                assistant_message_id=asst_id,
                expected_start_offset=start,
                observed_end_offset=end,
            ),
            db_path=self.db,
        )
        self.assertTrue(result.ok, result.error_code)
        return result

    def test_native_cold_count_zero(self):
        """1) count=0 → NATIVE_COLD; fresh recheck conn; no Registry/JSONL/Mapping."""
        before = _db_fingerprint(self.db)
        opened: list[Any] = []
        real_open = _open_preview_db_readonly

        def _tracking_open(db_path: str):
            conn = real_open(db_path)
            opened.append(conn)
            return conn

        with mock.patch(
            'chat.context_window_preview._open_preview_db_readonly',
            side_effect=_tracking_open,
        ):
            out = preview_context_window(
                source_context_id=self.context_id,
                source_context_epoch=self.epoch,
                count=0,
                preview_id=self.preview_id,
                thinking_policy=ThinkingPolicy.DROP,
                chat_id='default',
                db_path=self.db,
                now=NOW,
            )
        self.assertTrue(out['ok'])
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_NATIVE_COLD)
        self.assertEqual(out['candidate']['event_count'], 0)
        self.assertEqual(out['candidate']['round_count'], 0)
        self.assertEqual(out['candidate']['proof_kind'], 'native_cold_contract')
        self.assertEqual(
            out['candidate']['output_sha256'],
            hashlib.sha256(b'').hexdigest(),
        )
        self.assertFalse(out['candidate_session_reserved'])
        self.assertIsNone(out['source']['claude_session_id'])
        self.assertEqual(out['selection']['selected_round_count'], 0)
        self.assertNotIn('transcript_path', json.dumps(out))
        self.assertNotIn(self.cwd, json.dumps(out))
        self.assertNotIn('error_detail', out)
        self.assertEqual(_db_fingerprint(self.db), before)
        # Fresh recheck: two distinct readonly connections
        self.assertEqual(len(opened), 2)
        self.assertIsNot(opened[0], opened[1])
        # No JSONL created / no Registry required
        self.assertFalse(any(Path(self.home).rglob('*.jsonl')))
        self.assertIsNone(
            get_context_claude_session(self.context_id, self.gen, db_path=self.db),
        )

    def test_plain_ready_respects_scan_offset(self):
        """2) READY for two rounds; decoy past scan_offset must not be read."""
        path = self._jsonl_path()
        turn1 = [
            _line('u1', 'user', session=SESSION_A, parent=None, content='第一轮用户'),
            _line(
                'a1', 'assistant', session=SESSION_A, parent='u1',
                content=[{'type': 'text', 'text': '第一轮助手'}],
            ),
        ]
        turn2 = [
            _line('u2', 'user', session=SESSION_A, parent='a1', content='第二轮用户'),
            _line(
                'a2', 'assistant', session=SESSION_A, parent='u2',
                content=[{'type': 'text', 'text': '第二轮助手'}],
            ),
        ]
        end1 = _write_jsonl(path, turn1)
        end2 = _append_jsonl(path, turn2)
        self._register(session=SESSION_A, scan_offset=0)

        u1 = _insert_msg(self.db, 'hayana', '第一轮用户')
        a1 = _insert_msg(self.db, 'fyodor', '第一轮助手')
        u2 = _insert_msg(self.db, 'hayana', '第二轮用户')
        a2 = _insert_msg(self.db, 'fyodor', '第二轮助手')
        for mid, role in ((u1, 'user'), (a1, 'assistant'), (u2, 'user'), (a2, 'assistant')):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )
        self._map_turn(user_id=u1, asst_id=a1, start=0, end=end1)
        self._map_turn(user_id=u2, asst_id=a2, start=end1, end=end2)

        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), end2)

        # Decoy past scan_offset — must not affect Preview
        before_bytes = path.read_bytes()
        _append_jsonl(path, [
            _line('u-decoy', 'user', session=SESSION_A, parent='a2', content='诱饵不可见'),
            _line(
                'a-decoy', 'assistant', session=SESSION_A, parent='u-decoy',
                content=[{'type': 'text', 'text': '诱饵助手'}],
            ),
        ])
        after_append = path.read_bytes()
        self.assertGreater(len(after_append), len(before_bytes))

        before_db = _db_fingerprint(self.db)
        # Freeze file content hash of the scan range portion
        scan_prefix = after_append[:end2]

        out = preview_context_window(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=2,
            preview_id=self.preview_id,
            thinking_policy=ThinkingPolicy.DROP,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_READY)
        self.assertEqual(out['selection']['selected_round_count'], 2)
        self.assertEqual(out['selection']['selected_message_ids'], [u1, a1, u2, a2])
        self.assertEqual(len(out['selection']['selected_rounds']), 2)
        self.assertTrue(out['validation']['ok'])
        self.assertGreater(out['candidate']['event_count'], 0)
        self.assertEqual(out['candidate']['round_count'], 2)
        self.assertFalse(out['candidate_session_reserved'])
        blob = json.dumps(out)
        self.assertNotIn('transcript_path', blob)
        self.assertNotIn(str(path), blob)
        self.assertNotIn('/tmp/preview-cwd', blob)
        self.assertNotIn('events', out['candidate'])
        self.assertNotIn('诱饵', blob)

        # DB + scan-range bytes unchanged (decoy may remain after offset)
        self.assertEqual(_db_fingerprint(self.db), before_db)
        self.assertEqual(path.read_bytes()[:end2], scan_prefix)

    def test_ready_preview_skips_user_continuation_in_vision_round(self):
        """Parent-chained vision user maps + transforms to DB canonical only."""
        path = self._jsonl_path()
        turn = [
            _line(
                'u-vision', 'user', session=SESSION_A, parent=None,
                content='x' * 5000,
            ),
            _line(
                'u-text', 'user', session=SESSION_A, parent='u-vision',
                content='short-text',
            ),
            _line(
                'a-part', 'assistant', session=SESSION_A, parent='u-text',
                content=[{'type': 'text', 'text': 'part'}],
            ),
            _line(
                'a-final', 'assistant', session=SESSION_A, parent='a-part',
                content=[{'type': 'text', 'text': 'final'}],
            ),
        ]
        end = _write_jsonl(path, turn)
        self._register(session=SESSION_A, scan_offset=0)

        user_id = _insert_msg(self.db, 'hayana', 'db-vision-user')
        asst_id = _insert_msg(self.db, 'fyodor', 'db-vision-asst')
        for mid, role in ((user_id, 'user'), (asst_id, 'assistant')):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )
        self._map_turn(user_id=user_id, asst_id=asst_id, start=0, end=end)

        out = preview_context_window(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=1,
            preview_id=self.preview_id,
            thinking_policy=ThinkingPolicy.DROP,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_READY, out)
        self.assertEqual(out['selection']['selected_round_count'], 1)
        self.assertEqual(out['selection']['selected_message_ids'], [user_id, asst_id])
        self.assertTrue(out['validation']['ok'])
        blob = json.dumps(out)
        self.assertNotIn('short-text', blob)
        self.assertNotIn('u-text', blob)

    def test_ready_preview_vision_round_preserves_db_image_block(self):
        """DB text+image_url must survive Preview→Transform for vision-split rounds."""
        from chat.cc_vision_bridge import resolve_image_bytes
        from chat.claude_transcript_transform import transform_transcript as real_transform
        from tests.test_context_window_forge_switch import _make_vision_png

        upload_dir = Path(self.tmp) / 'uploads'
        upload_dir.mkdir(parents=True)
        png_name = 'preview_vision.png'
        _make_vision_png(upload_dir / png_name, 'pixel-marker')
        image_ref = '/static/uploads/%s' % png_name

        path = self._jsonl_path()
        turn = [
            _line(
                'u-vision', 'user', session=SESSION_A, parent=None,
                content='x' * 5000,
            ),
            _line(
                'u-text', 'user', session=SESSION_A, parent='u-vision',
                content='native-only-prompt',
            ),
            _line(
                'a-final', 'assistant', session=SESSION_A, parent='u-text',
                content=[{'type': 'text', 'text': 'reply'}],
            ),
        ]
        end = _write_jsonl(path, turn)
        self._register(session=SESSION_A, scan_offset=0)

        user_id = _insert_msg_with_image(
            self.db, 'hayana', '看看这个', image_ref,
        )
        asst_id = _insert_msg(self.db, 'fyodor', 'reply')
        for mid, role in ((user_id, 'user'), (asst_id, 'assistant')):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )
        self._map_turn(user_id=user_id, asst_id=asst_id, start=0, end=end)

        captured: dict[str, Any] = {}

        def _spy_transform(graph, request):
            captured['canonical'] = dict(request.user_canonical_by_event_uuid)
            result = real_transform(graph, request)
            captured['events'] = result.events
            return result

        with mock.patch(
            'chat.cc_vision_bridge.resolve_image_bytes',
            side_effect=lambda ref, **kw: resolve_image_bytes(
                ref,
                upload_dir=str(upload_dir),
                attach_dir=str(upload_dir),
            ),
        ), mock.patch(
            'chat.context_window_preview.transform_transcript',
            side_effect=_spy_transform,
        ):
            out = preview_context_window(
                source_context_id=self.context_id,
                source_context_epoch=self.epoch,
                count=1,
                preview_id=self.preview_id,
                thinking_policy=ThinkingPolicy.DROP,
                chat_id='default',
                db_path=self.db,
                now=NOW,
            )

        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_READY, out)
        canonical = captured['canonical']['u-vision']
        self.assertIsInstance(canonical, list)
        img_blocks = [b for b in canonical if b.get('type') == 'image']
        text_blocks = [b for b in canonical if b.get('type') == 'text']
        self.assertEqual(len(img_blocks), 1)
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]['text'], '看看这个')
        forged_user = captured['events'][0]['message']['content']
        self.assertIsInstance(forged_user, list)
        self.assertEqual(
            [b.get('type') for b in forged_user],
            ['text', 'image'],
        )
        self.assertNotIn('native-only-prompt', json.dumps(captured['events']))
        self.assertEqual(
            captured['events'][1]['parentUuid'],
            captured['events'][0]['uuid'],
        )

    def test_complex_ready_thinking_tool_sidechain(self):
        """3) Complex READY: sidechain excluded; keep thinking + tool pair."""
        from chat.claude_transcript_reader import read_transcript_range

        path = self._jsonl_path()
        # Round 1: sidechain-impacted (must be in canonical map to count as sidechain drop)
        # Round 2: signed thinking + tool_use/tool_result + text
        lines = [
            _line('s-u1', 'user', session=SESSION_A, parent=None, content='受sidechain影响'),
            _line(
                's-a1', 'assistant', session=SESSION_A, parent='s-u1',
                content=[{'type': 'text', 'text': '子代理'}],
                extra={'isSidechain': True},
            ),
            _line(
                's-u2', 'user', session=SESSION_A, parent='s-a1', content='子输入',
                extra={'isSidechain': True},
            ),
            _line('c-u1', 'user', session=SESSION_A, parent='s-u1', content='请读文件并思考'),
            _line(
                'c-t1', 'assistant', session=SESSION_A, parent='c-u1',
                content=[{
                    'type': 'thinking',
                    'thinking': '先读再答',
                    'signature': 'sig_preview_keep',
                }, {
                    'type': 'tool_use',
                    'id': 'toolu_preview_001',
                    'name': 'Read',
                    'input': {'file_path': '/tmp/x.txt'},
                }],
            ),
            _line(
                'c-r1', 'user', session=SESSION_A, parent='c-t1',
                content=[{
                    'type': 'tool_result',
                    'tool_use_id': 'toolu_preview_001',
                    'content': 'ok',
                    'is_error': False,
                }],
                extra={'sourceToolUseID': 'toolu_preview_001'},
            ),
            _line(
                'c-a1', 'assistant', session=SESSION_A, parent='c-r1',
                content=[{'type': 'text', 'text': '读完了'}],
            ),
        ]
        end = _write_jsonl(path, lines)
        self._register(session=SESSION_A, scan_offset=end)

        # App has both rounds; count=1 selects only the last (complex) app round.
        u_side = _insert_msg(self.db, 'hayana', '受sidechain影响')
        a_side = _insert_msg(self.db, 'fyodor', '受影响助手占位')
        u = _insert_msg(self.db, 'hayana', '请读文件并思考')
        a = _insert_msg(self.db, 'fyodor', '读完了')
        for mid, role in (
            (u_side, 'user'), (a_side, 'assistant'), (u, 'user'), (a, 'assistant'),
        ):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )

        graph = read_transcript_range(path, 0, end)
        now_s = dc._now_local_str()
        conn = sqlite3.connect(self.db)
        # Sidechain-impacted mainchain user must be canonical-confirmed so
        # Transform counts it under sidechain_round_count (not unconfirmed).
        for uid, mid, role in (
            ('s-u1', u_side, 'user'),
            ('c-u1', u, 'user'),
            ('c-t1', a, 'tool_use'),
            ('c-r1', a, 'tool_result_user'),
            ('c-a1', a, 'assistant'),
        ):
            evt = graph.by_uuid[uid]
            conn.execute(
                '''INSERT INTO chat_message_claude_events (
                    event_uuid, message_id, role, claude_session_id,
                    context_id, context_epoch, resident_generation,
                    jsonl_byte_offset, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)''',
                (
                    uid, mid, role, SESSION_A,
                    self.context_id, self.epoch, self.gen,
                    int(evt.byte_offset), now_s,
                ),
            )
        conn.commit()
        conn.close()

        before_db = _db_fingerprint(self.db)
        before_bytes = path.read_bytes()

        out = preview_context_window(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=1,
            preview_id=self.preview_id,
            thinking_policy=ThinkingPolicy.KEEP,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_READY, out)
        self.assertEqual(out['selection']['selected_round_count'], 1)
        self.assertEqual(out['selection']['selected_message_ids'], [u, a])
        self.assertGreaterEqual(out['dropped']['sidechain_round_count'], 1)
        self.assertTrue(out['validation']['ok'], out['validation'])
        # Transformed candidate includes thinking under KEEP
        # (not exposed as events; validator ok + event_count>1 proves structure)
        self.assertGreaterEqual(out['candidate']['event_count'], 3)
        self.assertEqual(_db_fingerprint(self.db), before_db)
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_span_sessions_blocked(self):
        """4) Selected rounds spanning sessions → BLOCKED, no writes."""
        # Two generations with different sessions mapped into one app selection.
        path_a = self._jsonl_path(SESSION_A)
        t1 = [
            _line('u-a', 'user', session=SESSION_A, parent=None, content='session-a'),
            _line(
                'a-a', 'assistant', session=SESSION_A, parent='u-a',
                content=[{'type': 'text', 'text': 'reply-a'}],
            ),
        ]
        end_a = _write_jsonl(path_a, t1)
        self._register(session=SESSION_A, scan_offset=0)
        u1 = _insert_msg(self.db, 'hayana', 'session-a')
        a1 = _insert_msg(self.db, 'fyodor', 'reply-a')
        _bind_msg(
            self.db, u1, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
        _bind_msg(
            self.db, a1, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='assistant',
        )
        self._map_turn(user_id=u1, asst_id=a1, start=0, end=end_a)

        # Bump generation and register a second session; bind second round to new gen
        dc.respawn_daily_resident(self.context_id, db_path=self.db)
        ctx2 = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        gen2 = int(ctx2['resident_generation'])
        self.assertGreater(gen2, self.gen)

        path_b = self._jsonl_path(SESSION_B)
        t2 = [
            _line('u-b', 'user', session=SESSION_B, parent=None, content='session-b'),
            _line(
                'a-b', 'assistant', session=SESSION_B, parent='u-b',
                content=[{'type': 'text', 'text': 'reply-b'}],
            ),
        ]
        end_b = _write_jsonl(path_b, t2)
        self._register(session=SESSION_B, scan_offset=0, gen=gen2)
        u2 = _insert_msg(self.db, 'hayana', 'session-b')
        a2 = _insert_msg(self.db, 'fyodor', 'reply-b')
        # Bind second pair under NEW gen but SAME context/epoch (selected together)
        _bind_msg(
            self.db, u2, context_id=self.context_id,
            epoch=self.epoch, gen=gen2, role='user',
        )
        _bind_msg(
            self.db, a2, context_id=self.context_id,
            epoch=self.epoch, gen=gen2, role='assistant',
        )
        self._map_turn(user_id=u2, asst_id=a2, start=0, end=end_b, gen=gen2)

        # Formal collect uses context_id+epoch only — both rounds selected with count=2.
        # Current open generation is gen2; Registry for gen2 is SESSION_B.
        # Mapping rows for selected messages span gen1 SESSION_A and gen2 SESSION_B.
        before_db = _db_fingerprint(self.db)
        before_a = path_a.read_bytes()
        before_b = path_b.read_bytes()

        out = preview_context_window(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=2,
            preview_id=self.preview_id,
            thinking_policy=ThinkingPolicy.DROP,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertTrue(out['ok'])
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_BLOCKED)
        self.assertEqual(out['error_code'], 'PREVIEW_SELECTED_ROUNDS_SPAN_SESSIONS')
        self.assertEqual(out['selection']['selected_round_count'], 2)
        self.assertEqual(_db_fingerprint(self.db), before_db)
        self.assertEqual(path_a.read_bytes(), before_a)
        self.assertEqual(path_b.read_bytes(), before_b)
        self.assertFalse(list(Path(self.tmp).rglob('*candidate*')))

    def test_readonly_connection_rejects_writes(self):
        conn = _open_preview_db_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.Error):
                conn.execute(
                    "UPDATE daily_contexts SET updated_at=updated_at WHERE id=?",
                    (self.context_id,),
                )
        finally:
            try:
                conn.execute('ROLLBACK')
            except Exception:
                pass
            conn.close()

    def _plain_two_round_fixture(self):
        path = self._jsonl_path()
        turn1 = [
            _line('u1', 'user', session=SESSION_A, parent=None, content='第一轮用户'),
            _line(
                'a1', 'assistant', session=SESSION_A, parent='u1',
                content=[{'type': 'text', 'text': '第一轮助手'}],
            ),
        ]
        turn2 = [
            _line('u2', 'user', session=SESSION_A, parent='a1', content='第二轮用户'),
            _line(
                'a2', 'assistant', session=SESSION_A, parent='u2',
                content=[{'type': 'text', 'text': '第二轮助手'}],
            ),
        ]
        end1 = _write_jsonl(path, turn1)
        end2 = _append_jsonl(path, turn2)
        self._register(session=SESSION_A, scan_offset=0)
        u1 = _insert_msg(self.db, 'hayana', '第一轮用户')
        a1 = _insert_msg(self.db, 'fyodor', '第一轮助手')
        u2 = _insert_msg(self.db, 'hayana', '第二轮用户')
        a2 = _insert_msg(self.db, 'fyodor', '第二轮助手')
        for mid, role in ((u1, 'user'), (a1, 'assistant'), (u2, 'user'), (a2, 'assistant')):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )
        self._map_turn(user_id=u1, asst_id=a1, start=0, end=end1)
        self._map_turn(user_id=u2, asst_id=a2, start=end1, end=end2)
        return path, end2, (u1, a1, u2, a2)

    def test_prefix_mutation_blocked(self):
        """Prefix rewrite inside scan_offset → BLOCKED PREVIEW_SOURCE_CHANGED."""
        path, end2, _ids = self._plain_two_round_fixture()
        before_db = _db_fingerprint(self.db)
        original = path.read_bytes()

        def _mutate_after_read(p, start, end):
            graph = _real_read_range(p, start, end)
            raw = bytearray(path.read_bytes())
            # Flip one byte inside the confirmed prefix (not past scan_offset).
            idx = max(0, min(end2 - 1, 10))
            raw[idx] = (raw[idx] + 1) % 256
            path.write_bytes(bytes(raw))
            return graph

        try:
            with mock.patch(
                'chat.context_window_preview.read_transcript_range',
                side_effect=_mutate_after_read,
            ):
                out = preview_context_window(
                    source_context_id=self.context_id,
                    source_context_epoch=self.epoch,
                    count=2,
                    preview_id=self.preview_id,
                    thinking_policy=ThinkingPolicy.DROP,
                    chat_id='default',
                    db_path=self.db,
                    now=NOW,
                )
            self.assertTrue(out['ok'], out)
            self.assertEqual(out['preview_status'], PREVIEW_STATUS_BLOCKED, out)
            self.assertEqual(out['error_code'], 'PREVIEW_SOURCE_CHANGED')
            self.assertNotEqual(out.get('preview_status'), PREVIEW_STATUS_READY)
            self.assertEqual(_db_fingerprint(self.db), before_db)
        finally:
            path.write_bytes(original)

    def test_bad_scan_offset_blocked(self):
        """Non-integer Registry scan_offset → content BLOCKED, no JSONL read."""
        path, _end2, _ids = self._plain_two_round_fixture()
        before_db = _db_fingerprint(self.db)
        before_bytes = path.read_bytes()
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE context_claude_sessions SET scan_offset=? '
            'WHERE context_id=? AND resident_generation=?',
            ('not-an-int', self.context_id, self.gen),
        )
        conn.commit()
        conn.close()
        before_db = _db_fingerprint(self.db)

        with mock.patch(
            'chat.context_window_preview.read_transcript_range',
            side_effect=AssertionError('must not read JSONL'),
        ) as read_mock:
            out = preview_context_window(
                source_context_id=self.context_id,
                source_context_epoch=self.epoch,
                count=2,
                preview_id=self.preview_id,
                thinking_policy=ThinkingPolicy.DROP,
                chat_id='default',
                db_path=self.db,
                now=NOW,
            )
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_BLOCKED)
        self.assertEqual(out['error_code'], 'PREVIEW_REGISTRY_SCAN_OFFSET_INVALID')
        self.assertIsNone(out['source']['scan_offset'])
        self.assertEqual(out['validation']['errors'], [
            'PREVIEW_REGISTRY_SCAN_OFFSET_INVALID',
        ])
        self.assertNotIn('error_detail', out)
        read_mock.assert_not_called()
        self.assertEqual(_db_fingerprint(self.db), before_db)
        self.assertEqual(path.read_bytes(), before_bytes)

    def test_response_privacy_no_path_leak(self):
        """Filesystem / internal errors must not leak path or error_detail."""
        secret = '/secret-preview-path/do-not-leak.jsonl'
        self._register(session=SESSION_A, scan_offset=128)
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE context_claude_sessions SET transcript_path=?, scan_offset=? '
            'WHERE context_id=? AND resident_generation=?',
            (secret, 128, self.context_id, self.gen),
        )
        # Mark READY (register already READY when offset advanced via mapping;
        # here force READY with bogus path).
        conn.execute(
            "UPDATE context_claude_sessions SET scan_status=? "
            'WHERE context_id=? AND resident_generation=?',
            (SCAN_STATUS_READY, self.context_id, self.gen),
        )
        conn.commit()
        conn.close()

        # Need at least one selected round so Registry path is exercised.
        u = _insert_msg(self.db, 'hayana', '隐私探测')
        a = _insert_msg(self.db, 'fyodor', '隐私探测回复')
        _bind_msg(
            self.db, u, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
        _bind_msg(
            self.db, a, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='assistant',
        )

        out = preview_context_window(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=1,
            preview_id=self.preview_id,
            thinking_policy=ThinkingPolicy.DROP,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        blob = json.dumps(out)
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['preview_status'], PREVIEW_STATUS_BLOCKED)
        self.assertNotIn('/secret-preview-path', blob)
        self.assertNotIn('do-not-leak.jsonl', blob)
        self.assertNotIn(self.cwd, blob)
        self.assertNotIn('transcript_path', blob)
        self.assertNotIn('error_detail', out)

        # Route unknown-exception path must also stay opaque.
        from flask import Flask
        from context_window_routes import create_context_window_blueprint

        app = Flask(__name__)
        app.register_blueprint(create_context_window_blueprint(
            db_path=self.db, token_getter=lambda: 'tok',
        ))
        client = app.test_client()
        with mock.patch('context_window_routes.enabled', return_value=True), mock.patch(
            'context_window_routes.preview_context_window',
            side_effect=RuntimeError('/secret-preview-path/internal'),
        ):
            resp = client.post(
                '/api/context-window/preview',
                headers={'Authorization': 'Bearer tok'},
                json={
                    'source_context_id': self.context_id,
                    'source_context_epoch': self.epoch,
                    'count': 0,
                    'preview_id': self.preview_id,
                },
            )
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertEqual(body.get('code'), 'preview_internal_error')
        self.assertEqual(body.get('error'), 'preview failed')
        text = resp.get_data(as_text=True)
        self.assertNotIn('/secret-preview-path', text)
        self.assertNotIn('do-not-leak', text)
        self.assertNotIn('/secret-preview-path/internal', text)


if __name__ == '__main__':
    unittest.main()
