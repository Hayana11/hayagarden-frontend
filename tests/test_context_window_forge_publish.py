"""Focused Forge publish + Intent bind R1 tests — temp SQLite + temp JSONL only."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
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
from chat.context_window import (
    INTENT_FORGING,
    INTENT_RELEASED,
    _intent_row,
    get_active_switch_intent,
    has_active_switch_intent,
    terminalize_pre_ready_intent_failure,
)
from chat.context_window_forge_publish import (
    ForgePublishError,
    PUBLISH_STATUS_ALREADY_PUBLISHED,
    PUBLISH_STATUS_NATIVE_COLD_BOUND,
    PUBLISH_STATUS_PUBLISHED,
    publish_context_window_forge_candidate,
)
import chat.context_window_forge_publish as forge_mod
from chat.daily_context import _connect
from chat.session_registry import (
    get_context_claude_session,
    register_context_claude_session,
)
from tools.cc_jsonl_usage import session_jsonl_path

SESSION_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
NOW = datetime.datetime(2026, 7, 31, 12, 0, 0)


def _tmp_workspace():
    tmp = tempfile.mkdtemp(prefix='cw-forge-pub-')
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
    parent: Optional[str],
    content: Any,
    cwd: str = '/tmp/forge-publish-cwd',
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


class ContextWindowForgePublishTests(unittest.TestCase):
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
        # Clear test hooks
        forge_mod._post_publish_hook = None
        forge_mod._finalize_fault_hook = None
        forge_mod._before_owned_cleanup_hook = None

    def tearDown(self) -> None:
        forge_mod._post_publish_hook = None
        forge_mod._finalize_fault_hook = None
        forge_mod._before_owned_cleanup_hook = None
        self._home_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jsonl_path(self, session: str = SESSION_A) -> Path:
        path = session_jsonl_path(self.cwd, session, claude_home=str(self.claude_home))
        assert path is not None
        return Path(path)

    def _register(self, *, session: str, scan_offset: int):
        return register_context_claude_session(
            context_id=self.context_id,
            context_epoch=self.epoch,
            resident_generation=self.gen,
            chat_id='default',
            claude_session_id=session,
            cwd=self.cwd,
            source='test_forge_publish',
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

    def _intent(self) -> dict[str, Any]:
        conn = _connect(self.db)
        try:
            row = _intent_row(conn, self.request_id)
            assert row is not None
            return row
        finally:
            conn.close()

    def _publish(self, *, count: int = 3):
        return publish_context_window_forge_candidate(
            source_context_id=self.context_id,
            source_context_epoch=self.epoch,
            count=count,
            preview_id=self.request_id,
            request_id=self.request_id,
            thinking_policy=ThinkingPolicy.DROP,
            forge_cwd=self.cwd,
            claude_home=self.claude_home,
            chat_id='default',
            db_path=self.db,
            now=NOW,
        )

    def test_plain_published(self):
        """1) READY candidate → PUBLISHED; one-shot temp unlink failure recovered."""
        _src_path, _end, ids = self._plain_two_rounds()
        before_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts'
        ).fetchone()[0]
        before_reg = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM context_claude_sessions'
        ).fetchone()[0]

        real_unlink = os.unlink
        unlink_fails = {'n': 0}

        def _flaky_unlink(path, *args, **kwargs):
            name = path if isinstance(path, str) else str(path)
            if '.forge-tmp-' in name and unlink_fails['n'] == 0:
                unlink_fails['n'] += 1
                raise OSError(errno.EIO, 'injected temp unlink failure')
            return real_unlink(path, *args, **kwargs)

        import errno
        with mock.patch('os.unlink', side_effect=_flaky_unlink):
            result = self._publish(count=3)
        self.assertEqual(unlink_fails['n'], 1)
        self.assertEqual(result.publish_status, PUBLISH_STATUS_PUBLISHED)
        self.assertIsNotNone(result.jsonl_path)
        assert result.jsonl_path is not None
        self.assertTrue(result.jsonl_path.is_file())
        self.assertFalse(result.jsonl_path.is_symlink())
        mode = stat.S_IMODE(result.jsonl_path.stat().st_mode)
        self.assertEqual(mode & ~0o600, 0)
        self.assertEqual(result.jsonl_path.stat().st_size, result.jsonl_size)
        self.assertEqual(
            __import__('hashlib').sha256(result.jsonl_path.read_bytes()).hexdigest(),
            result.jsonl_sha256,
        )
        self.assertGreater(result.event_count, 0)
        self.assertEqual(result.selected_round_count, 2)
        self.assertFalse(result.recovered_existing_file)
        parent = result.jsonl_path.parent
        self.assertEqual(list(parent.glob('.forge-tmp-*')), [])

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_FORGING)
        self.assertEqual(intent['preview_id'], self.request_id)
        self.assertEqual(intent['thinking_policy'], 'drop')
        self.assertEqual(intent['target_session_id'], result.candidate_session_id)
        self.assertEqual(intent['target_jsonl_sha256'], result.jsonl_sha256)
        self.assertEqual(int(intent['target_jsonl_size']), result.jsonl_size)
        self.assertEqual(intent['orphan_jsonl_state'], 'none')
        self.assertIsNone(intent['target_context_id'])
        self.assertIsNone(intent['staged_ready_at'])

        after_contexts = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts'
        ).fetchone()[0]
        after_reg = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM context_claude_sessions'
        ).fetchone()[0]
        self.assertEqual(after_contexts, before_contexts)
        self.assertEqual(after_reg, before_reg)
        reg = get_context_claude_session(
            self.context_id, self.gen, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['claude_session_id'], SESSION_A)
        ctx = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertIsNone(ctx.get('closed_at'))
        self.assertEqual(int(ctx['resident_generation']), self.gen)
        self.assertEqual(
            json.loads(intent['selected_message_ids_json']), list(ids),
        )

    def test_native_cold_bound(self):
        """2) NATIVE_COLD bind; persisted ALREADY without prepare after source change."""
        before_files = set(Path(self.home).rglob('*.jsonl'))
        projects_before = (
            set((self.claude_home / 'projects').rglob('*'))
            if (self.claude_home / 'projects').exists() else set()
        )
        result = self._publish(count=0)
        self.assertEqual(result.publish_status, PUBLISH_STATUS_NATIVE_COLD_BOUND)
        self.assertIsNone(result.jsonl_path)
        self.assertEqual(result.jsonl_size, 0)
        self.assertEqual(
            result.jsonl_sha256,
            __import__('hashlib').sha256(b'').hexdigest(),
        )
        self.assertEqual(result.proof_kind, 'native_cold_contract')
        after_files = set(Path(self.home).rglob('*.jsonl'))
        self.assertEqual(after_files, before_files)
        projects_after = (
            set((self.claude_home / 'projects').rglob('*'))
            if (self.claude_home / 'projects').exists() else set()
        )
        self.assertEqual(projects_after, projects_before)

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_FORGING)
        self.assertEqual(intent['target_session_id'], result.candidate_session_id)
        self.assertEqual(int(intent['target_jsonl_size']), 0)
        self.assertEqual(intent['orphan_jsonl_state'], 'none')
        self.assertIsNone(intent['target_context_id'])
        updated_at = intent['updated_at']

        # Representative post-publish source change (version bump).
        conn = sqlite3.connect(self.db)
        conn.execute(
            'UPDATE daily_contexts SET version=version+1 WHERE id=?',
            (self.context_id,),
        )
        conn.commit()
        conn.close()

        prepare_calls = {'n': 0}

        def _forbid_prepare(*_a, **_k):
            prepare_calls['n'] += 1
            raise AssertionError('prepare must not run on persisted ALREADY')

        with mock.patch(
            'chat.context_window_forge_publish.prepare_context_window_candidate',
            side_effect=_forbid_prepare,
        ):
            again = self._publish(count=0)
        self.assertEqual(again.publish_status, PUBLISH_STATUS_ALREADY_PUBLISHED)
        self.assertEqual(again.candidate_session_id, result.candidate_session_id)
        self.assertEqual(again.proof_kind, 'native_cold_contract')
        self.assertEqual(prepare_calls['n'], 0)
        intent2 = self._intent()
        self.assertEqual(intent2['updated_at'], updated_at)

    def test_pending_crash_recovery(self):
        """3) Pending crash recovery + same-request concurrent serialize."""
        self._plain_two_rounds()

        def _fail_finalize():
            raise ForgePublishError(
                'injected finalize failure', error_code='FORGE_DB_FINALIZE_FAILED',
            )

        forge_mod._finalize_fault_hook = _fail_finalize
        with self.assertRaises(ForgePublishError) as ctx:
            self._publish(count=3)
        self.assertEqual(ctx.exception.error_code, 'FORGE_DB_FINALIZE_FAILED')

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_FORGING)
        self.assertEqual(intent['orphan_jsonl_state'], 'pending')
        self.assertIsNotNone(intent['target_session_id'])
        sid = str(intent['target_session_id'])
        path = session_jsonl_path(self.cwd, sid, claude_home=str(self.claude_home))
        assert path is not None
        path = Path(path)
        self.assertTrue(path.is_file())
        st1 = path.stat()
        inode1 = st1.st_ino
        mtime1 = st1.st_mtime_ns
        sha1 = __import__('hashlib').sha256(path.read_bytes()).hexdigest()

        forge_mod._finalize_fault_hook = None
        outcomes: list[Any] = []
        errors: list[BaseException] = []

        def _worker():
            try:
                outcomes.append(self._publish(count=3))
            except BaseException as exc:  # noqa: BLE001 — capture for assertion
                errors.append(exc)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        statuses = sorted(r.publish_status for r in outcomes)
        self.assertEqual(
            statuses,
            sorted([PUBLISH_STATUS_PUBLISHED, PUBLISH_STATUS_ALREADY_PUBLISHED]),
        )
        for r in outcomes:
            self.assertNotEqual(r.publish_status, 'FORGE_TARGET_EXISTS')
        st2 = path.stat()
        self.assertEqual(st2.st_ino, inode1)
        self.assertEqual(st2.st_mtime_ns, mtime1)
        self.assertEqual(
            __import__('hashlib').sha256(path.read_bytes()).hexdigest(), sha1,
        )
        intent2 = self._intent()
        self.assertEqual(intent2['orphan_jsonl_state'], 'none')
        self.assertEqual(intent2['status'], INTENT_FORGING)
        self.assertNotEqual(intent2['status'], INTENT_RELEASED)
        self.assertNotEqual(intent2.get('orphan_jsonl_state'), 'foreign_exists')

    def test_foreign_target_and_source_change(self):
        """4) Foreign blocked; owned delete; replacement inode → delete_blocked."""
        src_path, end2, _ids = self._plain_two_rounds()
        original_src = src_path.read_bytes()

        with self.subTest('foreign_target'):
            from chat.context_window_preview import prepare_context_window_candidate
            from chat.context_window import reserve_or_load_intent
            rid = str(uuid.uuid4())
            reserve_or_load_intent(
                source_context_id=self.context_id,
                source_context_epoch=self.epoch,
                count=3,
                request_id=rid,
                chat_id='default',
                db_path=self.db,
                now=NOW,
            )
            prep = prepare_context_window_candidate(
                source_context_id=self.context_id,
                source_context_epoch=self.epoch,
                count=3,
                preview_id=rid,
                thinking_policy=ThinkingPolicy.DROP,
                chat_id='default',
                db_path=self.db,
                now=NOW,
                allowed_switch_request_id=rid,
            )
            self.assertEqual(prep.response['preview_status'], 'READY')
            assert prep.artifact is not None
            foreign = Path(session_jsonl_path(
                self.cwd, prep.artifact.candidate_session_id,
                claude_home=str(self.claude_home),
            ))
            foreign.parent.mkdir(parents=True, exist_ok=True)
            foreign.write_bytes(b'{"type":"user","uuid":"foreign"}\n')
            foreign_bytes = foreign.read_bytes()

            with self.assertRaises(ForgePublishError) as ctx:
                publish_context_window_forge_candidate(
                    source_context_id=self.context_id,
                    source_context_epoch=self.epoch,
                    count=3,
                    preview_id=rid,
                    request_id=rid,
                    thinking_policy=ThinkingPolicy.DROP,
                    forge_cwd=self.cwd,
                    claude_home=self.claude_home,
                    chat_id='default',
                    db_path=self.db,
                    now=NOW,
                )
            self.assertEqual(ctx.exception.error_code, 'FORGE_TARGET_EXISTS')
            self.assertEqual(foreign.read_bytes(), foreign_bytes)
            conn = _connect(self.db)
            try:
                row = _intent_row(conn, rid)
            finally:
                conn.close()
            assert row is not None
            self.assertEqual(row['status'], INTENT_RELEASED)
            self.assertEqual(row['orphan_jsonl_state'], 'foreign_exists')
            self.assertEqual(row['error_code'], 'FORGE_TARGET_EXISTS')

        with self.subTest('source_changed_deletes_owned'):
            rid2 = str(uuid.uuid4())
            self.request_id = rid2
            src_path.write_bytes(original_src)

            def _mutate_source():
                raw = bytearray(src_path.read_bytes())
                idx = min(10, max(0, end2 - 1))
                raw[idx] = (raw[idx] + 1) % 256
                src_path.write_bytes(bytes(raw))

            forge_mod._post_publish_hook = _mutate_source
            with self.assertRaises(ForgePublishError) as ctx:
                self._publish(count=3)
            self.assertEqual(ctx.exception.error_code, 'FORGE_SOURCE_CHANGED')
            forge_mod._post_publish_hook = None

            intent = self._intent()
            self.assertEqual(intent['status'], INTENT_RELEASED)
            self.assertEqual(intent['error_code'], 'FORGE_SOURCE_CHANGED')
            self.assertEqual(intent['orphan_jsonl_state'], 'deleted')
            sid = intent.get('target_session_id')
            self.assertIsNotNone(sid)
            gone = Path(session_jsonl_path(
                self.cwd, str(sid), claude_home=str(self.claude_home),
            ))
            self.assertFalse(gone.exists())
            ctx_row = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
            self.assertIsNone(ctx_row.get('closed_at'))
            self.assertEqual(int(ctx_row['resident_generation']), self.gen)

        with self.subTest('replacement_inode_delete_blocked'):
            src_path.write_bytes(original_src)
            rid3 = str(uuid.uuid4())
            self.request_id = rid3

            def _fail_finalize():
                raise ForgePublishError(
                    'injected finalize failure',
                    error_code='FORGE_DB_FINALIZE_FAILED',
                )

            forge_mod._finalize_fault_hook = _fail_finalize
            with self.assertRaises(ForgePublishError) as ctx:
                self._publish(count=3)
            self.assertEqual(ctx.exception.error_code, 'FORGE_DB_FINALIZE_FAILED')
            forge_mod._finalize_fault_hook = None

            intent = self._intent()
            self.assertEqual(intent['orphan_jsonl_state'], 'pending')
            sid = str(intent['target_session_id'] or '')
            final = Path(session_jsonl_path(
                self.cwd, sid, claude_home=str(self.claude_home),
            ))
            self.assertTrue(final.is_file())
            replacement = b'{"type":"replacement-inode"}\n'

            def _swap_inode() -> None:
                if final.exists():
                    final.unlink()
                final.write_bytes(replacement)
                os.chmod(final, 0o600)

            forge_mod._before_owned_cleanup_hook = _swap_inode

            def _mutate_source_again():
                raw = bytearray(src_path.read_bytes())
                idx = min(10, max(0, end2 - 1))
                raw[idx] = (raw[idx] + 2) % 256
                src_path.write_bytes(bytes(raw))

            forge_mod._post_publish_hook = _mutate_source_again
            try:
                with self.assertRaises(ForgePublishError) as ctx:
                    self._publish(count=3)
                self.assertEqual(ctx.exception.error_code, 'FORGE_SOURCE_CHANGED')
            finally:
                forge_mod._post_publish_hook = None
                forge_mod._before_owned_cleanup_hook = None

            intent2 = self._intent()
            self.assertEqual(intent2['status'], INTENT_RELEASED)
            self.assertEqual(intent2['error_code'], 'FORGE_SOURCE_CHANGED')
            self.assertEqual(intent2['orphan_jsonl_state'], 'delete_blocked')
            self.assertTrue(final.is_file())
            self.assertEqual(final.read_bytes(), replacement)

    def test_preview_registry_missing_terminalizes_pre_ready(self):
        """Incident replay: chat msgs exist, no Registry/mapping, count=3.

        Preview correctly fail-closes with PREVIEW_REGISTRY_MISSING; Gateway
        terminalize must release the reserved intent so chat is not locked.
        """
        # Formal chat rounds bound to source — but no Registry / mapping.
        u1 = _insert_msg(self.db, 'hayana', '旧窗用户一')
        a1 = _insert_msg(self.db, 'fyodor', '旧窗助手一')
        u2 = _insert_msg(self.db, 'hayana', '旧窗用户二')
        a2 = _insert_msg(self.db, 'fyodor', '旧窗助手二')
        for mid, role in ((u1, 'user'), (a1, 'assistant'), (u2, 'user'), (a2, 'assistant')):
            _bind_msg(
                self.db, mid, context_id=self.context_id,
                epoch=self.epoch, gen=self.gen, role=role,
            )
        source_before = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        contexts_before = sqlite3.connect(self.db).execute(
            'SELECT COUNT(*) FROM daily_contexts'
        ).fetchone()[0]

        with self.assertRaises(ForgePublishError) as ctx:
            self._publish(count=3)
        self.assertEqual(ctx.exception.error_code, 'PREVIEW_REGISTRY_MISSING')

        # Mirror Gateway: terminalize before surfacing structured pre-READY error.
        terminalize_pre_ready_intent_failure(
            self.request_id,
            error_code=ctx.exception.error_code,
            db_path=self.db,
            now=NOW,
        )

        intent = self._intent()
        self.assertEqual(intent['status'], INTENT_RELEASED)
        self.assertEqual(intent['error_code'], 'PREVIEW_REGISTRY_MISSING')
        self.assertIsNone(intent.get('target_context_id'))
        self.assertIsNone(intent.get('staged_ready_at'))
        self.assertIsNone(intent.get('target_session_id'))
        self.assertFalse(has_active_switch_intent('default', db_path=self.db))
        self.assertIsNone(get_active_switch_intent('default', db_path=self.db))

        source_after = dc.get_daily_context_by_id(self.context_id, db_path=self.db)
        self.assertEqual(source_after.get('closed_at'), source_before.get('closed_at'))
        self.assertEqual(int(source_after['version']), int(source_before['version']))
        self.assertEqual(
            int(source_after['resident_generation']),
            int(source_before['resident_generation']),
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                'SELECT COUNT(*) FROM daily_contexts'
            ).fetchone()[0],
            contexts_before,
        )
        self.assertEqual(
            sqlite3.connect(self.db).execute(
                "SELECT COUNT(*) FROM daily_contexts WHERE window_mode='manual_staged'"
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(intent.get('first_turn_request_id'))
        self.assertIsNone(intent.get('first_user_message_id'))


if __name__ == '__main__':
    unittest.main()
