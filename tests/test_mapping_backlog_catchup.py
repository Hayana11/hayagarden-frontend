"""Step 10 Defect A/B — mapping backlog catch-up + stale prefix guard.

Three representative cases only. No new framework.
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat import daily_context as dc
from chat.capacity_swap_runtime import prepare_capacity_swap_for_plan
from chat.claude_event_mapping import (
    ERROR_CATCHUP_AUTH_MISMATCH,
    ERROR_MAPPING_LAG,
    MappingPassRequest,
    attempt_mapping_catchup_through,
    run_mapping_pass,
)
from chat.context_window_forge import CarryoverUnforgeableError, build_events_from_selected_messages
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SCAN_STATUS_READY,
    get_context_claude_session,
    register_context_claude_session,
)

SESSION = '11111111-2222-3333-4444-555555555555'


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='map-catchup-')
    db = os.path.join(tmp, 'test.db')
    claude_home = os.path.join(tmp, '.claude')
    cwd = os.path.join(tmp, 'proj')
    os.makedirs(cwd, exist_ok=True)
    return tmp, db, claude_home, cwd


def _init_db(db: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '',
            cache_info TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db)


def _insert_msg(db: str, author: str, content: str, *, cache_info: str = '') -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, cache_info) VALUES (?,?,?)',
        (author, content, cache_info),
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


def _append_jsonl(path: Path, lines: list[str]) -> int:
    extra = ''.join(line if line.endswith('\n') else line + '\n' for line in lines).encode('utf-8')
    with path.open('ab') as fh:
        fh.write(extra)
    return path.stat().st_size


def _line(
    uuid: str,
    typ: str,
    *,
    session: str,
    parent: Optional[str],
    content: Any,
) -> str:
    obj: dict[str, Any] = {
        'type': typ,
        'uuid': uuid,
        'parentUuid': parent,
        'cwd': '/tmp/synth',
        'sessionId': session,
        'message': {'role': 'user' if typ == 'user' else 'assistant', 'content': content},
    }
    return json.dumps(obj, ensure_ascii=False)


class MappingBacklogCatchupTests(unittest.TestCase):
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

    def _register(self, *, scan_offset: int = 0):
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            claude_session_id=SESSION,
            cwd=self.cwd,
            source='test',
            scan_offset=scan_offset,
            claude_home=self.claude_home,
            db_path=self.db,
        )

    def _transcript_path(self) -> Path:
        row = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        assert row is not None
        return Path(row['transcript_path'])

    def _seed_pair(self, user_text: str, asst_text: str, *, cache_info: str = ''):
        user_id = _insert_msg(self.db, 'hayana', user_text)
        asst_id = _insert_msg(self.db, 'fyodor', asst_text, cache_info=cache_info)
        _bind_msg(
            self.db, user_id, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
        _bind_msg(
            self.db, asst_id, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='assistant',
        )
        return user_id, asst_id

    def _mapped(self, message_id: int) -> bool:
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            'SELECT COUNT(*) FROM chat_message_claude_events WHERE message_id=?',
            (int(message_id),),
        ).fetchone()[0]
        conn.close()
        return int(n) > 0

    # ---- CASE 1: normal no-gap fast path unchanged ----
    def test_case1_normal_no_gap_fast_path(self) -> None:
        self._register(scan_offset=0)
        user_id, asst_id = self._seed_pair('hello', 'hi')
        path = self._transcript_path()
        end = _write_jsonl(path, [
            _line('u-a', 'user', session=SESSION, parent=None, content='hello'),
            _line(
                'a-a', 'assistant', session=SESSION, parent='u-a',
                content=[{'type': 'text', 'text': 'hi'}],
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
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), end)
        self.assertEqual(int(reg['last_mapped_message_id']), asst_id)
        self.assertTrue(self._mapped(user_id))
        self.assertTrue(self._mapped(asst_id))

    # ---- CASE 2: representative backlog catch-up + swap uses recent ----
    def test_case2_backlog_catchup_then_swap_includes_recent(self) -> None:
        self._register(scan_offset=0)
        path = self._transcript_path()

        # Round A — mapped
        ua, aa = self._seed_pair('round-A', 'reply-A')
        end_a = _write_jsonl(path, [
            _line('u-a', 'user', session=SESSION, parent=None, content='round-A'),
            _line(
                'a-a', 'assistant', session=SESSION, parent='u-a',
                content=[{'type': 'text', 'text': 'reply-A'}],
            ),
        ])
        ok_a = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=ua, assistant_message_id=aa,
                expected_start_offset=0, observed_end_offset=end_a,
            ),
            db_path=self.db,
        )
        self.assertTrue(ok_a.ok, ok_a.error_code)

        # Round B — complete in JSONL+DB but mapping missed
        ub, ab = self._seed_pair('round-B-bath', 'reply-B-shower')
        end_b = _append_jsonl(path, [
            _line('u-b', 'user', session=SESSION, parent='a-a', content='round-B-bath'),
            _line(
                'a-b', 'assistant', session=SESSION, parent='u-b',
                content=[{'type': 'text', 'text': 'reply-B-shower'}],
            ),
        ])

        # Round C — complete; finalize/mapping must catch up B then map C
        uc, ac = self._seed_pair('round-C', 'reply-C')
        end_c = _append_jsonl(path, [
            _line('u-c', 'user', session=SESSION, parent='a-b', content='round-C'),
            _line(
                'a-c', 'assistant', session=SESSION, parent='u-c',
                content=[{'type': 'text', 'text': 'reply-C'}],
            ),
        ])

        # Registry still at A; C's expected_start is end_b (gap)
        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(int(reg['scan_offset']), end_a)
        self.assertEqual(int(reg['last_mapped_message_id']), aa)
        self.assertFalse(self._mapped(ub))
        self.assertFalse(self._mapped(uc))

        ok_c = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=uc, assistant_message_id=ac,
                expected_start_offset=end_b, observed_end_offset=end_c,
            ),
            db_path=self.db,
        )
        self.assertTrue(ok_c.ok, ok_c.error_code)

        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        self.assertEqual(reg['scan_status'], SCAN_STATUS_READY)
        self.assertEqual(int(reg['scan_offset']), end_c)
        self.assertEqual(int(reg['last_mapped_message_id']), ac)
        self.assertTrue(self._mapped(ub))
        self.assertTrue(self._mapped(ab))
        self.assertTrue(self._mapped(uc))
        self.assertTrue(self._mapped(ac))

        # Capacity Swap candidate must include recent B/C, not fall back to only A
        current_uid = _insert_msg(self.db, 'hayana', 'swap-trigger')
        _bind_msg(
            self.db, current_uid, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )
        plan = mock.Mock()
        plan.resident_generation = self.gen
        plan.context_id = self.context_id
        plan.context_epoch = self.epoch
        plan.user_message_id = current_uid
        plan.user_content = 'swap-trigger'
        plan.db_path = self.db
        plan.chat_id = 'default'
        plan.cursor_before = current_uid
        plan.transcript_cwd = self.cwd
        plan.assembly = {'state': ''}

        prep = prepare_capacity_swap_for_plan(
            plan=plan,
            trigger_reason='soft_context',
            static_system='STATIC',
        )
        self.assertTrue(prep.ok, prep.error_code)
        self.assertIsNotNone(prep.candidate)
        selected = set(int(x) for x in prep.candidate.selected_message_ids)
        self.assertTrue(selected & {ub, ab, uc, ac}, selected)
        blob = prep.candidate.serialized_jsonl or ''
        self.assertIn('round-B-bath', blob)
        self.assertIn('round-C', blob)

    # ---- CASE 3: unalignable backlog → fail closed (swap + forge) ----
    def test_case3_unalignable_backlog_fail_closed(self) -> None:
        self._register(scan_offset=0)
        path = self._transcript_path()

        ua, aa = self._seed_pair('round-A', 'reply-A')
        end_a = _write_jsonl(path, [
            _line('u-a', 'user', session=SESSION, parent=None, content='round-A'),
            _line(
                'a-a', 'assistant', session=SESSION, parent='u-a',
                content=[{'type': 'text', 'text': 'reply-A'}],
            ),
        ])
        self.assertTrue(run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id, context_epoch=self.epoch,
                resident_generation=self.gen, chat_id='default',
                user_message_id=ua, assistant_message_id=aa,
                expected_start_offset=0, observed_end_offset=end_a,
            ),
            db_path=self.db,
        ).ok)

        # Auth has complete B, but transcript only has an incomplete user-only
        # segment with no following boundary → cannot safely skip or align.
        ub, ab = self._seed_pair('round-B', 'reply-B')
        end_b = _append_jsonl(path, [
            _line('u-b-only', 'user', session=SESSION, parent='a-a', content='dangling'),
        ])

        current_uid = _insert_msg(self.db, 'hayana', 'forge-or-swap')
        _bind_msg(
            self.db, current_uid, context_id=self.context_id,
            epoch=self.epoch, gen=self.gen, role='user',
        )

        # Catch-up cannot safely complete
        catchup = attempt_mapping_catchup_through(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            through_assistant_id=ab,
            through_user_message_id=ub,
            db_path=self.db,
        )
        self.assertFalse(catchup.ok)
        self.assertIn(
            catchup.error_code,
            {
                ERROR_MAPPING_LAG,
                ERROR_CATCHUP_AUTH_MISMATCH,
                'catchup_incomplete_unaligned',
                'incomplete_round_no_assistant',
                'catchup_failed',
            },
            catchup.error_code,
        )

        reg = get_context_claude_session(self.context_id, self.gen, db_path=self.db)
        # Must not look like a reliable READY-at-frontier prefix for Swap
        self.assertTrue(
            reg['scan_status'] == SCAN_STATUS_BLOCKED
            or int(reg['last_mapped_message_id']) < ab,
        )

        plan = mock.Mock()
        plan.resident_generation = self.gen
        plan.context_id = self.context_id
        plan.context_epoch = self.epoch
        plan.user_message_id = current_uid
        plan.user_content = 'forge-or-swap'
        plan.db_path = self.db
        plan.chat_id = 'default'
        plan.cursor_before = current_uid
        plan.transcript_cwd = self.cwd
        plan.assembly = {'state': ''}

        prep = prepare_capacity_swap_for_plan(
            plan=plan,
            trigger_reason='soft_context',
            static_system='STATIC',
        )
        self.assertFalse(prep.ok)
        self.assertEqual(prep.error_code, ERROR_MAPPING_LAG)
        self.assertIsNone(prep.candidate)

        # Manual Forge: selected recent B must not silently become old mapped A.
        # Missing mapping → fail closed (Carryover path uses DB rows; Preview
        # already fails on PREVIEW_MAPPING_MISSING). Here we assert the
        # selected recent IDs still lack mapping and must not be rewritten.
        self.assertFalse(self._mapped(ub))
        self.assertFalse(self._mapped(ab))
        self.assertTrue(self._mapped(ua))
        # Building forge events from selected recent IDs is allowed from DB,
        # but the mapping seam for continuity fails closed above; additionally
        # ensure we do not auto-substitute A when asked for B.
        conn = dc._connect(self.db)
        try:
            events = build_events_from_selected_messages(
                conn,
                selected_message_ids=[ub, ab],
                target_session_id='ffffffff-ffff-ffff-ffff-ffffffffffff',
                cwd=self.cwd,
            )
        finally:
            conn.close()
        texts = []
        for evt in events:
            content = (evt.get('message') or {}).get('content')
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        texts.append(str(block.get('text') or ''))
        joined = '\n'.join(texts)
        self.assertIn('round-B', joined)
        self.assertNotIn('round-A', joined)


if __name__ == '__main__':
    unittest.main()
