"""Focused Target prepare tests — temp SQLite + temp JSONL + fake resident only."""
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
    INTENT_FORGING,
    INTENT_READY,
    INTENT_RELEASED,
    _intent_row,
    resolve_canonical_context_row_conn,
    terminalize_pre_ready_intent_failure,
)
from chat.context_window_forge_publish import publish_context_window_forge_candidate
from chat.context_window_target_prepare import (
    PREPARE_STATUS_ALREADY_READY,
    PREPARE_STATUS_READY,
    TargetPrepareCrash,
    TargetPrepareError,
    offline_target_prepare_hooks,
    prepare_context_window_target,
)
import chat.context_window_target_prepare as prepare_mod
from chat.daily_context import WINDOW_MODE_MANUAL_STAGED, _connect
from chat.session_registry import get_context_claude_session, register_context_claude_session
from tools.cc_jsonl_usage import session_jsonl_path

SESSION_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
NOW = datetime.datetime(2026, 7, 31, 12, 0, 0)


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='cw-target-prep-')
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
    cwd: str = '/tmp/target-prepare-cwd',
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


class ContextWindowTargetPrepareTests(unittest.TestCase):
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
        self.request_id = str(uuid.uuid4())
        self.claude_home = Path(self.home) / '.claude'
        # Build offline staged hooks against the same cwd/claude_home as Forge publish
        # (avoid a second Claude home tree that can trip PREVIEW_CWD_AMBIGUOUS).
        base_hooks = offline_target_prepare_hooks(Path(self.tmp) / 'hooks_logic')
        self._discard_count = {'n': 0}
        real_discard = base_hooks.discard_staged

        def _counting_discard(staged: Any) -> None:
            self._discard_count['n'] += 1
            return real_discard(staged)

        self.hooks = prepare_mod.TargetPrepareHooks(
            prepare_staged=base_hooks.prepare_staged,
            discard_staged=_counting_discard,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
        )
        prepare_mod._after_target_hook = None
        prepare_mod._after_registry_hook = None
        prepare_mod._before_staged_hook = None
        dr.reset_bindings_for_tests()

    def tearDown(self) -> None:
        prepare_mod._after_target_hook = None
        prepare_mod._after_registry_hook = None
        prepare_mod._before_staged_hook = None
        dr.reset_bindings_for_tests()
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jsonl_path(self, session: str = SESSION_A) -> Path:
        path = session_jsonl_path(self.cwd, session, claude_home=str(self.claude_home))
        assert path is not None
        return Path(path)

    def _register_source(self, *, session: str, scan_offset: int):
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            claude_session_id=session,
            cwd=self.cwd,
            source='test_target_prepare',
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

    def _plain_two_rounds(self):
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
        return path, end2

    def _publish(self):
        return publish_context_window_forge_candidate(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=3,
            preview_id=self.request_id,
            request_id=self.request_id,
            thinking_policy=ThinkingPolicy.DROP,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )

    def _intent(self) -> dict[str, Any]:
        conn = _connect(self.db)
        try:
            row = _intent_row(conn, self.request_id)
            assert row is not None
            return row
        finally:
            conn.close()

    def _prepare(self):
        return prepare_context_window_target(
            request_id=self.request_id,
            db_path=self.db,
            hooks=self.hooks,
            now=NOW,
        )

    def test_core_prepare_success(self):
        """1) target + Registry + staged health; source remains canonical."""
        self._plain_two_rounds()
        published = self._publish()
        self.assertEqual(published.publish_status, 'PUBLISHED')
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        owners_before = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_owners'
        ).fetchone()[0]
        cursors_before = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_resident_cursors'
        ).fetchone()[0]
        binding_before = dr.get_local_binding()

        out = self._prepare()
        self.assertEqual(out.prepare_status, PREPARE_STATUS_READY)
        self.assertTrue(out.jsonl_path.is_file())
        self.assertEqual(out.jsonl_sha256, published.jsonl_sha256)
        self.assertEqual(out.jsonl_size, published.jsonl_size)
        self.assertEqual(self._discard_count['n'], 1)

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_READY)
        self.assertEqual(int(intent['target_context_id']), out.target_context_id)
        self.assertIsNotNone(intent['staged_ready_at'])

        again = self._prepare()
        self.assertEqual(again.prepare_status, PREPARE_STATUS_ALREADY_READY)
        self.assertEqual(self._discard_count['n'], 2)

        origin = dc.resolve_or_create_daily_context_for_origin(
            chat_id='default',
            origin_local_day='2026-07-31',
            actual_wall_now=NOW,
            db_path=self.db,
        )
        self.assertEqual(int(origin['id']), self.context_id)
        got = dc.get_or_create_daily_context(
            chat_id='default',
            local_day='2026-07-31',
            db_path=self.db,
            now=NOW,
        )
        self.assertEqual(int(got['id']), self.context_id)

        target = dc.get_daily_context_by_id(out.target_context_id, db_path=self.db)
        self.assertEqual(target['window_mode'], WINDOW_MODE_MANUAL_STAGED)
        self.assertEqual(target['status'], dc.STATUS_PROVISIONAL)
        self.assertEqual(int(target['is_backfill']), 0)
        self.assertEqual(int(target['resident_generation']), 1)
        self.assertEqual(int(target['source_context_id']), self.context_id)
        self.assertEqual(target['switch_request_id'], self.request_id)
        self.assertEqual(target['claude_session_id'], published.candidate_session_id)

        reg = get_context_claude_session(
            out.target_context_id, 1, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['claude_session_id'], published.candidate_session_id)
        self.assertEqual(int(reg['scan_offset']), published.jsonl_size)
        self.assertEqual(reg['source'], 'context_window_forge')
        self.assertEqual(
            Path(reg['transcript_path']).resolve(), out.jsonl_path.resolve(),
        )

        # Source still canonical / formal-current.
        latest = dc.get_latest_active_context('default', db_path=self.db)
        self.assertEqual(int(latest['id']), self.context_id)
        conn = _connect(self.db)
        try:
            canonical = resolve_canonical_context_row_conn(conn, chat_id='default', now=NOW)
        finally:
            conn.close()
        self.assertEqual(int(canonical['id']), self.context_id)

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertEqual(source_after.get('closed_at'), source_before.get('closed_at'))
        self.assertEqual(int(source_after['version']), int(source_before['version']))
        self.assertEqual(int(source_after['context_epoch']), int(source_before['context_epoch']))
        self.assertEqual(
            int(source_after['resident_generation']),
            int(source_before['resident_generation']),
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM daily_resident_owners'
            ).fetchone()[0],
            owners_before,
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM daily_resident_cursors'
            ).fetchone()[0],
            cursors_before,
        )
        self.assertEqual(dr.get_local_binding(), binding_before)

        # Unique staged fence: second staged for same chat must fail closed.
        # (would require another request — covered indirectly by unique index existence)
        idx = sqlite3.connect(self.db).execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_daily_contexts_manual_staged_unique'"
        ).fetchone()
        self.assertIsNotNone(idx)

    def test_same_request_idempotent_recovery(self):
        """2) Crash after target/registry; retry fills gaps without duplicates."""
        self._plain_two_rounds()
        published = self._publish()

        def crash_after_target():
            raise TargetPrepareCrash('crash after target')

        prepare_mod._after_target_hook = crash_after_target
        with self.assertRaises(TargetPrepareCrash):
            self._prepare()
        prepare_mod._after_target_hook = None

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_FORGING)
        self.assertIsNotNone(intent['target_context_id'])
        tid1 = int(intent['target_context_id'])
        targets = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual_staged'"
        ).fetchone()[0]
        self.assertEqual(targets, 1)
        self.assertIsNone(
            get_context_claude_session(tid1, 1, db_path=self.db),
        )

        # Registry-only gap fill.
        out = self._prepare()
        self.assertEqual(out.prepare_status, PREPARE_STATUS_READY)
        self.assertEqual(out.target_context_id, tid1)
        self.assertTrue(out.recovered)
        targets2 = sqlite3.connect(self.db).execute(
            "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual_staged'"
        ).fetchone()[0]
        self.assertEqual(targets2, 1)
        reg_count = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM context_claude_sessions WHERE context_id=?',
            (tid1,),
        ).fetchone()[0]
        self.assertEqual(reg_count, 1)

        # Crash after registry, then retry — still one target / one registry.
        # Reset intent to forging with target retained to simulate mid-prepare.
        # Instead: crash after registry on a fresh request path by deleting ready
        # and re-running with crash hook from forging+target+registry state.
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE context_switch_intents SET status='forging', "
            "staged_ready_at=NULL, updated_at=? WHERE request_id=?",
            ('2026-07-31 12:00:01', self.request_id),
        )
        conn.commit()
        conn.close()

        def crash_after_registry():
            raise TargetPrepareCrash('crash after registry')

        prepare_mod._after_registry_hook = crash_after_registry
        with self.assertRaises(TargetPrepareCrash):
            self._prepare()
        prepare_mod._after_registry_hook = None

        out2 = self._prepare()
        self.assertIn(
            out2.prepare_status,
            (PREPARE_STATUS_READY, PREPARE_STATUS_ALREADY_READY),
        )
        self.assertEqual(out2.target_context_id, tid1)
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual_staged'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM context_claude_sessions WHERE context_id=?',
                (tid1,),
            ).fetchone()[0],
            1,
        )
        # Candidate file preserved.
        self.assertTrue(Path(published.jsonl_path).is_file())
        self.assertEqual(
            hashlib.sha256(Path(published.jsonl_path).read_bytes()).hexdigest(),
            published.jsonl_sha256,
        )

    def test_staged_health_failure_safe_stop(self):
        """3) Staged health fails → cleanup target/registry; source continues; file kept."""
        self._plain_two_rounds()
        published = self._publish()
        path = Path(published.jsonl_path)
        original = path.read_bytes()
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)

        def mutate_before_staged():
            # Change inode/content so offline health rejects identity.
            path.unlink()
            path.write_bytes(b'{"type":"mutated"}\n')
            os.chmod(path, 0o600)

        # Restore file for verify at start; mutate only right before staged.
        # prepare verifies twice — first verify must pass, so mutate in before_staged.
        def swap_back_then_mutate_setup():
            pass

        # Keep original for first verify; mutate in _before_staged_hook.
        path.write_bytes(original)
        os.chmod(path, 0o600)

        prepare_mod._before_staged_hook = mutate_before_staged
        with self.assertRaises(TargetPrepareError) as ctx:
            self._prepare()
        prepare_mod._before_staged_hook = None
        self.assertIn(
            ctx.exception.error_code,
            (
                'TARGET_STAGED_JSONL_MUTATED',
                'TARGET_FILE_VERIFY_FAILED',
                'TARGET_STAGED_HEALTH_FAILED',
            ),
        )

        # Mirror Gateway: structured TargetPrepareError must terminalize pre-READY.
        terminalize_pre_ready_intent_failure(
            self.request_id,
            error_code=ctx.exception.error_code,
            db_path=self.db,
            now=NOW,
        )

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_RELEASED)
        self.assertEqual(intent['error_code'], ctx.exception.error_code)
        self.assertNotEqual(intent['status'], INTENT_FORGING)
        self.assertIsNone(intent.get('target_context_id'))
        self.assertIsNone(intent.get('staged_ready_at'))
        # Candidate JSONL was published — mark orphan pending; do not delete.
        self.assertEqual(intent.get('orphan_jsonl_state'), 'pending')
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual_staged'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM context_claude_sessions "
                "WHERE source='context_window_forge'"
            ).fetchone()[0],
            0,
        )

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertEqual(source_after.get('closed_at'), source_before.get('closed_at'))
        self.assertEqual(int(source_after['version']), int(source_before['version']))
        latest = dc.get_latest_active_context('default', db_path=self.db)
        self.assertEqual(int(latest['id']), self.context_id)

        # Candidate session file still present (replacement bytes kept — not deleted).
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
