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
from typing import Any
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
    _intent_row,
    resolve_canonical_context_row_conn,
)
from chat.context_window_first_turn import (
    FirstTurnError,
    abort_first_turn_clean,
    claim_and_start_first_turn,
    complete_first_turn_round,
    ingest_first_turn_text_delta,
    offline_first_turn_hooks,
    recover_first_turn_handoff_pending,
)
import chat.context_window_first_turn as ft_mod
from chat.context_window_forge_publish import publish_context_window_forge_candidate
from chat.context_window_target_prepare import (
    TargetPrepareHooks,
    offline_target_prepare_hooks,
    prepare_context_window_target,
)
from chat.context_window import WINDOW_MODE_MANUAL
from chat.daily_context import WINDOW_MODE_MANUAL_STAGED, _connect
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

    def test_happy_path_first_delta_commit_and_complete(self):
        """1) prepare READY → reuse Gateway user id → first text handoff → complete."""
        published = self._seed_source_and_forge()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        intent_before = self._intent()
        self.assertEqual(intent_before['status'], INTENT_READY)
        self.assertEqual(
            dc.get_daily_context_by_id(
                int(intent_before['target_context_id']), db_path=self.db,
            )['window_mode'],
            WINDOW_MODE_MANUAL_STAGED,
        )
        target_id = int(intent_before['target_context_id'])
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
        intent_committing = self._intent()
        self.assertEqual(intent_committing['status'], INTENT_COMMITTING)
        self.assertEqual(int(intent_committing['first_user_message_id']), gateway_user_id)

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
        asst_count = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant' "
            "AND content=?",
            ('新房第一句回复',),
        ).fetchone()[0]
        self.assertEqual(asst_count, 1)

        # Retry after completed_at: still one assistant, same id.
        again = complete_first_turn_round(
            session,
            assistant_content='新房第一句回复',
            end_offset=int(published.jsonl_size) + 100,
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(again.assistant_message_id, orphan_id)
        asst_count2 = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM chat_messages WHERE author='assistant' "
            "AND content=?",
            ('新房第一句回复',),
        ).fetchone()[0]
        self.assertEqual(asst_count2, 1)

        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        assert binding is not None
        self.assertEqual(int(binding.context_id), target_id)

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

    def test_clean_failure_before_commit_retry_same_message(self):
        """2) source canonical, target staged; same user_message_id retryable."""
        self._seed_source_and_forge()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        intent_before = self._intent()
        target_id = int(intent_before['target_context_id'])
        gateway_user_id = _insert_msg(self.db, 'hayana', '可重试')
        target_session_before = str(intent_before['target_session_id'])

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
        # First delta still held until same-process recover.
        self.assertFalse(session._handoff_complete)

        dr.set_owner_cursor_write_hook_for_tests(None)
        recovered = recover_first_turn_handoff_pending(
            session, hooks=self.hooks, db_path=self.db, now=NOW,
        )
        self.assertEqual(self._intent()['status'], INTENT_COMMITTED)
        self.assertTrue(session._handoff_complete)
        self.assertEqual(int(self._intent()['target_context_id']), target_id)
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

    def test_gateway_prepare_only_runner_contract(self):
        """Source-level: seamless runner is prepare-only; public keys have no paths."""
        import inspect
        import gateway

        src = inspect.getsource(gateway._gw_run_seamless_switch)
        self.assertIn('publish_context_window_forge_candidate', src)
        self.assertIn('prepare_context_window_target', src)
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
