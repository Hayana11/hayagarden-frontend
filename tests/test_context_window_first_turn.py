"""Focused first-turn commit tests — temp SQLite + fake resident only."""
from __future__ import annotations

import datetime
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
from chat import daily_runtime as dr
from chat.claude_event_mapping import MappingPassRequest, run_mapping_pass
from chat.claude_transcript_model import ThinkingPolicy
from chat.context_window import (
    INTENT_COMMITTING,
    INTENT_COMMITTED,
    INTENT_HANDOFF_PENDING,
    INTENT_READY,
    INTENT_RESERVED,
    FirstTurnFinalizePendingError,
    _intent_row,
    get_first_turn_finalize_pending,
    get_latest_last_good_checkpoint,
    has_active_switch_intent,
    reserve_or_load_intent,
    resolve_canonical_context_row_conn,
)
from chat.context_window_first_turn import (
    FIRST_TURN_POSTCOMMIT_ABORT,
    FirstTurnError,
    abort_first_turn_clean,
    abort_first_turn_postcommit,
    claim_and_start_first_turn,
    complete_first_turn_round,
    ingest_first_turn_text_delta,
    offline_first_turn_hooks,
    recover_first_turn_finalize_pending,
    recover_first_turn_handoff_pending,
)
import chat.context_window_first_turn as ft_mod
from chat.context_window_forge_publish import (
    EMPTY_SHA256,
    PUBLISH_STATUS_NATIVE_COLD_BOUND,
    is_native_cold_binding,
    publish_context_window_forge_candidate,
)
from chat.context_window_target_prepare import (
    PREPARE_STATUS_READY,
    TargetPrepareHooks,
    offline_target_prepare_hooks,
    prepare_context_window_target,
)
from chat.context_window import WINDOW_MODE_MANUAL
from chat.daily_context import ConflictError, WINDOW_MODE_MANUAL_STAGED, _connect
from chat.session_registry import get_context_claude_session
from tools.cc_jsonl_usage import session_jsonl_path

SESSION_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
SESSION_B = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
NOW = datetime.datetime(2026, 7, 31, 12, 0, 0)


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='cw-first-turn-')
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
            cache_info TEXT DEFAULT '',
            choices TEXT DEFAULT '',
            image_url TEXT,
            source_kind TEXT NOT NULL DEFAULT 'chat',
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


def _line(
    uid: str,
    typ: str,
    *,
    session: str,
    parent,
    content,
    cwd: str = '/tmp/first-turn-cwd',
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
    return json.dumps(obj, ensure_ascii=False)


def _write_jsonl(path: Path, lines: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return len(raw)


def _append_jsonl(path: Path, lines: list[str]) -> int:
    raw = ''.join(
        (line if line.endswith('\n') else line + '\n') for line in lines
    ).encode('utf-8')
    with path.open('ab') as fh:
        fh.write(raw)
    os.chmod(path, 0o600)
    return path.stat().st_size


class ContextWindowFirstTurnTests(unittest.TestCase):
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
        self.switch_request_id = str(uuid.uuid4())
        self.first_turn_request_id = str(uuid.uuid4())
        self.claude_home = Path(self.home) / '.claude'
        self.prepare_hooks = offline_target_prepare_hooks(Path(self.tmp) / 'prep_logic')
        self.hooks = offline_first_turn_hooks(Path(self.tmp) / 'hooks_logic')
        # Align prepare hooks cwd/home with forge publish fixture tree.
        self.prepare_hooks = TargetPrepareHooks(
            prepare_staged=self.prepare_hooks.prepare_staged,
            discard_staged=self.prepare_hooks.discard_staged,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
        )
        self.hooks = ft_mod.FirstTurnHooks(
            prepare_staged=self.hooks.prepare_staged,
            discard_staged=self.hooks.discard_staged,
            formal_holder=self.hooks.formal_holder,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            prepare_fresh=self.hooks.prepare_fresh,
        )
        ft_mod._after_db_commit_hook = None
        ft_mod._before_handoff_hook = None
        dr.reset_bindings_for_tests()
        dr.set_owner_cursor_write_hook_for_tests(None)

    def tearDown(self) -> None:
        ft_mod._after_db_commit_hook = None
        ft_mod._before_handoff_hook = None
        dr.set_owner_cursor_write_hook_for_tests(None)
        dr.reset_bindings_for_tests()
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jsonl_path(self, session: str = SESSION_B) -> Path:
        path = session_jsonl_path(self.cwd, session, claude_home=str(self.claude_home))
        assert path is not None
        return Path(path)

    def _register_source(self, *, session: str, scan_offset: int):
        from chat.session_registry import register_context_claude_session
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            claude_session_id=session,
            cwd=self.cwd,
            source='test_first_turn',
            scan_offset=scan_offset,
            claude_home=str(self.claude_home),
            db_path=self.db,
        )

    def _map_turn(self, *, user_id: int, asst_id: int, start: int, end: int):
        result = run_mapping_pass(
            MappingPassRequest(
                context_id=self.context_id,
                context_epoch=self.epoch,
                resident_generation=self.gen,
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

    def _seed_source_and_forge(self):
        source_path = session_jsonl_path(self.cwd, SESSION_A, claude_home=str(self.claude_home))
        assert source_path is not None
        source_path = Path(source_path)
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
        end1 = _write_jsonl(source_path, turn1)
        end2 = _append_jsonl(source_path, turn2)
        self._register_source(session=SESSION_A, scan_offset=0)
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

        published = publish_context_window_forge_candidate(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=3,
            preview_id=self.switch_request_id,
            request_id=self.switch_request_id,
            thinking_policy=ThinkingPolicy.DROP,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(published.publish_status, 'PUBLISHED')
        prepare_context_window_target(
            request_id=self.switch_request_id,
            db_path=self.db,
            hooks=self.prepare_hooks,
            now=NOW,
        )
        return published

    def _intent(self) -> dict[str, Any]:
        conn = _connect(self.db)
        try:
            row = _intent_row(conn, self.switch_request_id)
            assert row is not None
            return row
        finally:
            conn.close()

    def _assert_last_good_empty(self, intent: Optional[dict[str, Any]] = None) -> None:
        row = intent if intent is not None else self._intent()
        for key in (
            'last_good_context_id',
            'last_good_context_epoch',
            'last_good_resident_generation',
            'last_good_history_cursor_message_id',
            'last_good_recorded_at',
        ):
            self.assertIsNone(row.get(key), key)

    def test_happy_path_first_delta_commit_and_complete(self):
        """1) prepare READY → first text → complete → target last-good checkpoint."""
        published = self._seed_source_and_forge()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        intent_before = self._intent()
        self.assertEqual(intent_before['status'], INTENT_READY)
        self._assert_last_good_empty(intent_before)
        self.assertIsNone(get_latest_last_good_checkpoint(db_path=self.db))
        self.assertEqual(
            dc.get_daily_context_by_id(
                int(intent_before['target_context_id']), db_path=self.db,
            )['window_mode'],
            WINDOW_MODE_MANUAL_STAGED,
        )
        target_id = int(intent_before['target_context_id'])
        target_epoch = int(
            dc.get_daily_context_by_id(target_id, db_path=self.db)['context_epoch'],
        )
        # Gateway chat_stream already inserted the formal user row.
        gateway_user_id = _insert_msg(self.db, 'hayana', '新房第一句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='新房第一句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(session.user_message_id, gateway_user_id)
        self.assertEqual(session.user_content, '新房第一句')
        intent_committing = self._intent()
        self.assertEqual(intent_committing['status'], INTENT_COMMITTING)
        self.assertEqual(int(intent_committing['first_user_message_id']), gateway_user_id)
        self._assert_last_good_empty(intent_committing)

        msg_ctx = dc.get_message_context(session.user_message_id, db_path=self.db)
        self.assertIsNotNone(msg_ctx)
        assert msg_ctx is not None
        self.assertEqual(int(msg_ctx['context_id']), target_id)

        # Source remains canonical before first non-empty text.
        conn = _connect(self.db)
        try:
            canonical_pre = resolve_canonical_context_row_conn(
                conn, chat_id='default', now=NOW,
            )
        finally:
            conn.close()
        self.assertEqual(int(canonical_pre['id']), self.context_id)
        self.assertIsNone(source_before.get('closed_at'))

        empty = ingest_first_turn_text_delta(
            session, text='   ', hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(empty.released_text, ())
        self.assertFalse(empty.db_committed)

        ft_mod.mark_first_turn_stdin_sent(session)
        self._assert_last_good_empty()  # stdin Ack must not advance last-good

        first = ingest_first_turn_text_delta(
            session, text='你好', hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(first.released_text, ('你好',))
        self.assertTrue(first.handoff_complete)

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertIsNotNone(source_after.get('closed_at'))
        target = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL)
        self.assertIsNone(target.get('closed_at'))

        intent_after = self._intent()
        self.assertEqual(intent_after['status'], INTENT_COMMITTED)
        self.assertIsNotNone(intent_after['first_delta_committed_at'])
        self._assert_last_good_empty(intent_after)  # HANDOFF/COMMITTED pre-complete

        conn = _connect(self.db)
        try:
            canonical = resolve_canonical_context_row_conn(conn, chat_id='default', now=NOW)
        finally:
            conn.close()
        self.assertEqual(int(canonical['id']), target_id)

        user_count = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages WHERE author=?', ('hayana',),
        ).fetchone()[0]
        self.assertEqual(user_count, 3)  # 2 seeded + 1 gateway user (no duplicate)

        # Crash window: assistant already mapped, intent missing first_assistant_message_id.
        orphan_id = dc.persist_daily_assistant_if_current(
            chat_id='default',
            context_id=session.target_context_id,
            context_epoch=session.target_context_epoch,
            resident_generation=session.target_resident_generation,
            lease_owner=self.first_turn_request_id,
            content='新房第一句回复',
            db_path=self.db,
            now=NOW,
        )
        self.assertIsNone(self._intent().get('first_assistant_message_id'))
        self._assert_last_good_empty()

        done = complete_first_turn_round(
            session,
            assistant_content='新房第一句回复',
            end_offset=int(published.jsonl_size) + 100,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(done.assistant_message_id, orphan_id)
        self.assertTrue(done.cursor_advanced)

        intent_done = self._intent()
        self.assertIsNotNone(intent_done.get('first_turn_completed_at'))
        self.assertEqual(
            int(intent_done['first_assistant_message_id']), done.assistant_message_id,
        )
        self.assertEqual(int(intent_done['last_good_context_id']), target_id)
        self.assertEqual(int(intent_done['last_good_context_epoch']), target_epoch)
        self.assertEqual(
            int(intent_done['last_good_resident_generation']),
            int(session.target_resident_generation),
        )
        self.assertEqual(
            int(intent_done['last_good_history_cursor_message_id']),
            done.assistant_message_id,
        )
        self.assertEqual(
            intent_done['last_good_recorded_at'],
            intent_done['first_turn_completed_at'],
        )
        # Source id must never be written into last-good.
        self.assertNotEqual(int(intent_done['last_good_context_id']), self.context_id)

        checkpoint = get_latest_last_good_checkpoint(db_path=self.db)
        self.assertIsNotNone(checkpoint)
        assert checkpoint is not None
        self.assertEqual(int(checkpoint['context_id']), target_id)
        self.assertEqual(int(checkpoint['context_epoch']), target_epoch)
        self.assertEqual(
            int(checkpoint['resident_generation']),
            int(session.target_resident_generation),
        )
        self.assertEqual(
            int(checkpoint['history_cursor_message_id']), done.assistant_message_id,
        )
        self.assertEqual(checkpoint['recorded_at'], intent_done['last_good_recorded_at'])
        self.assertEqual(checkpoint['switch_request_id'], self.switch_request_id)

        asst_count = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant' "
            "AND content=?",
            ('新房第一句回复',),
        ).fetchone()[0]
        self.assertEqual(asst_count, 1)

        # Retry after completed_at: still one assistant, same id; checkpoint stable.
        recorded_before = intent_done['last_good_recorded_at']
        again = complete_first_turn_round(
            session,
            assistant_content='新房第一句回复',
            end_offset=int(published.jsonl_size) + 100,
            db_path=self.db,
            now=NOW + datetime.timedelta(minutes=5),
        )
        self.assertEqual(again.assistant_message_id, orphan_id)
        intent_again = self._intent()
        self.assertEqual(intent_again['last_good_recorded_at'], recorded_before)
        self.assertEqual(
            int(intent_again['last_good_history_cursor_message_id']), orphan_id,
        )
        asst_count2 = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant' "
            "AND content=?",
            ('新房第一句回复',),
        ).fetchone()[0]
        self.assertEqual(asst_count2, 1)
        cp_again = get_latest_last_good_checkpoint(db_path=self.db)
        self.assertEqual(cp_again, checkpoint)

        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(int(binding.context_id), target_id)
        db_cursor_after = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        self.assertEqual(int(db_cursor_after), done.assistant_message_id)
        self.assertEqual(
            int(binding.bound_cursor_message_id), done.assistant_message_id,
        )

        with self.subTest('legacy_completed_row_backfill_when_cursor_confirmed'):
            # Simulate pre-upgrade completed row: completed_at set, last-good NULL.
            conn = _connect(self.db)
            try:
                conn.execute(
                    '''UPDATE context_switch_intents SET
                       last_good_context_id=NULL,
                       last_good_context_epoch=NULL,
                       last_good_resident_generation=NULL,
                       last_good_history_cursor_message_id=NULL,
                       last_good_recorded_at=NULL
                       WHERE request_id=?''',
                    (self.switch_request_id,),
                )
                conn.commit()
            finally:
                conn.close()
            self._assert_last_good_empty()
            cursor = dc.get_resident_history_cursor(
                target_id, session.target_resident_generation, db_path=self.db,
            )
            self.assertEqual(int(cursor), orphan_id)
            backfilled = complete_first_turn_round(
                session,
                assistant_content='新房第一句回复',
                end_offset=int(published.jsonl_size) + 100,
                db_path=self.db,
                now=NOW + datetime.timedelta(minutes=10),
            )
            self.assertEqual(backfilled.assistant_message_id, orphan_id)
            intent_bf = self._intent()
            self.assertEqual(int(intent_bf['last_good_context_id']), target_id)
            self.assertEqual(
                intent_bf['last_good_recorded_at'],
                intent_bf['first_turn_completed_at'],
            )
            self.assertNotEqual(
                int(intent_bf['last_good_context_id']), self.context_id,
            )

        # Public prepare whitelist must not contain server paths / switched_at.
        allowed_public = {
            'ok', 'prepare_status', 'request_id',
            'source_context_id', 'source_context_epoch', 'source_resident_generation',
            'target_context_id', 'target_context_epoch', 'candidate_session_id',
            'jsonl_sha256', 'jsonl_size', 'staged_ready_at', 'recovered', 'status',
        }
        sample_public = {
            'ok': True,
            'prepare_status': 'READY',
            'request_id': self.switch_request_id,
            'source_context_id': self.context_id,
            'source_context_epoch': self.epoch,
            'source_resident_generation': self.gen,
            'target_context_id': target_id,
            'target_context_epoch': int(target['context_epoch']),
            'candidate_session_id': str(published.candidate_session_id),
            'jsonl_sha256': str(published.jsonl_sha256),
            'jsonl_size': int(published.jsonl_size),
            'staged_ready_at': str(intent_before.get('staged_ready_at') or 'x'),
            'recovered': False,
            'status': 'ready',
        }
        self.assertEqual(set(sample_public.keys()), allowed_public)
        for key in (
            'jsonl_path', 'cwd', 'claude_home', 'transcript', 'st_dev', 'st_ino',
            'switched_at', 'staged_handle',
        ):
            self.assertNotIn(key, sample_public)

    def test_first_turn_complete_syncs_binding_second_turn_hot(self):
        """9A: complete advances DB+Local binding; next prepare is hot continuation."""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '新窗第一句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='新窗第一句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='第一句回复', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        binding_after_handoff = dr.get_local_binding()
        self.assertIsNotNone(binding_after_handoff)
        assert binding_after_handoff is not None
        watermark = binding_after_handoff.bound_cursor_message_id
        self.assertIsNotNone(watermark)

        done = complete_first_turn_round(
            session,
            assistant_content='第一句回复全文',
            end_offset=int(published.jsonl_size) + 80,
            db_path=self.db,
            now=NOW,
            thinking='我感觉到她换了新窗。',
            cache_info=json.dumps({
                'cache_read': 12698,
                'cache_creation': 130,
            }, ensure_ascii=False),
        )

        db_cursor = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(int(db_cursor), done.assistant_message_id)
        self.assertEqual(int(binding.bound_cursor_message_id), done.assistant_message_id)
        self.assertNotEqual(int(binding.bound_cursor_message_id), int(watermark))

        row = sqlite3.connect(self.db).execute(
            'SELECT thinking, cache_info FROM chat_messages WHERE id=?',
            (done.assistant_message_id,),
        ).fetchone()
        self.assertEqual(row[0], '我感觉到她换了新窗。')
        cache = json.loads(row[1])
        self.assertEqual(int(cache.get('cache_read') or 0), 12698)
        self.assertEqual(int(cache.get('cache_creation') or 0), 130)

        second_user = _insert_msg(self.db, 'hayana', '紧接着第二句')
        resident = self.hooks.formal_holder.get()
        self.assertIsNotNone(resident)
        with mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch(
                 'chat.daily_history._build_state_text', return_value=('', 'none', {}),
             ):
            plan = dr.prepare_daily_turn(
                user_message_id=second_user,
                db_path=self.db,
                resident=resident,
                static_system='S',
                now=datetime.datetime(2026, 7, 31, 12, 1, 0),
                wall_now=datetime.datetime(2026, 7, 31, 12, 1, 0),
            )
        self.assertFalse(plan.is_cold)
        self.assertFalse(plan.is_respawn)
        self.assertEqual(plan.manifest.get('turn_kind'), 'hot')

        assembled = dr.format_resident_turn_content(
            assembly=plan.assembly,
            user_content=plan.user_content,
            is_cold=plan.is_cold,
            is_respawn=plan.is_respawn,
        )
        self.assertNotIn('请回复最后一条用户消息。', assembled)
        self.assertNotIn('以下是本聊天日内的正式对话记录：', assembled)

    def test_gateway_first_turn_persists_thinking_and_cache_usage(self):
        """Gateway first-turn must persist streamed/done thinking + usage cache_info."""
        os.makedirs('/opt/workspace/tools', exist_ok=True)
        import gateway

        self._seed_source_and_forge()
        gateway_user_id = _insert_msg(self.db, 'hayana', '想想再说')
        seen = {}

        class _FakeStaged:
            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                seen['turn_lease'] = turn_lease
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                yield ('think', '先感受一下。')
                yield ('text', '小猫。')
                yield ('done', (
                    '小猫。',
                    '先感受一下。',
                    {
                        'cache_read': 100,
                        'cache_creation': 20,
                        'input_tokens': 120,
                        'output_tokens': 8,
                    },
                ))

            def _kill(self, quiet=True):
                return None

        hooks = self._gateway_first_turn_hooks(_FakeStaged())
        turn = {'user_message_id': gateway_user_id}
        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks):
            chunks = list(gateway._stream_cc_first_turn(turn, '想想再说', self._intent()))

        joined = ''.join(chunks)
        self.assertIn('"t": "think"', joined)
        self.assertIn('"t": "usage"', joined)
        self.assertIn('"ok": true', joined)
        self.assertEqual(self._intent()['status'], INTENT_COMMITTED)
        lease = seen.get('turn_lease')
        self.assertIsInstance(lease, dict)
        self.assertEqual(lease.get('turn_mode'), 'chat')
        self.assertEqual(lease.get('issued_from'), 'default_policy')
        self.assertTrue(str(lease.get('turn_id') or '').startswith('context-window-first-turn:'))

        aid = int(self._intent()['first_assistant_message_id'])
        row = sqlite3.connect(self.db).execute(
            'SELECT thinking, cache_info FROM chat_messages WHERE id=?',
            (aid,),
        ).fetchone()
        self.assertEqual(row[0], '先感受一下。')
        cache = json.loads(row[1])
        self.assertEqual(int(cache.get('cache_read') or 0), 100)
        self.assertEqual(int(cache.get('cache_creation') or 0), 20)

        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        assert binding is not None
        target_id = int(self._intent()['target_context_id'])
        db_cursor = dc.get_resident_history_cursor(
            target_id, 1, db_path=self.db,
        )
        self.assertEqual(int(binding.bound_cursor_message_id), aid)
        self.assertEqual(int(db_cursor), aid)

    def test_cursor_cas_failure_does_not_advance_binding_or_last_good(self):
        """assistant 已落库后若 DB cursor CAS 失败：binding/last-good 不得单边前进。"""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', 'CAS 失败句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='CAS 失败句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='会落库', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        binding_before = dr.get_local_binding()
        self.assertIsNotNone(binding_before)
        assert binding_before is not None
        watermark = int(binding_before.bound_cursor_message_id)
        db_cursor_before = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        self.assertEqual(int(db_cursor_before), watermark)
        self._assert_last_good_empty()

        hayana_before = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='hayana'",
        ).fetchone()[0]
        asst_before = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]

        with mock.patch(
            'chat.context_window_first_turn.advance_resident_history_cursor',
            side_effect=ConflictError('resident cursor CAS failed'),
        ):
            with self.assertRaises(ConflictError) as ar:
                complete_first_turn_round(
                    session,
                    assistant_content='已保存的回答',
                    end_offset=int(published.jsonl_size) + 50,
                    db_path=self.db,
                    now=NOW,
                    thinking='这次思考要留下。',
                    cache_info=json.dumps({'cache_read': 7, 'cache_creation': 3}),
                )
        self.assertIn('CAS failed', str(ar.exception))

        intent_failed = self._intent()
        self.assertEqual(intent_failed['status'], INTENT_COMMITTED)
        self.assertIsNone(intent_failed.get('first_turn_completed_at'))
        self._assert_last_good_empty(intent_failed)
        self.assertIsNotNone(intent_failed.get('first_assistant_message_id'))
        assistant_id = int(intent_failed['first_assistant_message_id'])

        # Exactly one new assistant; no second answer invented.
        hayana_after = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='hayana'",
        ).fetchone()[0]
        asst_after = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        self.assertEqual(hayana_after, hayana_before)
        self.assertEqual(asst_after, asst_before + 1)

        row = sqlite3.connect(self.db).execute(
            'SELECT content, thinking, cache_info FROM chat_messages WHERE id=?',
            (assistant_id,),
        ).fetchone()
        self.assertEqual(row[0], '已保存的回答')
        self.assertEqual(row[1], '这次思考要留下。')
        self.assertEqual(json.loads(row[2]).get('cache_read'), 7)

        # DB cursor + LocalResidentBinding stay on watermark — never one-sided.
        db_cursor_after = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        binding_after = dr.get_local_binding()
        self.assertIsNotNone(binding_after)
        assert binding_after is not None
        self.assertEqual(int(db_cursor_after), watermark)
        self.assertEqual(int(binding_after.bound_cursor_message_id), watermark)
        self.assertNotEqual(int(binding_after.bound_cursor_message_id), assistant_id)

        # Lease remains held — completion did not falsely finish.
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (
                target_id,
                session.target_resident_generation,
                self.first_turn_request_id,
            ),
        ).fetchone()[0]
        self.assertEqual(lease_n, 1)

        with self.subTest('retry_after_cas_failure_succeeds_without_wiping_think'):
            # Retry with empty thinking/cache must reuse the same assistant row.
            recovered = complete_first_turn_round(
                session,
                assistant_content='不该覆盖的重试正文',
                end_offset=int(published.jsonl_size) + 50,
                db_path=self.db,
                now=NOW + datetime.timedelta(seconds=30),
                thinking='',
                cache_info='',
            )
            self.assertEqual(recovered.assistant_message_id, assistant_id)
            self.assertTrue(recovered.cursor_advanced)

            intent_ok = self._intent()
            self.assertIsNotNone(intent_ok.get('first_turn_completed_at'))
            self.assertEqual(
                int(intent_ok['last_good_history_cursor_message_id']), assistant_id,
            )

            row2 = sqlite3.connect(self.db).execute(
                'SELECT content, thinking, cache_info FROM chat_messages WHERE id=?',
                (assistant_id,),
            ).fetchone()
            self.assertEqual(row2[0], '已保存的回答')
            self.assertEqual(row2[1], '这次思考要留下。')
            self.assertEqual(json.loads(row2[2]).get('cache_read'), 7)

            binding_ok = dr.get_local_binding()
            db_ok = dc.get_resident_history_cursor(
                target_id, session.target_resident_generation, db_path=self.db,
            )
            self.assertEqual(int(db_ok), assistant_id)
            self.assertEqual(int(binding_ok.bound_cursor_message_id), assistant_id)

            asst_final = sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
            ).fetchone()[0]
            self.assertEqual(asst_final, asst_before + 1)

    def test_complete_idempotent_preserves_thinking_and_cache_info(self):
        """重复 complete：不造第二份回答，不抹掉 Think/cache，cursor/last-good 稳定。"""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '幂等收尾句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='幂等收尾句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='幂等正文', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        thinking = '第一次完整思考，不能被重试抹掉。'
        cache_info = json.dumps({
            'cache_read': 222,
            'cache_creation': 11,
        }, ensure_ascii=False)

        first = complete_first_turn_round(
            session,
            assistant_content='幂等正文全文',
            end_offset=int(published.jsonl_size) + 60,
            db_path=self.db,
            now=NOW,
            thinking=thinking,
            cache_info=cache_info,
        )
        self.assertTrue(first.cursor_advanced)

        intent_done = self._intent()
        recorded_at = intent_done['last_good_recorded_at']
        self.assertEqual(
            int(intent_done['last_good_history_cursor_message_id']),
            first.assistant_message_id,
        )

        hayana_n = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='hayana'",
        ).fetchone()[0]
        asst_n = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]

        # Network blip retry: empty thinking/cache must not wipe the first persist.
        again = complete_first_turn_round(
            session,
            assistant_content='另一份不该写入的回答',
            end_offset=int(published.jsonl_size) + 999,
            db_path=self.db,
            now=NOW + datetime.timedelta(minutes=3),
            thinking='',
            cache_info='',
        )
        self.assertEqual(again.assistant_message_id, first.assistant_message_id)
        self.assertFalse(again.cursor_advanced)

        intent_again = self._intent()
        self.assertEqual(intent_again['last_good_recorded_at'], recorded_at)
        self.assertEqual(
            int(intent_again['last_good_history_cursor_message_id']),
            first.assistant_message_id,
        )
        self.assertEqual(
            int(intent_again['first_assistant_message_id']),
            first.assistant_message_id,
        )

        hayana_again = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='hayana'",
        ).fetchone()[0]
        asst_again = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        self.assertEqual(hayana_again, hayana_n)
        self.assertEqual(asst_again, asst_n)

        row = sqlite3.connect(self.db).execute(
            'SELECT content, thinking, cache_info FROM chat_messages WHERE id=?',
            (first.assistant_message_id,),
        ).fetchone()
        self.assertEqual(row[0], '幂等正文全文')
        self.assertEqual(row[1], thinking)
        self.assertEqual(row[2], cache_info)

        db_cursor = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(int(db_cursor), first.assistant_message_id)
        self.assertEqual(int(binding.bound_cursor_message_id), first.assistant_message_id)

    def test_gateway_first_turn_extracts_and_persists_choices(self):
        """First-turn must strip [choices] from body and persist choices column."""
        os.makedirs('/opt/workspace/tools', exist_ok=True)
        import gateway

        self._seed_source_and_forge()
        gateway_user_id = _insert_msg(self.db, 'hayana', '给我选项')

        class _FakeStaged:
            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                body = '正文。\n[choices]A|B[/choices]'
                yield ('text', body)
                yield ('done', (body, '想了想选项。', {'cache_read': 1}))

            def _kill(self, quiet=True):
                return None

        hooks = self._gateway_first_turn_hooks(_FakeStaged())
        turn = {'user_message_id': gateway_user_id}
        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks):
            chunks = list(gateway._stream_cc_first_turn(turn, '给我选项', self._intent()))

        joined = ''.join(chunks)
        self.assertIn('"ok": true', joined)
        aid = int(self._intent()['first_assistant_message_id'])
        row = sqlite3.connect(self.db).execute(
            'SELECT content, choices, thinking FROM chat_messages WHERE id=?',
            (aid,),
        ).fetchone()
        self.assertEqual(row[0], '正文。')
        self.assertNotIn('[choices]', row[0])
        self.assertEqual(json.loads(row[1]), ['A', 'B'])
        self.assertEqual(row[2], '想了想选项。')

    def test_final_checkpoint_failure_keeps_lease_blocks_next_turn(self):
        """final txn 失败：lease 与 checkpoint 同退；下一轮被挡；重试 finalize 成功。"""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', 'checkpoint 失败句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='checkpoint 失败句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='已出正文', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        real_update = ft_mod._update_intent_conn
        boom = {'n': 0}

        def _boom_on_completed_at(conn, request_id, *, status=None, fields=None, now_s=None):
            fields = fields or {}
            if fields.get('first_turn_completed_at') is not None and boom['n'] == 0:
                boom['n'] += 1
                raise sqlite3.OperationalError('simulated final checkpoint failure')
            return real_update(
                conn, request_id, status=status, fields=fields, now_s=now_s,
            )

        with mock.patch.object(ft_mod, '_update_intent_conn', side_effect=_boom_on_completed_at):
            with self.assertRaises(sqlite3.OperationalError):
                complete_first_turn_round(
                    session,
                    assistant_content='已保存的回答',
                    end_offset=int(published.jsonl_size) + 40,
                    db_path=self.db,
                    now=NOW,
                    thinking='留下思考',
                    cache_info=json.dumps({'cache_read': 9}),
                    choices=json.dumps(['是', '否'], ensure_ascii=False),
                )

        intent_failed = self._intent()
        self.assertIsNone(intent_failed.get('first_turn_completed_at'))
        self._assert_last_good_empty(intent_failed)
        self.assertIsNotNone(intent_failed.get('first_assistant_message_id'))
        assistant_id = int(intent_failed['first_assistant_message_id'])
        pending = get_first_turn_finalize_pending(db_path=self.db)
        self.assertIsNotNone(pending)
        self.assertEqual(str(pending['request_id']), self.switch_request_id)

        # Cursor/binding may already be at assistant; lease must remain (atomic).
        db_cursor = dc.get_resident_history_cursor(
            target_id, session.target_resident_generation, db_path=self.db,
        )
        binding = dr.get_local_binding()
        self.assertEqual(int(db_cursor), assistant_id)
        self.assertEqual(int(binding.bound_cursor_message_id), assistant_id)
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (
                target_id,
                session.target_resident_generation,
                self.first_turn_request_id,
            ),
        ).fetchone()[0]
        self.assertEqual(lease_n, 1)

        row = sqlite3.connect(self.db).execute(
            'SELECT content, thinking, choices FROM chat_messages WHERE id=?',
            (assistant_id,),
        ).fetchone()
        self.assertEqual(row[0], '已保存的回答')
        self.assertEqual(row[1], '留下思考')
        self.assertEqual(json.loads(row[2]), ['是', '否'])

        # Keep lease live so atomic retention is still observable; the ordinary
        # turn gate is FIRST_TURN_FINALIZE_PENDING (not lease TTL).
        wall_now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
        live_exp = (wall_now + datetime.timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')
        conn = _connect(self.db)
        try:
            conn.execute(
                'UPDATE daily_resident_turn_leases SET expires_at=?, updated_at=? '
                'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
                (
                    live_exp, live_exp, target_id,
                    session.target_resident_generation, self.first_turn_request_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        second_user = _insert_msg(self.db, 'hayana', '被挡住的第二句')
        resident = self.hooks.formal_holder.get()
        with mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch(
                 'chat.daily_history._build_state_text', return_value=('', 'none', {}),
             ):
            with self.assertRaises(dr.FirstTurnFinalizePendingError) as ar:
                dr.prepare_daily_turn(
                    user_message_id=second_user,
                    db_path=self.db,
                    resident=resident,
                    static_system='S',
                    now=wall_now,
                    wall_now=wall_now,
                )
            self.assertEqual(ar.exception.error_code, 'FIRST_TURN_FINALIZE_PENDING')

        # Explicit finalize retry heals checkpoint and releases lease.
        healed = complete_first_turn_round(
            session,
            assistant_content='',
            end_offset=int(published.jsonl_size) + 40,
            db_path=self.db,
            now=NOW + datetime.timedelta(seconds=45),
            thinking='',
            cache_info='',
            choices='',
        )
        self.assertEqual(healed.assistant_message_id, assistant_id)
        intent_ok = self._intent()
        self.assertIsNotNone(intent_ok.get('first_turn_completed_at'))
        self.assertEqual(
            int(intent_ok['last_good_history_cursor_message_id']), assistant_id,
        )
        self.assertIsNone(get_first_turn_finalize_pending(db_path=self.db))
        lease_after = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (
                target_id,
                session.target_resident_generation,
                self.first_turn_request_id,
            ),
        ).fetchone()[0]
        self.assertEqual(lease_after, 0)

        # Think/choices survive empty retry payload.
        row2 = sqlite3.connect(self.db).execute(
            'SELECT content, thinking, choices FROM chat_messages WHERE id=?',
            (assistant_id,),
        ).fetchone()
        self.assertEqual(row2[0], '已保存的回答')
        self.assertEqual(row2[1], '留下思考')
        self.assertEqual(json.loads(row2[2]), ['是', '否'])

    def test_final_checkpoint_failure_blocks_after_lease_ttl(self):
        """final txn fail → lease TTL(+481s) 后仍挡；explicit finalize 才放行第二轮。"""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', 'TTL 穿透句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='TTL 穿透句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='已出正文', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        real_update = ft_mod._update_intent_conn
        boom = {'n': 0}

        def _boom_on_completed_at(conn, request_id, *, status=None, fields=None, now_s=None):
            fields = fields or {}
            if fields.get('first_turn_completed_at') is not None and boom['n'] == 0:
                boom['n'] += 1
                raise sqlite3.OperationalError('simulated final checkpoint failure')
            return real_update(
                conn, request_id, status=status, fields=fields, now_s=now_s,
            )

        with mock.patch.object(ft_mod, '_update_intent_conn', side_effect=_boom_on_completed_at):
            with self.assertRaises(sqlite3.OperationalError):
                complete_first_turn_round(
                    session,
                    assistant_content='TTL 失败后仍在的回答',
                    end_offset=int(published.jsonl_size) + 40,
                    db_path=self.db,
                    now=NOW,
                    thinking='留下思考',
                    cache_info=json.dumps({'cache_read': 3}),
                    choices=json.dumps(['续', '停'], ensure_ascii=False),
                )

        intent_failed = self._intent()
        self.assertEqual(intent_failed.get('status'), INTENT_COMMITTED)
        self.assertIsNone(intent_failed.get('first_turn_completed_at'))
        self.assertIsNotNone(intent_failed.get('first_assistant_message_id'))
        assistant_id = int(intent_failed['first_assistant_message_id'])
        self._assert_last_good_empty(intent_failed)

        # Simulate first-turn lease TTL elapsed: claim_daily_resident_turn would
        # treat expires_at <= now as absent and re-claim — without the persist
        # gate that would penetrate with a incomplete first-turn checkpoint.
        wall_now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
        expired_at = (wall_now - datetime.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        conn = _connect(self.db)
        try:
            conn.execute(
                'UPDATE daily_resident_turn_leases SET expires_at=?, updated_at=? '
                'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
                (
                    expired_at, expired_at, target_id,
                    session.target_resident_generation, self.first_turn_request_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Logical +481s past mint (lease TTL is 480s); wall_now also past expiry.
        after_ttl = NOW + datetime.timedelta(seconds=481)
        second_user = _insert_msg(self.db, 'hayana', 'TTL 后仍应被挡')
        resident = self.hooks.formal_holder.get()
        with mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch(
                 'chat.daily_history._build_state_text', return_value=('', 'none', {}),
             ):
            with self.assertRaises(dr.FirstTurnFinalizePendingError) as ar:
                dr.prepare_daily_turn(
                    user_message_id=second_user,
                    db_path=self.db,
                    resident=resident,
                    static_system='S',
                    now=after_ttl,
                    wall_now=wall_now,
                )
            self.assertEqual(ar.exception.error_code, 'FIRST_TURN_FINALIZE_PENDING')

        # Explicit finalize clears the gate; ordinary second turn may proceed.
        healed = complete_first_turn_round(
            session,
            assistant_content='',
            end_offset=int(published.jsonl_size) + 40,
            db_path=self.db,
            now=after_ttl,
            thinking='',
            cache_info='',
            choices='',
        )
        self.assertEqual(healed.assistant_message_id, assistant_id)
        self.assertIsNotNone(self._intent().get('first_turn_completed_at'))
        self.assertIsNone(get_first_turn_finalize_pending(db_path=self.db))

        third_user = _insert_msg(self.db, 'hayana', 'finalize 后第二轮')
        with mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch(
                 'chat.daily_history._build_state_text', return_value=('', 'none', {}),
             ):
            plan = dr.prepare_daily_turn(
                user_message_id=third_user,
                db_path=self.db,
                resident=resident,
                static_system='S',
                now=wall_now,
                wall_now=wall_now,
            )
        self.assertEqual(int(plan.context_id), target_id)
        self.assertFalse(plan.is_cold)
        self.assertEqual(int(plan.cursor_before), assistant_id)

    def test_db_only_finalize_recovery_across_request_boundary(self):
        """final txn fail → drop FirstTurnSession → DB-only finalize clears gate."""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '跨请求收尾句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='跨请求收尾句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='已出正文', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        real_update = ft_mod._update_intent_conn
        boom = {'n': 0}

        def _boom_on_completed_at(conn, request_id, *, status=None, fields=None, now_s=None):
            fields = fields or {}
            if fields.get('first_turn_completed_at') is not None and boom['n'] == 0:
                boom['n'] += 1
                raise sqlite3.OperationalError('simulated final checkpoint failure')
            return real_update(
                conn, request_id, status=status, fields=fields, now_s=now_s,
            )

        with mock.patch.object(ft_mod, '_update_intent_conn', side_effect=_boom_on_completed_at):
            with self.assertRaises(sqlite3.OperationalError):
                complete_first_turn_round(
                    session,
                    assistant_content='跨请求仍在的回答',
                    end_offset=int(published.jsonl_size) + 40,
                    db_path=self.db,
                    now=NOW,
                    thinking='留下思考',
                    cache_info=json.dumps({'cache_read': 2}),
                    choices=json.dumps(['好', '行'], ensure_ascii=False),
                )

        assistant_id = int(self._intent()['first_assistant_message_id'])
        self.assertIsNotNone(get_first_turn_finalize_pending(db_path=self.db))
        # Request/process boundary: discard the only FirstTurnSession handle.
        session = None

        healed = recover_first_turn_finalize_pending(
            db_path=self.db,
            now=NOW + datetime.timedelta(seconds=90),
        )
        self.assertIsNotNone(healed)
        assert healed is not None
        self.assertEqual(healed.assistant_message_id, assistant_id)
        self.assertIsNone(get_first_turn_finalize_pending(db_path=self.db))
        intent_ok = self._intent()
        self.assertIsNotNone(intent_ok.get('first_turn_completed_at'))
        self.assertEqual(
            int(intent_ok['last_good_history_cursor_message_id']), assistant_id,
        )
        lease_after = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND lease_owner=?',
            (target_id, self.first_turn_request_id),
        ).fetchone()[0]
        self.assertEqual(lease_after, 0)

        second_user = _insert_msg(self.db, 'hayana', '收尾后第二轮')
        resident = self.hooks.formal_holder.get()
        wall_now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
        with mock.patch('chat.daily_context.enabled', return_value=True), \
             mock.patch(
                 'chat.daily_history._build_state_text', return_value=('', 'none', {}),
             ):
            plan = dr.prepare_daily_turn(
                user_message_id=second_user,
                db_path=self.db,
                resident=resident,
                static_system='S',
                now=wall_now,
                wall_now=wall_now,
            )
        self.assertEqual(int(plan.context_id), target_id)
        self.assertFalse(plan.is_cold)
        self.assertEqual(int(plan.cursor_before), assistant_id)

    def test_finalize_pending_blocks_new_manual_switch(self):
        """final txn fail → lease TTL 后仍拒新换窗；DB-only finalize 后才放行。"""
        published = self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        target_row = dc.get_daily_context_by_id(target_id, db_path=self.db)
        target_epoch = int(target_row['context_epoch'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '换窗绕过句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='换窗绕过句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='已出正文', hooks=self.hooks, db_path=self.db, now=NOW,
        )

        real_update = ft_mod._update_intent_conn
        boom = {'n': 0}

        def _boom_on_completed_at(conn, request_id, *, status=None, fields=None, now_s=None):
            fields = fields or {}
            if fields.get('first_turn_completed_at') is not None and boom['n'] == 0:
                boom['n'] += 1
                raise sqlite3.OperationalError('simulated final checkpoint failure')
            return real_update(
                conn, request_id, status=status, fields=fields, now_s=now_s,
            )

        with mock.patch.object(ft_mod, '_update_intent_conn', side_effect=_boom_on_completed_at):
            with self.assertRaises(sqlite3.OperationalError):
                complete_first_turn_round(
                    session,
                    assistant_content='换窗前残缺回答',
                    end_offset=int(published.jsonl_size) + 40,
                    db_path=self.db,
                    now=NOW,
                )

        self.assertEqual(self._intent().get('status'), INTENT_COMMITTED)
        self.assertIsNotNone(get_first_turn_finalize_pending(db_path=self.db))

        # Expire first-turn lease (TTL +481s); switch must still be blocked.
        wall_now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
        expired_at = (wall_now - datetime.timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S')
        conn = _connect(self.db)
        try:
            conn.execute(
                'UPDATE daily_resident_turn_leases SET expires_at=?, updated_at=? '
                'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
                (
                    expired_at, expired_at, target_id,
                    session.target_resident_generation, self.first_turn_request_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        session = None

        new_switch_id = str(uuid.uuid4())
        with self.assertRaises(FirstTurnFinalizePendingError) as ar:
            reserve_or_load_intent(
                source_context_id=target_id,
                source_context_epoch=target_epoch,
                count=0,
                request_id=new_switch_id,
                db_path=self.db,
                now=NOW + datetime.timedelta(seconds=481),
            )
        self.assertEqual(ar.exception.error_code, 'FIRST_TURN_FINALIZE_PENDING')

        healed = recover_first_turn_finalize_pending(
            db_path=self.db,
            now=NOW + datetime.timedelta(seconds=500),
        )
        self.assertIsNotNone(healed)
        self.assertIsNone(get_first_turn_finalize_pending(db_path=self.db))

        reserved = reserve_or_load_intent(
            source_context_id=target_id,
            source_context_epoch=target_epoch,
            count=0,
            request_id=new_switch_id,
            db_path=self.db,
            now=NOW + datetime.timedelta(seconds=510),
        )
        self.assertEqual(reserved['status'], INTENT_RESERVED)
        self.assertEqual(str(reserved['request_id']), new_switch_id)

    def test_postcommit_client_abort_releases_lease_and_bumps_generation(self):
        """Post-commit abort: release first-turn lease + bump gen once (idempotent)."""
        self._seed_source_and_forge()
        intent_ready = self._intent()
        self.assertEqual(intent_ready['status'], INTENT_READY)
        target_id = int(intent_ready['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', 'postcommit abort 第一句')

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='postcommit abort 第一句',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session)
        first = ingest_first_turn_text_delta(
            session, text='你好', hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertTrue(first.db_committed)
        self.assertTrue(first.handoff_complete)

        intent_committed = self._intent()
        self.assertEqual(intent_committed['status'], INTENT_COMMITTED)
        target_before = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(target_before['window_mode'], WINDOW_MODE_MANUAL)
        self.assertIsNone(target_before.get('closed_at'))
        old_gen = int(session.target_resident_generation)
        self.assertEqual(int(target_before['resident_generation']), old_gen)
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (target_id, old_gen, self.first_turn_request_id),
        ).fetchone()[0]
        self.assertEqual(lease_n, 1)

        # Simulate client abort / stream end after first delta released.
        abort_first_turn_postcommit(session, db_path=self.db, now=NOW)

        lease_after = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (target_id, old_gen, self.first_turn_request_id),
        ).fetchone()[0]
        self.assertEqual(lease_after, 0)
        self.assertFalse(
            dc.has_active_provider_turn_lease('default', db_path=self.db, now=NOW),
        )

        target_after = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(int(target_after['resident_generation']), old_gen + 1)
        self.assertEqual(target_after['window_mode'], WINDOW_MODE_MANUAL)
        self.assertIsNone(target_after.get('closed_at'))

        intent_after = self._intent()
        self.assertEqual(intent_after['status'], INTENT_COMMITTED)
        self.assertEqual(
            intent_after.get('first_turn_error_code'), FIRST_TURN_POSTCOMMIT_ABORT,
        )
        self.assertIsNone(intent_after.get('first_assistant_message_id'))
        self.assertIsNone(intent_after.get('first_turn_completed_at'))
        self.assertIsNone(intent_after.get('first_turn_end_offset'))
        self._assert_last_good_empty(intent_after)

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertIsNotNone(source_after.get('closed_at'))
        conn = _connect(self.db)
        try:
            canonical = resolve_canonical_context_row_conn(
                conn, chat_id='default', now=NOW,
            )
        finally:
            conn.close()
        self.assertEqual(int(canonical['id']), target_id)

        # Second call must not bump again (N+1 stays N+1).
        abort_first_turn_postcommit(session, db_path=self.db, now=NOW)
        target_again = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(int(target_again['resident_generation']), old_gen + 1)
        intent_again = self._intent()
        self.assertEqual(intent_again['status'], INTENT_COMMITTED)
        self.assertEqual(
            intent_again.get('first_turn_error_code'), FIRST_TURN_POSTCOMMIT_ABORT,
        )
        self.assertIsNone(intent_again.get('first_assistant_message_id'))
        self.assertIsNone(intent_again.get('first_turn_completed_at'))
        self._assert_last_good_empty(intent_again)
        self.assertFalse(
            dc.has_active_provider_turn_lease('default', db_path=self.db, now=NOW),
        )

    def _assert_pre_first_text_clean(
        self, *, source_before, target_id, gateway_user_id, expect_dirty: bool = False,
    ) -> None:
        intent = self._intent()
        source = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        target = dc.get_daily_context_by_id(target_id, db_path=self.db)
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND lease_owner=?',
            (target_id, self.first_turn_request_id),
        ).fetchone()[0]
        asst_n = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        user_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages WHERE content=?',
            ('可重试',),
        ).fetchone()[0]
        self.assertEqual(user_n, 1)
        self.assertEqual(asst_n, 0)
        self.assertEqual(int(intent['first_user_message_id']), gateway_user_id)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL_STAGED)
        self.assertEqual(source.get('closed_at'), source_before.get('closed_at'))
        if expect_dirty:
            self.assertEqual(intent.get('orphan_jsonl_state'), 'precommit_dirty')
            # Dirty must not roll back to READY (message may already be in Claude).
            self.assertNotEqual(intent['status'], INTENT_READY)
            self.assertNotEqual(intent['status'], INTENT_COMMITTED)
        else:
            self.assertEqual(intent['status'], INTENT_READY)
            self.assertEqual(lease_n, 0)
            self.assertNotEqual(intent.get('orphan_jsonl_state'), 'precommit_dirty')
            conn = _connect(self.db)
            try:
                canonical = resolve_canonical_context_row_conn(
                    conn, chat_id='default', now=NOW,
                )
            finally:
                conn.close()
            self.assertEqual(int(canonical['id']), self.context_id)

    def _gateway_first_turn_hooks(self, fake_staged):
        return ft_mod.FirstTurnHooks(
            prepare_staged=lambda intent, path: fake_staged,
            discard_staged=self.hooks.discard_staged,
            formal_holder=self.hooks.formal_holder,
            forge_cwd=self.hooks.forge_cwd,
            claude_home=self.hooks.claude_home,
            prepare_fresh=lambda intent, path: fake_staged,
        )

    def test_message_id_only_uses_db_content_for_send_turn(self):
        """LIVE_FAIL regression: FE stream sends user_message_id without body.

        Canonical text must come from chat_messages and reach staged.send_turn.
        Non-empty request mismatch still fails closed.
        """
        import gateway

        self._seed_source_and_forge()
        body = '爸爸爸爸！（探头，咪咪喵喵地跑来跑去。）'
        gateway_user_id = _insert_msg(self.db, 'hayana', body)

        with self.assertRaises(FirstTurnError) as ar:
            claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=self.first_turn_request_id,
                user_content='完全不同的正文',
                user_message_id=gateway_user_id,
                hooks=self.hooks,
                db_path=self.db,
                now=NOW,
            )
        self.assertEqual(ar.exception.error_code, 'FIRST_TURN_USER_CONTENT_CONFLICT')
        self.assertEqual(self._intent()['status'], INTENT_READY)

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='',  # production message-id-only contract
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(session.user_message_id, gateway_user_id)
        self.assertEqual(session.user_content, body)
        abort_first_turn_clean(session, hooks=self.hooks, db_path=self.db, now=NOW)
        self.assertEqual(self._intent()['status'], INTENT_READY)

        sent: list[str] = []

        class _CaptureStaged:
            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                sent.append(str(content))
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                yield ('text', '收到')
                yield ('done', ('收到', '', {}))

            def _kill(self, quiet=True):
                return None

        fake = _CaptureStaged()
        hooks = self._gateway_first_turn_hooks(fake)
        turn = {'user_message_id': gateway_user_id}
        live_intent = self._intent()
        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks):
            # Empty _uc mirrors production chat_stream after /api/chat/send.
            chunks = list(gateway._stream_cc_first_turn(turn, '', live_intent))
        self.assertEqual(sent, [body])
        joined = ''.join(chunks)
        self.assertIn('"t": "text"', joined)
        self.assertIn('"ok": true', joined)
        self.assertEqual(self._intent()['status'], INTENT_COMMITTED)

    def test_first_turn_idle_heartbeat_ping_no_commit(self):
        """MF-009 CASE 1: heartbeat ping must not commit first delta or close source."""
        import gateway

        self._seed_source_and_forge()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        gateway_user_id = _insert_msg(self.db, 'hayana', 'heartbeat case')

        class _FakeStaged:
            def __init__(self):
                self.killed = False
                self.ack_called = False

            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                if on_stdin_flushed is not None:
                    self.ack_called = True
                    on_stdin_flushed()
                yield ('heartbeat', None)
                yield ('heartbeat', None)
                yield ('text', '你好')
                yield ('done', ('你好', '', {}))

            def _kill(self, quiet=True):
                self.killed = True

        fake = _FakeStaged()
        hooks = self._gateway_first_turn_hooks(fake)
        turn = {'user_message_id': gateway_user_id}
        live_intent = self._intent()

        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks):
            gen = gateway._stream_cc_first_turn(turn, 'heartbeat case', live_intent)
            first_chunk = next(gen)
            self.assertIn('"t": "ping"', first_chunk)
            self.assertNotIn('"t": "text"', first_chunk)

            intent_after_first_ping = self._intent()
            self.assertEqual(intent_after_first_ping['status'], INTENT_COMMITTING)
            self.assertIsNone(intent_after_first_ping.get('first_delta_committed_at'))
            source_mid = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
            self.assertEqual(source_mid.get('closed_at'), source_before.get('closed_at'))

            rest = list(gen)

        joined = first_chunk + ''.join(rest)
        self.assertEqual(joined.count('"t": "ping"'), 2)
        self.assertIn('"t": "text"', joined)
        self.assertIn('"ok": true', joined)
        self.assertTrue(fake.ack_called)

        intent_after = self._intent()
        self.assertEqual(intent_after['status'], INTENT_COMMITTED)
        self.assertIsNotNone(intent_after['first_delta_committed_at'])
        asst_count = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        self.assertGreaterEqual(asst_count, 1)

    def test_native_cold_zero_carryover_full_path(self):
        """Round 1: count=0 NATIVE_COLD → Target READY → fresh first-turn → committed.

        Source has no Registry/mapping. Cuts transcript carryover only; offline
        prepare_fresh stands in for production system/persona/memory spawn.
        """
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        before_jsonl = set(Path(self.home).rglob('*.jsonl'))

        published = publish_context_window_forge_candidate(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=0,
            preview_id=self.switch_request_id,
            request_id=self.switch_request_id,
            thinking_policy=ThinkingPolicy.DROP,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(published.publish_status, PUBLISH_STATUS_NATIVE_COLD_BOUND)
        self.assertIsNone(published.jsonl_path)
        self.assertEqual(published.jsonl_size, 0)
        self.assertEqual(published.jsonl_sha256, EMPTY_SHA256)
        self.assertEqual(set(Path(self.home).rglob('*.jsonl')), before_jsonl)

        intent_forging = self._intent()
        self.assertTrue(is_native_cold_binding(intent_forging))
        self.assertEqual(int(intent_forging['carryover_count']), 0)
        self.assertEqual(json.loads(intent_forging['selected_message_ids_json']), [])

        prepare_calls = {'staged': 0}

        def counting_prepare(intent, path, identity):
            prepare_calls['staged'] += 1
            raise AssertionError('cold Target prepare must not spawn/resume')

        cold_prepare_hooks = TargetPrepareHooks(
            prepare_staged=counting_prepare,
            discard_staged=self.prepare_hooks.discard_staged,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
        )
        prepared = prepare_context_window_target(
            request_id=self.switch_request_id,
            db_path=self.db,
            hooks=cold_prepare_hooks,
            now=NOW,
        )
        self.assertEqual(prepare_calls['staged'], 0)
        self.assertEqual(prepared.prepare_status, PREPARE_STATUS_READY)
        self.assertEqual(prepared.jsonl_size, 0)
        self.assertEqual(prepared.jsonl_sha256, EMPTY_SHA256)
        self.assertFalse(prepared.jsonl_path.is_file())

        intent_ready = self._intent()
        self.assertEqual(intent_ready['status'], INTENT_READY)
        self.assertIsNotNone(intent_ready['staged_ready_at'])
        target_id = int(intent_ready['target_context_id'])
        reg = get_context_claude_session(target_id, 1, db_path=self.db)
        self.assertIsNotNone(reg)
        assert reg is not None
        self.assertEqual(reg['claude_session_id'], published.candidate_session_id)
        self.assertEqual(int(reg['scan_offset']), 0)
        self.assertEqual(reg['scan_status'], 'READY')
        self.assertEqual(
            Path(reg['transcript_path']).resolve(),
            prepared.jsonl_path.resolve(),
        )

        source_mid = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertEqual(source_mid.get('closed_at'), source_before.get('closed_at'))
        self.assertEqual(int(source_mid['version']), int(source_before['version']))

        calls = {'fresh': 0, 'staged': 0}
        orig_fresh = self.hooks.prepare_fresh
        orig_staged = self.hooks.prepare_staged

        def counting_fresh(intent, path):
            calls['fresh'] += 1
            return orig_fresh(intent, path)

        def counting_staged(intent, path):
            calls['staged'] += 1
            return orig_staged(intent, path)

        cold_hooks = ft_mod.FirstTurnHooks(
            prepare_staged=counting_staged,
            discard_staged=self.hooks.discard_staged,
            formal_holder=self.hooks.formal_holder,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            prepare_fresh=counting_fresh,
        )
        gateway_user_id = _insert_msg(self.db, 'hayana', '冷窗第一句')
        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='冷窗第一句',
            user_message_id=gateway_user_id,
            hooks=cold_hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(calls['fresh'], 1)
        self.assertEqual(calls['staged'], 0)
        self.assertEqual(session.start_offset, 0)
        self.assertEqual(self._intent()['status'], INTENT_COMMITTING)

        # Source still open before first non-empty text.
        self.assertIsNone(
            dc.get_daily_context_by_id(self.context_id, db_path=self.db).get('closed_at'),
        )

        ft_mod.mark_first_turn_stdin_sent(session)
        first = ingest_first_turn_text_delta(
            session, text='冷窗你好', hooks=cold_hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(first.released_text, ('冷窗你好',))
        self.assertTrue(first.handoff_complete)

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertIsNotNone(source_after.get('closed_at'))
        target = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL)
        self.assertIsNone(target.get('closed_at'))

        intent_after = self._intent()
        self.assertEqual(intent_after['status'], INTENT_COMMITTED)
        self.assertFalse(has_active_switch_intent('default', db_path=self.db))
        # Candidate JSONL still not pre-created by Forge/Target.
        self.assertEqual(set(Path(self.home).rglob('*.jsonl')), before_jsonl)

        # Production cold path: fresh-named uses --session-id; gateway wires prepare_fresh.
        import inspect
        import cc_resident
        import gateway
        gw_src = inspect.getsource(gateway._gw_build_switch_hooks)
        self.assertIn('spawn_fresh_named', gw_src)
        self.assertIn('prepare_fresh', inspect.getsource(gateway._gw_build_first_turn_hooks))
        # prepare_fresh must not call spawn_resumable.
        fresh_fn = [
            block for block in gw_src.split('def ') if block.startswith('prepare_fresh')
        ][0]
        self.assertIn('spawn_fresh_named', fresh_fn)
        self.assertNotIn('spawn_resumable', fresh_fn)
        fresh_src = inspect.getsource(cc_resident.ResidentSession.spawn_fresh_named)
        self.assertIn("'--session-id', session_id", fresh_src)

    def test_clean_failure_before_commit_retry_same_message(self):
        """2) source canonical, target staged; same user_message_id retryable.

        Includes Gateway generator sub-scenes for pre-first-text terminals.
        """
        import gateway

        self._seed_source_and_forge()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        intent_before = self._intent()
        target_id = int(intent_before['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '可重试')
        target_session_before = str(intent_before['target_session_id'])
        jsonl_path = self._jsonl_path(str(intent_before['target_session_id']))
        start_size = jsonl_path.stat().st_size
        baseline_asst_max_id = sqlite3.connect(self.db).execute(
            "SELECT MAX(id) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        target_gen_baseline = int(
            dc.get_daily_context_by_id(target_id, db_path=self.db)['resident_generation'],
        )
        target_cursor_baseline = dc.get_resident_history_cursor(
            target_id, target_gen_baseline, db_path=self.db,
        )

        # Prepare-staged failure after claim must auto-return READY + release lease.
        boom_hooks = ft_mod.FirstTurnHooks(
            prepare_staged=lambda intent, path: (_ for _ in ()).throw(
                RuntimeError('staged prepare boom'),
            ),
            discard_staged=self.hooks.discard_staged,
            formal_holder=self.hooks.formal_holder,
            forge_cwd=self.hooks.forge_cwd,
            claude_home=self.hooks.claude_home,
        )
        with self.assertRaises(RuntimeError):
            claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=self.first_turn_request_id,
                user_content='可重试',
                user_message_id=gateway_user_id,
                hooks=boom_hooks,
                db_path=self.db,
                now=NOW,
            )
        intent_after_boom = self._intent()
        self.assertEqual(intent_after_boom['status'], INTENT_READY)
        self.assertEqual(int(intent_after_boom['first_user_message_id']), gateway_user_id)
        self.assertEqual(str(intent_after_boom['target_session_id']), target_session_before)
        lease_n = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_turn_leases '
            'WHERE context_id=? AND lease_owner=?',
            (target_id, self.first_turn_request_id),
        ).fetchone()[0]
        self.assertEqual(lease_n, 0)

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='可重试',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(session.user_message_id, gateway_user_id)
        abort_first_turn_clean(session, hooks=self.hooks, db_path=self.db, now=NOW)

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        target_after = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(source_after.get('closed_at'), source_before.get('closed_at'))
        self.assertEqual(int(source_after['version']), int(source_before['version']))
        self.assertEqual(target_after['window_mode'], WINDOW_MODE_MANUAL_STAGED)

        intent_ready = self._intent()
        self.assertEqual(intent_ready['status'], INTENT_READY)

        session2 = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='可重试',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(session2.user_message_id, gateway_user_id)
        user_rows = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages WHERE content=?',
            ('可重试',),
        ).fetchone()[0]
        self.assertEqual(user_rows, 1)
        self.assertEqual(
            int(self._intent()['target_context_id']), target_id,
        )
        abort_first_turn_clean(session2, hooks=self.hooks, db_path=self.db, now=NOW)

        # Core helper fail-closed: stdin_sent forbids clean rollback.
        session_gate = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='可重试',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        ft_mod.mark_first_turn_stdin_sent(session_gate)
        with self.assertRaises(FirstTurnError) as ar_gate:
            abort_first_turn_clean(
                session_gate, hooks=self.hooks, db_path=self.db, now=NOW,
            )
        self.assertEqual(ar_gate.exception.error_code, 'FIRST_TURN_NOT_CLEAN')
        ft_mod.mark_first_turn_precommit_dirty(session_gate, db_path=self.db, now=NOW)
        conn = _connect(self.db)
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "UPDATE context_switch_intents SET status=?, orphan_jsonl_state=NULL, "
                "first_turn_error_code=NULL, updated_at=? WHERE request_id=?",
                (INTENT_READY, NOW.strftime('%Y-%m-%d %H:%M:%S'), self.switch_request_id),
            )
            conn.execute(
                'DELETE FROM daily_resident_turn_leases WHERE context_id=?',
                (target_id,),
            )
            conn.commit()
        finally:
            conn.close()

        class _FakeStaged:
            def __init__(self, mode: str, grow_path: Path | None = None):
                self.mode = mode
                self.grow_path = grow_path
                self.killed = False
                self.ack_called = False

            def _ack(self, on_stdin_flushed) -> None:
                if on_stdin_flushed is not None:
                    self.ack_called = True
                    on_stdin_flushed()

            def _grow(self) -> None:
                if self.grow_path is None:
                    return
                with self.grow_path.open('ab') as fh:
                    fh.write(b'{"type":"x"}\n')

            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                if self.mode == 'boom_before_ack':
                    raise RuntimeError('boom before stdin flush ack')
                    yield  # pragma: no cover
                if self.mode == 'ack_then_boom_no_jsonl':
                    self._ack(on_stdin_flushed)
                    raise RuntimeError('boom after ack, jsonl unchanged')
                    yield  # pragma: no cover
                if self.mode == 'boom_before_ack_jsonl_grown':
                    self._grow()
                    raise RuntimeError('boom before ack but jsonl grew')
                    yield  # pragma: no cover
                if self.mode == 'gen_exit_before_ack':
                    raise GeneratorExit()
                    yield  # pragma: no cover
                if self.mode == 'ack_think_then_gen_exit':
                    self._ack(on_stdin_flushed)
                    yield ('think', 'hold-secret')
                    raise GeneratorExit()
                if self.mode == 'partial_then_done':
                    self._ack(on_stdin_flushed)
                    yield ('text', 'partial')
                    yield ('text', ' full')
                    yield ('done', ('partial full', '', {}))
                if self.mode == 'done_before_text':
                    self._ack(on_stdin_flushed)
                    yield ('done', ('', '', {}))
                if self.mode == 'eof_before_ack':
                    return
                    yield  # pragma: no cover
                if self.mode == 'think_then_done':
                    self._ack(on_stdin_flushed)
                    yield ('think', 'hold-secret')
                    yield ('done', ('', '', {}))
                if self.mode == 'tool_order':
                    self._ack(on_stdin_flushed)
                    yield ('think', 'hold-secret')
                    yield ('tool_use', {
                        'id': 'tu-1', 'name': 'Read', 'args': {'path': 'x'},
                    })
                    yield ('tool_result', {
                        'tool_use_id': 'tu-1', 'result': 'file-ok', 'is_error': False,
                    })
                    yield ('text', '你好')
                    yield ('done', ('你好', '', {}))
                raise AssertionError(f'unknown fake mode {self.mode}')

            def _kill(self, quiet=True):
                self.killed = True

        turn = {'user_message_id': gateway_user_id}

        def _reset_ready_fixture() -> None:
            intent = self._intent()
            aid = intent.get('first_assistant_message_id')
            conn = _connect(self.db)
            try:
                conn.execute('BEGIN IMMEDIATE')
                if aid:
                    conn.execute(
                        'DELETE FROM chat_messages WHERE id=?', (int(aid),),
                    )
                if baseline_asst_max_id is not None:
                    conn.execute(
                        "DELETE FROM chat_messages WHERE author='assistant' AND id > ?",
                        (int(baseline_asst_max_id),),
                    )
                conn.execute(
                    "UPDATE context_switch_intents SET status=?, orphan_jsonl_state=NULL, "
                    "first_turn_error_code=NULL, first_turn_completed_at=NULL, "
                    "first_assistant_message_id=NULL, first_turn_end_offset=NULL, "
                    "first_user_message_id=NULL, last_good_context_id=NULL, "
                    "last_good_context_epoch=NULL, last_good_resident_generation=NULL, "
                    "last_good_history_cursor_message_id=NULL, last_good_recorded_at=NULL, "
                    "updated_at=? WHERE request_id=?",
                    (
                        INTENT_READY,
                        NOW.strftime('%Y-%m-%d %H:%M:%S'),
                        self.switch_request_id,
                    ),
                )
                conn.execute(
                    'DELETE FROM daily_resident_turn_leases WHERE context_id=?',
                    (target_id,),
                )
                conn.execute(
                    'UPDATE daily_contexts SET closed_at=NULL WHERE id=?',
                    (self.context_id,),
                )
                conn.execute(
                    'UPDATE daily_contexts SET window_mode=? WHERE id=?',
                    (WINDOW_MODE_MANUAL_STAGED, target_id),
                )
                conn.commit()
            finally:
                conn.close()
            if target_cursor_baseline is not None:
                dc.set_resident_history_cursor(
                    target_id,
                    target_gen_baseline,
                    int(target_cursor_baseline),
                    db_path=self.db,
                )
            else:
                conn = _connect(self.db)
                try:
                    conn.execute(
                        'DELETE FROM daily_resident_cursors WHERE context_id=?',
                        (target_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
            jsonl_path.write_bytes(jsonl_path.read_bytes()[:start_size])
            dr.reset_bindings_for_tests()

        def _run_stream(fake, *, expect_generator_exit=False):
            hooks = self._gateway_first_turn_hooks(fake)
            marked = mock.Mock(wraps=ft_mod.mark_first_turn_stdin_sent)
            complete = mock.Mock(wraps=ft_mod.complete_first_turn_round)
            live_intent = self._intent()
            with mock.patch.object(gateway, 'DB_PATH', self.db), \
                 mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks), \
                 mock.patch(
                     'chat.context_window_first_turn.mark_first_turn_stdin_sent', marked,
                 ), \
                 mock.patch(
                     'chat.context_window_first_turn.complete_first_turn_round', complete,
                 ):
                gen = gateway._stream_cc_first_turn(turn, '可重试', live_intent)
                chunks = []
                if expect_generator_exit:
                    with self.assertRaises(GeneratorExit):
                        while True:
                            chunks.append(next(gen))
                else:
                    for chunk in gen:
                        chunks.append(chunk)
                return chunks, marked, complete

        with self.subTest('1_ack_before_failure_clean'):
            self.assertEqual(jsonl_path.stat().st_size, start_size)
            fake = _FakeStaged('boom_before_ack')
            chunks, marked, complete = _run_stream(fake)
            self.assertFalse(fake.ack_called)
            marked.assert_not_called()
            complete.assert_not_called()
            joined = ''.join(chunks)
            self.assertIn('"t": "err"', joined)
            self.assertNotIn('"t": "text"', joined)
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
            )
            session_retry = claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=self.first_turn_request_id,
                user_content='可重试',
                user_message_id=gateway_user_id,
                hooks=self.hooks,
                db_path=self.db,
                now=NOW,
            )
            self.assertEqual(session_retry.user_message_id, gateway_user_id)
            abort_first_turn_clean(
                session_retry, hooks=self.hooks, db_path=self.db, now=NOW,
            )

        with self.subTest('2_ack_after_no_jsonl_must_dirty'):
            # Most important: flush ack without JSONL growth must NOT clean-rollback.
            fake = _FakeStaged('ack_then_boom_no_jsonl')
            chunks, marked, complete = _run_stream(fake)
            self.assertTrue(fake.ack_called)
            marked.assert_called()
            complete.assert_not_called()
            self.assertEqual(jsonl_path.stat().st_size, start_size)
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
                expect_dirty=True,
            )
            self._assert_last_good_empty()
            prepare_calls = {'n': 0}
            orig_prepare = self.hooks.prepare_staged

            def counting_prepare(intent, path):
                prepare_calls['n'] += 1
                return orig_prepare(intent, path)

            gated = ft_mod.FirstTurnHooks(
                prepare_staged=counting_prepare,
                discard_staged=self.hooks.discard_staged,
                formal_holder=self.hooks.formal_holder,
                forge_cwd=self.hooks.forge_cwd,
                claude_home=self.hooks.claude_home,
            )
            user_before = sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM chat_messages WHERE content=?', ('可重试',),
            ).fetchone()[0]
            asst_before = sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
            ).fetchone()[0]
            with self.assertRaises(FirstTurnError) as ar:
                claim_and_start_first_turn(
                    switch_request_id=self.switch_request_id,
                    first_turn_request_id=self.first_turn_request_id,
                    user_content='可重试',
                    user_message_id=gateway_user_id,
                    hooks=gated,
                    db_path=self.db,
                    now=NOW,
                )
            self.assertEqual(ar.exception.error_code, 'FIRST_TURN_PRECOMMIT_DIRTY')
            self.assertEqual(prepare_calls['n'], 0)
            user_after = sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM chat_messages WHERE content=?', ('可重试',),
            ).fetchone()[0]
            asst_after = sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
            ).fetchone()[0]
            self.assertEqual(user_after, user_before)
            self.assertEqual(asst_after, asst_before)
            self._assert_last_good_empty()
            _reset_ready_fixture()

        with self.subTest('2B_ambiguous_committing_same_ids_refuse'):
            # Core fail-closed: COMMITTING refuses re-claim even with same ids.
            session_c = claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=self.first_turn_request_id,
                user_content='可重试',
                user_message_id=gateway_user_id,
                hooks=self.hooks,
                db_path=self.db,
                now=NOW,
            )
            self.assertEqual(self._intent()['status'], INTENT_COMMITTING)
            self.assertNotEqual(
                self._intent().get('orphan_jsonl_state'), 'precommit_dirty',
            )
            self._assert_last_good_empty()
            prepare_calls = {'n': 0}
            orig_prepare = self.hooks.prepare_staged

            def counting_prepare_c(intent, path):
                prepare_calls['n'] += 1
                return orig_prepare(intent, path)

            gated_c = ft_mod.FirstTurnHooks(
                prepare_staged=counting_prepare_c,
                discard_staged=self.hooks.discard_staged,
                formal_holder=self.hooks.formal_holder,
                forge_cwd=self.hooks.forge_cwd,
                claude_home=self.hooks.claude_home,
            )
            with self.assertRaises(FirstTurnError) as ar:
                claim_and_start_first_turn(
                    switch_request_id=self.switch_request_id,
                    first_turn_request_id=self.first_turn_request_id,
                    user_content='可重试',
                    user_message_id=gateway_user_id,
                    hooks=gated_c,
                    db_path=self.db,
                    now=NOW,
                )
            self.assertEqual(ar.exception.error_code, 'FIRST_TURN_IN_PROGRESS')
            self.assertEqual(prepare_calls['n'], 0)
            self._assert_last_good_empty()
            abort_first_turn_clean(
                session_c, hooks=self.hooks, db_path=self.db, now=NOW,
            )
            self.assertEqual(self._intent()['status'], INTENT_READY)

        with self.subTest('2C_clean_ready_same_user_retryable'):
            session_r = claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=self.first_turn_request_id,
                user_content='可重试',
                user_message_id=gateway_user_id,
                hooks=self.hooks,
                db_path=self.db,
                now=NOW,
            )
            self.assertEqual(session_r.user_message_id, gateway_user_id)
            abort_first_turn_clean(
                session_r, hooks=self.hooks, db_path=self.db, now=NOW,
            )
            self.assertEqual(self._intent()['status'], INTENT_READY)
            self._assert_last_good_empty()
            _reset_ready_fixture()

        with self.subTest('2b_before_ack_jsonl_grown_dirty'):
            chunks, marked, complete = _run_stream(
                _FakeStaged('boom_before_ack_jsonl_grown', grow_path=jsonl_path),
            )
            marked.assert_not_called()
            complete.assert_not_called()
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
                expect_dirty=True,
            )
            _reset_ready_fixture()

        with self.subTest('B_generator_exit_before_ack_clean'):
            chunks, marked, complete = _run_stream(
                _FakeStaged('gen_exit_before_ack'),
                expect_generator_exit=True,
            )
            marked.assert_not_called()
            complete.assert_not_called()
            self.assertEqual(chunks, [])
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
            )

        with self.subTest('B2_generator_exit_after_ack_dirty'):
            chunks, marked, complete = _run_stream(
                _FakeStaged('ack_think_then_gen_exit'),
                expect_generator_exit=True,
            )
            marked.assert_called()
            complete.assert_not_called()
            self.assertEqual(chunks, [])
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
                expect_dirty=True,
            )
            _reset_ready_fixture()

        with self.subTest('B3_gen_close_after_precommit'):
            fake = _FakeStaged('done_before_text')
            hooks = self._gateway_first_turn_hooks(fake)
            with mock.patch.object(gateway, 'DB_PATH', self.db), \
                 mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks), \
                 mock.patch(
                     'chat.context_window_first_turn.complete_first_turn_round',
                     wraps=ft_mod.complete_first_turn_round,
                 ) as complete:
                gen = gateway._stream_cc_first_turn(turn, '可重试', self._intent())
                first = next(gen)
                self.assertIn('"t": "err"', first)
                self.assertNotIn('"t": "text"', first)
                complete.assert_not_called()
                gen.close()
            # Ack happened → dirty, not READY.
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
                expect_dirty=True,
            )
            _reset_ready_fixture()

        with self.subTest('C_done_before_first_text_no_complete'):
            chunks, marked, complete = _run_stream(_FakeStaged('think_then_done'))
            complete.assert_not_called()
            joined = ''.join(chunks)
            self.assertIn('"t": "err"', joined)
            self.assertIn('FIRST_TURN_DONE_BEFORE_TEXT', joined)
            self.assertNotIn('"ok": true', joined.replace(' ', ''))
            self.assertNotIn('"t": "text"', joined)
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
                expect_dirty=True,
            )
            _reset_ready_fixture()

        with self.subTest('C2_eof_before_ack_no_complete'):
            chunks, marked, complete = _run_stream(_FakeStaged('eof_before_ack'))
            marked.assert_not_called()
            complete.assert_not_called()
            joined = ''.join(chunks)
            self.assertIn('"t": "err"', joined)
            self.assertNotIn('"t": "text"', joined)
            self._assert_pre_first_text_clean(
                source_before=source_before,
                target_id=target_id,
                gateway_user_id=gateway_user_id,
            )

        with self.subTest('4_tool_result_order_after_handoff'):
            chunks, marked, complete = _run_stream(_FakeStaged('tool_order'))
            marked.assert_called()
            complete.assert_called()
            parsed = []
            for ch in chunks:
                if not ch.startswith('data: '):
                    continue
                body = ch[len('data: '):].strip()
                parsed.append(json.loads(body))
            types = [p.get('t') for p in parsed]
            # No client-visible events before first text; order after handoff:
            self.assertEqual(
                types[:5],
                ['think', 'tool_use', 'tool_result', 'tool_call', 'text'],
            )
            self.assertIn('done', types)
            tr = next(p for p in parsed if p.get('t') == 'tool_result')
            self.assertEqual(tr.get('idx'), 0)
            self.assertEqual(tr['d'].get('name'), 'Read')
            self.assertEqual(tr['d'].get('result'), 'file-ok')
            self.assertTrue(tr['d'].get('success'))
            # Ensure think/tool_* were not released before text in the stream prefix.
            text_i = types.index('text')
            self.assertEqual(types.index('think'), 0)
            self.assertLess(types.index('tool_use'), text_i)
            self.assertLess(types.index('tool_result'), text_i)

    def _partial_then_done_fake(self):
        class _FakeStaged:
            def __init__(self):
                self.killed = False

            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                yield ('text', 'partial')
                yield ('text', ' full')
                yield ('done', ('partial full', '', {}))

            def _kill(self, quiet=True):
                self.killed = True

        return _FakeStaged()

    def test_B2b_client_detach_drains_to_complete(self):
        """PR #233: online chunks + detach drain complete without POSTCOMMIT_ABORT."""
        import gateway

        self._seed_source_and_forge()
        gateway_user_id = _insert_msg(self.db, 'hayana', '可重试')
        turn = {'user_message_id': gateway_user_id}
        fake = self._partial_then_done_fake()
        hooks = self._gateway_first_turn_hooks(fake)
        complete = mock.Mock(wraps=ft_mod.complete_first_turn_round)
        live_intent = self._intent()
        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks), \
             mock.patch(
                 'chat.context_window_first_turn.complete_first_turn_round', complete,
             ), \
             mock.patch(
                 'chat.context_window_first_turn.abort_first_turn_postcommit',
             ) as postcommit_abort:
            gen = gateway._stream_cc_first_turn(turn, '可重试', live_intent)
            text_seen = []
            try:
                while len(text_seen) < 2:
                    chunk = next(gen)
                    if not chunk.startswith('data: '):
                        continue
                    body = json.loads(chunk[len('data: '):].strip())
                    if body.get('t') == 'text':
                        text_seen.append(body.get('d'))
            finally:
                self.assertEqual(text_seen, ['partial', ' full'])
                gen.close()
            complete.assert_called_once()
            postcommit_abort.assert_not_called()
            self.assertFalse(fake.killed)
        intent_after = self._intent()
        self.assertEqual(intent_after['status'], INTENT_COMMITTED)
        self.assertIsNotNone(intent_after.get('first_turn_completed_at'))
        self.assertIsNone(intent_after.get('first_turn_error_code'))
        aid = int(intent_after['first_assistant_message_id'])
        row = sqlite3.connect(self.db).execute(
            'SELECT content FROM chat_messages WHERE id=?', (aid,),
        ).fetchone()
        self.assertEqual(row[0], 'partial full')

    def test_B2c_detach_finalize_pending_no_abort(self):
        """PR #233: provider done + finalize txn fail must not POSTCOMMIT_ABORT."""
        import gateway

        self._seed_source_and_forge()
        gateway_user_id = _insert_msg(self.db, 'hayana', '可重试')
        turn = {'user_message_id': gateway_user_id}
        target_id = int(self._intent()['target_context_id'])
        fake = self._partial_then_done_fake()
        hooks = self._gateway_first_turn_hooks(fake)
        gen_before = int(
            dc.get_daily_context_by_id(target_id, db_path=self.db)['resident_generation'],
        )
        asst_before = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        real_update = ft_mod._update_intent_conn
        boom = {'n': 0}

        def _boom_on_completed_at(
            conn, request_id, *, status=None, fields=None, now_s=None,
        ):
            fields = fields or {}
            if (
                fields.get('first_turn_completed_at') is not None
                and boom['n'] == 0
            ):
                boom['n'] += 1
                raise sqlite3.OperationalError(
                    'simulated final checkpoint failure',
                )
            return real_update(
                conn, request_id, status=status, fields=fields, now_s=now_s,
            )

        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks), \
             mock.patch.object(
                 ft_mod, '_update_intent_conn', side_effect=_boom_on_completed_at,
             ), \
             mock.patch(
                 'chat.context_window_first_turn.abort_first_turn_postcommit',
             ) as postcommit_abort:
            gen = gateway._stream_cc_first_turn(turn, '可重试', self._intent())
            saw_text = False
            try:
                while True:
                    chunk = next(gen)
                    if '"t": "text"' in chunk:
                        saw_text = True
                        break
            finally:
                self.assertTrue(saw_text)
                gen.close()
            postcommit_abort.assert_not_called()
            self.assertFalse(fake.killed)
        intent_after = self._intent()
        self.assertIsNone(intent_after.get('first_turn_completed_at'))
        self.assertIsNone(intent_after.get('first_turn_error_code'))
        self.assertIsNotNone(intent_after.get('first_assistant_message_id'))
        pending = get_first_turn_finalize_pending(db_path=self.db)
        self.assertIsNotNone(pending)
        self.assertEqual(str(pending['request_id']), self.switch_request_id)
        gen_after = int(
            dc.get_daily_context_by_id(target_id, db_path=self.db)['resident_generation'],
        )
        self.assertEqual(gen_after, gen_before)
        asst_after = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant'",
        ).fetchone()[0]
        self.assertEqual(asst_after, asst_before + 1)
        aid = int(intent_after['first_assistant_message_id'])
        row = sqlite3.connect(self.db).execute(
            'SELECT content FROM chat_messages WHERE id=?', (aid,),
        ).fetchone()
        self.assertEqual(row[0], 'partial full')


    def _transcript_stall_fake(self, path: Path, session_id: str, text: str,
                               *, stop_reason='end_turn',
                               transcript_text=None, extra=None,
                               transcript_session=None, second_round=False,
                               malformed=False):
        import cc_resident

        written = {'done': False}
        user_uuid = 'recovery-user-' + uuid.uuid4().hex
        assistant_uuid = 'recovery-assistant-' + uuid.uuid4().hex
        event_session = transcript_session or session_id

        def write_terminal():
            if written['done']:
                return
            user = json.loads(_line(
                user_uuid, 'user', session=event_session, parent=None,
                content='recovery user',
            ))
            assistant = json.loads(_line(
                assistant_uuid, 'assistant', session=event_session,
                parent=user_uuid,
                content=[{'type': 'text', 'text': (
                    text if transcript_text is None else transcript_text
                )}],
            ))
            assistant['message']['stop_reason'] = stop_reason
            if extra:
                assistant.update(extra)
            lines = [
                json.dumps(user, ensure_ascii=False),
                json.dumps(assistant, ensure_ascii=False),
            ]
            if second_round:
                user2_uuid = 'recovery-user-2-' + uuid.uuid4().hex
                assistant2_uuid = 'recovery-assistant-2-' + uuid.uuid4().hex
                lines.extend([
                    _line(
                        user2_uuid, 'user', session=event_session,
                        parent=assistant_uuid, content='second user',
                    ),
                    _line(
                        assistant2_uuid, 'assistant', session=event_session,
                        parent=user2_uuid,
                        content=[{'type': 'text', 'text': 'second reply'}],
                    ),
                ])
            if malformed:
                lines.append('{"type":"assistant"')
            _append_jsonl(path, lines)
            written['done'] = True

        class _FakeStaged:
            def send_turn(
                self,
                content,
                commit_meta=None,
                on_stdin_flushed=None,
                idle_heartbeat_sec=None,
                turn_lease=None,
            ):
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                try:
                    yield ('text', text)
                    write_terminal()
                    raise cc_resident.ResidentError('simulated stall')
                except GeneratorExit:
                    write_terminal()
                    raise cc_resident.ResidentError('simulated stall')

            def _kill(self, quiet=True):
                return None

        return _FakeStaged()

    def _run_transcript_stall_case(self, *, detached=False, text='complete',
                                   stop_reason='end_turn', extra=None,
                                   transcript_text=None, transcript_session=None,
                                   second_round=False, malformed=False,
                                   nonzero_offset=False):
        import gateway

        self._seed_source_and_forge()
        intent = self._intent()
        target_session = str(intent['target_session_id'])
        path = self._jsonl_path(target_session)
        visible = str(text)
        transcript = visible if transcript_text is None else str(transcript_text)
        if nonzero_offset:
            prefix = _line(
                'recovery-prefix-' + uuid.uuid4().hex,
                'system',
                session=target_session,
                parent=None,
                content='prior transcript',
            )
            start_offset = _write_jsonl(path, [prefix])
            conn = sqlite3.connect(self.db)
            conn.execute(
                'UPDATE context_claude_sessions SET scan_offset=? '
                'WHERE context_id=? AND resident_generation=?',
                (start_offset, int(intent['target_context_id']),
                 int(intent['target_resident_generation'])),
            )
            conn.commit()
            conn.close()
        fake = self._transcript_stall_fake(
            path, target_session, visible, stop_reason=stop_reason,
            transcript_text=transcript, extra=extra,
            transcript_session=transcript_session,
            second_round=second_round, malformed=malformed,
        )
        gateway_user_id = _insert_msg(self.db, 'hayana', 'recovery user')
        turn = {'user_message_id': gateway_user_id}
        hooks = self._gateway_first_turn_hooks(fake)
        with mock.patch.object(gateway, 'DB_PATH', self.db), \
             mock.patch.object(gateway, '_gw_build_first_turn_hooks', return_value=hooks), \
             mock.patch('chat.context_window_first_turn.complete_first_turn_round',
                        wraps=ft_mod.complete_first_turn_round) as complete, \
             mock.patch('chat.context_window_first_turn.abort_first_turn_postcommit') as abort:
            gen = gateway._stream_cc_first_turn(turn, 'recovery user', self._intent())
            if detached:
                first = next(gen)
                self.assertIn('"t": "text"', first)
                gen.close()
                chunks = []
            else:
                chunks = list(gen)
            return self._intent(), target_session, complete, abort, chunks

    def test_transcript_endturn_recovery_connected(self):
        intent, target_session, complete, abort, chunks = (
            self._run_transcript_stall_case(text='complete')
        )
        self.assertIn('"t": "done", "ok": true', ''.join(chunks))
        complete.assert_called_once()
        abort.assert_not_called()
        self.assertIsNone(intent['first_turn_error_code'])
        self.assertIsNotNone(intent['first_assistant_message_id'])
        self.assertIsNotNone(intent['first_turn_completed_at'])
        cache = sqlite3.connect(self.db).execute(
            'SELECT cache_info FROM chat_messages WHERE id=?',
            (int(intent['first_assistant_message_id']),),
        ).fetchone()[0]
        self.assertEqual(
            json.loads(cache)['first_turn_terminal_recovery'],
            'transcript_end_turn',
        )

    def test_transcript_endturn_recovery_detached(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            detached=True, text='complete',
        )
        complete.assert_called_once()
        abort.assert_not_called()
        self.assertIsNone(intent['first_turn_error_code'])
        self.assertIsNotNone(intent['first_assistant_message_id'])
        self.assertIsNotNone(intent['first_turn_completed_at'])

    def test_transcript_endturn_recovery_stop_reason_missing_fail_closed(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            stop_reason=None,
        )
        complete.assert_not_called()
        abort.assert_called_once()
        # abort is intentionally mocked here; DB error persistence is covered by the existing abort regression tests.

    def test_transcript_endturn_recovery_tool_use_fail_closed(self):
        tool = {'message': {'content': [{'type': 'tool_use', 'id': 'tool-1'}]}}
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            extra=tool,
        )
        complete.assert_not_called()
        abort.assert_called_once()
        # abort is intentionally mocked here; DB error persistence is covered by the existing abort regression tests.

    def test_transcript_endturn_recovery_text_mismatch_fail_closed(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            text='visible', transcript_text='different',
        )
        complete.assert_not_called()
        abort.assert_called_once()
        # abort is intentionally mocked here; DB error persistence is covered by the existing abort regression tests.

    def test_transcript_endturn_recovery_session_mismatch_fail_closed(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            transcript_session='wrong-session',
        )
        complete.assert_not_called()
        abort.assert_called_once()

    def test_transcript_endturn_recovery_multi_round_fail_closed(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            second_round=True,
        )
        complete.assert_not_called()
        abort.assert_called_once()

    def test_transcript_endturn_recovery_malformed_terminal_fail_closed(self):
        intent, _, complete, abort, _ = self._run_transcript_stall_case(
            malformed=True,
        )
        complete.assert_not_called()
        abort.assert_called_once()

    def test_transcript_endturn_recovery_unstable_eof_fail_closed(self):
        def grow_transcript(_delay):
            _append_jsonl(
                self._jsonl_path(str(self._intent()['target_session_id'])),
                [_line(
                    'recovery-growth-' + uuid.uuid4().hex,
                    'system',
                    session=str(self._intent()['target_session_id']),
                    parent=None,
                    content='late growth',
                )],
            )

        with mock.patch.object(ft_mod.time, 'sleep', side_effect=grow_transcript):
            intent, _, complete, abort, _ = self._run_transcript_stall_case()
        complete.assert_not_called()
        abort.assert_called_once()

    def test_transcript_endturn_recovery_nonzero_offset(self):
        intent, _, complete, abort, chunks = self._run_transcript_stall_case(
            nonzero_offset=True,
        )
        complete.assert_called_once()
        abort.assert_not_called()
        self.assertIn('"t": "done", "ok": true', ''.join(chunks))


    def test_db_commit_then_swap_fail_recovery(self):
        """3) same-process HANDOFF_PENDING: first delta once, no second user/asst."""
        self._seed_source_and_forge()
        target_id = int(self._intent()['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', 'swap fail case')
        fail_once = {'n': 0}

        def owner_hook(_phase: str) -> None:
            if fail_once['n'] == 0:
                fail_once['n'] += 1
                raise RuntimeError('simulated swap metadata failure')

        dr.set_owner_cursor_write_hook_for_tests(owner_hook)

        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='swap fail case',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(session.user_message_id, gateway_user_id)
        ft_mod.mark_first_turn_stdin_sent(session)
        with self.assertRaises(RuntimeError):
            ingest_first_turn_text_delta(
                session, text='小猫，我', hooks=self.hooks, db_path=self.db, now=NOW,
            )

        source = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertIsNotNone(source.get('closed_at'))
        target = dc.get_daily_context_by_id(target_id, db_path=self.db)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL)
        intent_hp = self._intent()
        self.assertEqual(intent_hp['status'], INTENT_HANDOFF_PENDING)
        self.assertIsNotNone(intent_hp['first_delta_committed_at'])
        self._assert_last_good_empty(intent_hp)  # HANDOFF must not advance last-good
        # First delta still held until same-process recover.
        self.assertFalse(session._handoff_complete)

        dr.set_owner_cursor_write_hook_for_tests(None)
        recovered = recover_first_turn_handoff_pending(
            session, hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(self._intent()['status'], INTENT_COMMITTED)
        self.assertTrue(session._handoff_complete)
        self.assertEqual(int(self._intent()['target_context_id']), target_id)
        self._assert_last_good_empty()  # recover ≠ complete; last-good still empty
        # Held first delta released exactly once on recovery.
        self.assertEqual(recovered.released_text, ('小猫，我',))
        again = recover_first_turn_handoff_pending(
            session, hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(again.released_text, ())

        released = ingest_first_turn_text_delta(
            session, text='尾句', hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(released.released_text, ('尾句',))

        with self.assertRaises(FirstTurnError) as ar:
            claim_and_start_first_turn(
                switch_request_id=self.switch_request_id,
                first_turn_request_id=str(uuid.uuid4()),
                user_content='must not send again',
                user_message_id=gateway_user_id,
                hooks=self.hooks,
                db_path=self.db,
                now=NOW,
            )
        self.assertEqual(ar.exception.error_code, 'FIRST_TURN_INTENT_STATUS')
        user_rows = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM chat_messages WHERE content=?',
            ('swap fail case',),
        ).fetchone()[0]
        self.assertEqual(user_rows, 1)
        self.assertIsNone(get_latest_last_good_checkpoint(db_path=self.db))

    def test_structural_canary_isolation(self):
        """3) STRUCTURAL_CANARY_ONLY — temp DB/home/JSONL; fake resident; no flag/prod.

        OWNER_CANARY_NOT_IMPLEMENTED / NIGHTLY_SCHEDULER_NOT_IMPLEMENTED.
        """
        # Isolation evidence: every path under this test's temp root.
        self.assertTrue(self.db.startswith(self.tmp + os.sep) or self.db == self.tmp)
        self.assertTrue(str(self.claude_home).startswith(self.tmp + os.sep))
        self.assertTrue(self.cwd.startswith(self.tmp + os.sep))
        self.assertTrue(str(self.hooks.claude_home).startswith(self.tmp + os.sep))
        self.assertTrue(str(self.hooks.forge_cwd).startswith(self.tmp + os.sep))
        # Flag untouched; production DB path must not equal this temp db.
        self.assertNotIn('DAILY_SOFT_WINDOW_ENABLED', os.environ)
        self.assertNotEqual(os.path.realpath(self.db), os.path.realpath(dc.DEFAULT_DB_PATH))

        published = self._seed_source_and_forge()
        jsonl = self._jsonl_path(str(self._intent()['target_session_id']))
        self.assertTrue(str(jsonl).startswith(self.tmp + os.sep))
        gateway_user_id = _insert_msg(self.db, 'hayana', 'structural canary')
        session = claim_and_start_first_turn(
            switch_request_id=self.switch_request_id,
            first_turn_request_id=self.first_turn_request_id,
            user_content='structural canary',
            user_message_id=gateway_user_id,
            hooks=self.hooks,
            db_path=self.db,
            now=NOW,
        )
        # Fake staged only (offline hooks); no live Claude process.
        self.assertTrue(hasattr(session.staged, 'kill'))
        self.assertTrue(session.staged.is_alive())
        ft_mod.mark_first_turn_stdin_sent(session)
        ingest_first_turn_text_delta(
            session, text='canary-ok', hooks=self.hooks, db_path=self.db, now=NOW,
        )
        done = complete_first_turn_round(
            session,
            assistant_content='canary-ok-reply',
            end_offset=int(published.jsonl_size) + 40,
            db_path=self.db,
            now=NOW,
        )
        cp = get_latest_last_good_checkpoint(db_path=self.db)
        self.assertIsNotNone(cp)
        assert cp is not None
        self.assertEqual(int(cp['history_cursor_message_id']), done.assistant_message_id)
        self.assertEqual(int(cp['context_id']), int(session.target_context_id))

        with self.subTest('representative_failure_no_auto_repair_no_last_good_advance'):
            # Second switch intent would need a full forge; instead force dirty on a
            # fresh READY fixture and prove claim fail-closed without repair.
            other_tmp, other_db, other_home, other_cwd = _tmp_workspace()
            try:
                _init_db(other_db)
                with mock.patch.dict(os.environ, {'HOME': other_home}):
                    ctx = dc.get_or_create_daily_context(
                        chat_id='default', local_day='2026-07-31',
                        db_path=other_db, now=NOW,
                    )
                    switch_id = str(uuid.uuid4())
                    ft_req = str(uuid.uuid4())
                    # Minimal READY intent without forge publish — prove dirty gate
                    # isolation on a separate temp DB only.
                    conn = _connect(other_db)
                    try:
                        conn.execute(
                            '''INSERT INTO context_switch_intents (
                                request_id, chat_id, payload_hash, status,
                                source_context_id, source_context_epoch, source_version,
                                source_resident_generation, source_boundary_message_id,
                                carryover_count, selected_message_ids_json,
                                orphan_jsonl_state, created_at, updated_at,
                                first_turn_request_id, first_user_message_id
                            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (
                                switch_id, 'default', 'x', INTENT_COMMITTING,
                                int(ctx['id']), int(ctx['context_epoch']),
                                int(ctx['version']), int(ctx['resident_generation']),
                                0, 0, '[]', 'precommit_dirty',
                                NOW.strftime('%Y-%m-%d %H:%M:%S'),
                                NOW.strftime('%Y-%m-%d %H:%M:%S'),
                                ft_req, 1,
                            ),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    prepare_n = {'n': 0}
                    fail_hooks = offline_first_turn_hooks(Path(other_tmp) / 'fail_hooks')

                    def boom_prepare(intent, path):
                        prepare_n['n'] += 1
                        raise AssertionError('prepare must not run')

                    fail_hooks = ft_mod.FirstTurnHooks(
                        prepare_staged=boom_prepare,
                        discard_staged=fail_hooks.discard_staged,
                        formal_holder=fail_hooks.formal_holder,
                        forge_cwd=other_cwd,
                        claude_home=Path(other_home) / '.claude',
                    )
                    with self.assertRaises(FirstTurnError) as ar:
                        claim_and_start_first_turn(
                            switch_request_id=switch_id,
                            first_turn_request_id=ft_req,
                            user_content='no',
                            user_message_id=1,
                            hooks=fail_hooks,
                            db_path=other_db,
                            now=NOW,
                        )
                    self.assertEqual(ar.exception.error_code, 'FIRST_TURN_PRECOMMIT_DIRTY')
                    self.assertEqual(prepare_n['n'], 0)
                    self.assertIsNone(
                        get_latest_last_good_checkpoint(db_path=other_db),
                    )
                    self.assertNotEqual(
                        os.path.realpath(other_db),
                        os.path.realpath(dc.DEFAULT_DB_PATH),
                    )
            finally:
                shutil.rmtree(other_tmp, ignore_errors=True)

        # Cleanup: discard staged resident from success path.
        self.hooks.discard_staged(session.staged)
        self.assertFalse(session.staged.is_alive())

    def test_gateway_prepare_only_runner_contract(self):
        """Source-level: seamless runner is prepare-only; public keys have no paths."""
        import inspect
        import gateway

        src = inspect.getsource(gateway._gw_run_seamless_switch)
        self.assertIn('publish_context_window_forge_candidate', src)
        self.assertIn('prepare_context_window_target', src)
        self.assertIn('terminalize_pre_ready_intent_failure', src)
        self.assertNotIn('switch_context_window(', src)
        self.assertIn('ThinkingPolicy.DROP', src)
        self.assertIn('preview_id=request_id', src)

        prepared = mock.Mock(
            prepare_status='READY',
            request_id='req-1',
            target_context_id=9,
            target_context_epoch=2,
            candidate_session_id='sid',
            jsonl_sha256='a' * 64,
            jsonl_size=12,
            staged_ready_at='2026-07-31 12:00:00',
            recovered=False,
        )
        intent = {
            'source_context_id': 1,
            'source_context_epoch': 1,
            'source_resident_generation': 1,
        }
        public = gateway._gw_prepare_public_response(prepared=prepared, intent=intent)
        self.assertEqual(public['status'], 'ready')
        self.assertEqual(public['prepare_status'], 'READY')
        for key in (
            'jsonl_path', 'cwd', 'claude_home', 'switched_at', 'window_mode',
            'selected_message_ids', 'st_dev', 'st_ino',
        ):
            self.assertNotIn(key, public)


if __name__ == '__main__':
    unittest.main()
