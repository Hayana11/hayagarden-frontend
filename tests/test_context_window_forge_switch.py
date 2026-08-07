"""Focused contract tests for seamless Forge context-window switch (step5)."""
from __future__ import annotations

import base64
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
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PIL import Image, ImageDraw, ImageFont

from chat import context_window as cw
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.cc_vision_bridge import VisionBridgeError, resolve_image_bytes
from chat.context_window_forge import (
    CarryoverUnforgeableError,
    build_events_from_selected_messages,
    forge_target_session_from_db,
)
from tools.claude_forge_core import load_jsonl, sha256_file
from tools.claude_forge_validator import validate_forged_transcript

ASSISTANT_IMAGE_ACK = '收到这张图片。'


def _random_vision_marker() -> str:
    """Pixel-only canary. Must never appear in filename/DB text/prompt/JSONL text."""
    return 'VISION-%s' % os.urandom(16).hex()


def _make_vision_png(path: Path, text: str) -> None:
    img = Image.new('RGB', (480, 120), 'white')
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 22,
        )
    except Exception:
        font = ImageFont.load_default()
    draw.text((16, 40), text, fill='black', font=font)
    img.save(path, format='PNG')


def _jsonl_text_blobs(events: list) -> str:
    parts: list[str] = []
    for evt in events:
        msg = evt.get('message') or {}
        content = msg.get('content')
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    parts.append(str(block.get('text') or ''))
    return '\n'.join(parts)


def _assert_marker_absent_from_textual_metadata(
    test: unittest.TestCase,
    *,
    marker: str,
    events: list,
    path: Path,
) -> None:
    test.assertNotIn(marker, path.name)
    test.assertNotIn(marker, str(path))
    test.assertNotIn(marker, _jsonl_text_blobs(events))


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
            created_at TEXT,
            source_kind TEXT
        )'''
    )
    conn.commit()
    conn.close()


def _insert(db: str, author: str, content: str, created_at: str) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, created_at, source_kind) VALUES (?,?,?,?)',
        (author, content, created_at, 'chat'),
    )
    mid = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return mid


def _insert_with_image(
    db: str,
    author: str,
    content: str,
    image_url: str,
    created_at: str,
) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        'INSERT INTO chat_messages (author, content, image_url, created_at, source_kind) '
        'VALUES (?,?,?,?,?)',
        (author, content, image_url, created_at, 'chat'),
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


def _seed_rounds(db: str, ctx: dict, n: int) -> list[int]:
    ids: list[int] = []
    base = datetime.datetime(2026, 7, 27, 10, 0, 0)
    for i in range(n):
        ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
        ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
        u = _insert(db, 'hayana', 'user-%d' % i, ts_u)
        a = _insert(db, 'fyodor', 'asst-%d' % i, ts_a)
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), a, 'assistant')
        ids.extend([u, a])
    return ids


class ForgeSwitchContractTests(unittest.TestCase):
    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-switch-')
        self.hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        dr.reset_bindings_for_tests()
        # Flag on for prepare_daily_turn gate tests.
        self._flag_patch = mock.patch(
            'chat.daily_context.enabled', return_value=True,
        )
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_A_reserve_blocks_prepare_daily_turn(self):
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        self.assertEqual(intent['status'], cw.INTENT_RESERVED)
        u = _insert(self.db, 'hayana', 'blocked', '2026-07-27 11:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError) as ar:
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        self.assertEqual(ar.exception.error_code, 'switch_in_progress')

    def test_B_db_forge_counts(self):
        _seed_rounds(self.db, self.ctx, 12)
        for count in (0, 3, 5, 10):
            with self.subTest(count=count):
                messages = []
                conn = dc._connect(self.db)
                try:
                    # Replicate reserve selection without leaving active intents.
                    from chat.context_window import (
                        _collect_context_formal_messages,
                        _select_rounds_locked,
                    )
                    rows = _collect_context_formal_messages(
                        conn,
                        context_id=int(self.ctx['id']),
                        context_epoch=int(self.ctx['context_epoch']),
                    )
                    _rounds, selected, _rc = _select_rounds_locked(rows, count)
                    if count == 0:
                        self.assertEqual(selected, [])
                    else:
                        self.assertEqual(len(selected), count * 2)
                    forged = forge_target_session_from_db(
                        conn,
                        selected_message_ids=selected,
                        cwd=self.hooks.forge_cwd,
                        claude_home=self.hooks.claude_home,
                    )
                finally:
                    conn.close()
                events = load_jsonl(forged.jsonl_path)
                vr = validate_forged_transcript(
                    events, session_id=forged.target_session_id,
                )
                self.assertTrue(vr.ok, vr.errors)
                if count == 0:
                    self.assertEqual(len(events), 2)
                    self.assertIn('context-window-boundary', events[0]['message']['content'])
                else:
                    self.assertEqual(len(events), count * 2)
                    for evt in events:
                        self.assertIn(evt['type'], ('user', 'assistant'))
                        self.assertNotIn('usage', evt.get('message') or {})

    def test_C_staged_health_jsonl_unchanged(self):
        req = str(uuid.uuid4())
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        path = Path(self.hooks.claude_home) / 'projects'
        # find jsonl
        files = list(Path(self.hooks.claude_home).rglob('*.jsonl') )
        self.assertTrue(files)
        digest = sha256_file(files[0])
        # offline prepare again must keep bytes
        intent = {
            'target_session_id': out['claude_session_id'],
            'target_jsonl_sha256': digest,
        }
        staged = self.hooks.prepare_staged(intent, files[0])
        self.assertEqual(sha256_file(files[0]), digest)
        self.hooks.discard_staged(staged)

    def test_D_forge_failure_keeps_source(self):
        # Force unforgeable by locking a nonexistent message id via direct intent forge path
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        # Corrupt selected ids
        conn = dc._connect(self.db)
        try:
            conn.execute(
                'UPDATE context_switch_intents SET selected_message_ids_json=? WHERE request_id=?',
                (json.dumps([999999]), req),
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(cw.CarryoverMessageUnforgeableError):
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
        open_ctx = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(open_ctx['id']), int(self.ctx['id']))
        self.assertIsNone(open_ctx.get('closed_at'))

    def test_D_staged_failure_keeps_source_and_old_resident(self):
        killed = {'n': 0}

        class BoomHooks(cw.SwitchHooks):
            pass

        def prepare_staged(intent, forge_path):
            raise cw.SwitchFailedError('staged_exited_during_health_window')

        def take_handoff(staged, result):
            return None

        def discard_staged(staged):
            killed['n'] += 1

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=self.hooks.forge_cwd,
            claude_home=self.hooks.claude_home,
        )
        old = mock.MagicMock()
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=dc.make_resident_key(
                chat_id='default',
                context_epoch=int(self.ctx['context_epoch']),
                resident_generation=1,
            ),
            context_id=int(self.ctx['id']),
            context_epoch=int(self.ctx['context_epoch']),
            resident_generation=1,
            bound_cursor_message_id=None,
            process_generation=1,
            tool_profile='daily',
        ))
        with self.assertRaises(cw.SwitchFailedError):
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=str(uuid.uuid4()),
                db_path=self.db,
                hooks=hooks,
            )
        open_ctx = cw.get_current_context_window(db_path=self.db)
        self.assertEqual(int(open_ctx['id']), int(self.ctx['id']))
        self.assertIsNotNone(dr.get_local_binding())
        old._kill.assert_not_called()

    def test_E_handoff_pending_blocks_turns(self):
        req = str(uuid.uuid4())
        # Manually move to handoff_pending after a successful forge/ready/commit
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        # Force status back to handoff_pending to simulate crash before committed mark
        conn = dc._connect(self.db)
        try:
            conn.execute(
                'UPDATE context_switch_intents SET status=? WHERE request_id=?',
                (cw.INTENT_HANDOFF_PENDING, req),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(cw.has_active_switch_intent(db_path=self.db))
        u = _insert(self.db, 'hayana', 'race', '2026-07-27 12:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError):
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        # recover
        recovered = cw.complete_handoff_pending_recovery(
            request_id=req, hooks=self.hooks, db_path=self.db,
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered['target_context_id'], out['target_context_id'])
        conn = dc._connect(self.db)
        try:
            st = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?', (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st, cw.INTENT_COMMITTED)

    def test_G_committed_required_for_first_turn(self):
        req = str(uuid.uuid4())
        intent = cw.reserve_or_load_intent(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
        )
        for status in (
            cw.INTENT_RESERVED,
            cw.INTENT_FORGING,
            cw.INTENT_READY,
            cw.INTENT_COMMITTING,
            cw.INTENT_HANDOFF_PENDING,
        ):
            conn = dc._connect(self.db)
            try:
                conn.execute(
                    'UPDATE context_switch_intents SET status=? WHERE request_id=?',
                    (status, req),
                )
                conn.commit()
            finally:
                conn.close()
            u = _insert(self.db, 'hayana', 'x-%s' % status, '2026-07-27 13:00:00')
            with self.assertRaises(dr.SwitchInProgressRuntimeError):
                dr.prepare_daily_turn(user_message_id=u, db_path=self.db)

    def test_H_same_request_does_not_reforge(self):
        _seed_rounds(self.db, self.ctx, 3)
        req = str(uuid.uuid4())
        forge_calls = {'n': 0}
        import chat.context_window_forge as forge_mod
        real_forge = forge_mod.forge_target_session_from_db

        def counting_forge(*args, **kwargs):
            forge_calls['n'] += 1
            return real_forge(*args, **kwargs)

        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting_forge):
            out1 = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
            n_after_first = forge_calls['n']
            out2 = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
                hooks=self.hooks,
            )
        self.assertEqual(out1['target_context_id'], out2['target_context_id'])
        self.assertEqual(forge_calls['n'], n_after_first)
        self.assertGreaterEqual(n_after_first, 1)

    def test_I_idempotency_mismatch(self):
        req = str(uuid.uuid4())
        cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=req,
            db_path=self.db,
            hooks=self.hooks,
        )
        # New open context is target; mismatch on count against committed intent
        with self.assertRaises(cw.IdempotencyMismatchError):
            cw.reserve_or_load_intent(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=req,
                db_path=self.db,
            )


class StagedIdentityAndCursorTests(unittest.TestCase):
    """A/B: formal identity continuity + DB cursor hot first turn."""

    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-id-')
        self.hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        dr.reset_bindings_for_tests()
        self._flag_patch = mock.patch('chat.daily_context.enabled', return_value=True)
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _binding_hooks(self, staged):
        killed = {'old': 0, 'staged': 0}
        old = mock.MagicMock()
        old._kill = mock.Mock(side_effect=lambda quiet=True: killed.__setitem__('old', killed['old'] + 1))
        old.generation = 9

        def prepare_staged(intent, forge_path):
            staged.session_id = str(intent['target_session_id'])
            staged.jsonl_path = forge_path
            return staged

        def take_handoff(s, result):
            dr.bind_target_resident_after_switch(
                staged_resident=s,
                result=result,
                tool_profile=dr.DAILY_TOOL_PROFILE,
                db_path=self.db,
            )
            return old

        def discard_staged(s):
            killed['staged'] += 1
            kill = getattr(s, '_kill', None)
            if callable(kill):
                kill(quiet=True)

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=self.hooks.forge_cwd,
            claude_home=self.hooks.claude_home,
        )
        return hooks, old, killed

    def test_A_ensure_alive_does_not_respawn_after_handoff(self):
        ids = _seed_rounds(self.db, self.ctx, 2)
        exact_full_system = 'EXACT_FULL_SYSTEM_FOR_DAILY'
        exact_env = {
            'CLAUDE_CODE_OAUTH_TOKEN': 'tok',
            'PATH': os.environ.get('PATH', ''),
        }
        staged = mock.MagicMock()
        staged.generation = 3
        staged.session_id = None
        staged.tool_profile = dr.DAILY_TOOL_PROFILE
        staged._system_text = exact_full_system
        staged._tool_profile = dr.DAILY_TOOL_PROFILE
        staged._alive = mock.Mock(return_value=True)
        staged._spawn = mock.Mock()
        staged._kill = mock.Mock()
        # Real ensure_alive decision path via a thin wrapper.
        real = __import__('cc_resident').ResidentSession
        # Use a real ResidentSession instance with mocked process alive.

        class _Staged:
            def __init__(self):
                self.session_id = None
                self.jsonl_path = None
                self.generation = 3
                self._tool_profile = dr.DAILY_TOOL_PROFILE
                self._system_text = exact_full_system
                self._spawn_calls = 0
                self._kill_calls = 0
                self._alive_flag = True
                self._last_used = __import__('time').time()
                self._last_round_context = 0
                self._resident_turn_count = 0
                self._turns_since_respawn = 0
                self._lock = __import__('threading').RLock()
                self._cold = False

            @property
            def tool_profile(self):
                return self._tool_profile

            def _alive(self):
                return self._alive_flag

            def _decide_respawn_reason(self, system_text, *, tool_profile='legacy'):
                return real._decide_respawn_reason(
                    self, system_text, tool_profile=tool_profile,
                )

            def ensure_alive(self, system_text, env, *, tool_profile='legacy'):
                return real.ensure_alive(
                    self, system_text, env, tool_profile=tool_profile,
                )

            def _spawn(self, system_text, env, *, reason='process_dead', tool_profile='legacy'):
                self._spawn_calls += 1
                self._system_text = system_text
                self._tool_profile = tool_profile

            def _kill(self, quiet=True):
                self._kill_calls += 1

        staged = _Staged()
        hooks, old, killed = self._binding_hooks(staged)
        before_gen = staged.generation
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=0,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
            hooks=hooks,
        )
        target_sid = out['claude_session_id']
        self.assertEqual(staged.session_id, target_sid)
        files = list(Path(hooks.claude_home).rglob('*.jsonl'))
        self.assertTrue(files)
        before_bytes = files[0].read_bytes()
        cold = staged.ensure_alive(
            exact_full_system,
            exact_env,
            tool_profile=dr.DAILY_TOOL_PROFILE,
        )
        self.assertFalse(cold)
        self.assertEqual(staged._spawn_calls, 0)
        self.assertEqual(staged._kill_calls, 0)
        self.assertEqual(staged.generation, before_gen)
        self.assertEqual(staged.session_id, target_sid)
        self.assertEqual(files[0].read_bytes(), before_bytes)
        # Old closed only after committed success.
        self.assertEqual(killed['old'], 1)
        self.assertEqual(ids[-1], out['boundary_message_id'])

    def _assert_first_turn_hot(self, *, count: int):
        seeded = _seed_rounds(self.db, self.ctx, max(count, 1))
        staged = mock.MagicMock()
        staged.generation = 7
        staged.session_id = None
        staged._tool_profile = dr.DAILY_TOOL_PROFILE
        staged.tool_profile = dr.DAILY_TOOL_PROFILE
        staged._alive = mock.Mock(return_value=True)
        staged._kill = mock.Mock()

        hooks, old, killed = self._binding_hooks(staged)
        out = cw.switch_context_window(
            source_context_id=int(self.ctx['id']),
            source_context_epoch=int(self.ctx['context_epoch']),
            count=count,
            request_id=str(uuid.uuid4()),
            db_path=self.db,
            hooks=hooks,
        )
        target_id = int(out['target_context_id'])
        target_gen = int(out['resident_generation'])
        db_cursor = dc.get_resident_history_cursor(
            target_id, target_gen, db_path=self.db,
        )
        self.assertIsNotNone(db_cursor)
        if count == 0:
            self.assertEqual(db_cursor, int(out['boundary_message_id']))
        else:
            self.assertEqual(db_cursor, int(out['selected_message_ids'][-1]))

        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        self.assertEqual(binding.bound_cursor_message_id, db_cursor)
        self.assertEqual(binding.tool_profile, dr.DAILY_TOOL_PROFILE)
        self.assertEqual(binding.process_generation, 7)

        # First formal message on target: must be hot, no carryover inject.
        u = _insert(self.db, 'hayana', 'first-after-switch', '2026-07-27 16:00:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            plan = dr.prepare_daily_turn(
                user_message_id=u,
                db_path=self.db,
                resident=staged,
                static_system='S',
                now=datetime.datetime(2026, 7, 27, 16, 0, 0),
                wall_now=datetime.datetime(2026, 7, 27, 16, 0, 0),
            )
        self.assertFalse(plan.is_cold)
        self.assertFalse(plan.is_respawn)
        self.assertEqual(plan.manifest.get('turn_kind'), 'hot')
        self.assertFalse(plan.manifest.get('carryover_injected_this_turn'))
        staged._kill.assert_not_called()
        return out, seeded

    def test_B_count0_first_turn_hot_no_carryover_reinject(self):
        self._assert_first_turn_hot(count=0)

    def test_B_count3_first_turn_hot_no_carryover_reinject(self):
        self._assert_first_turn_hot(count=3)

    def test_B_empty_boundary0_bootstrap_not_fake_hot(self):
        # No seeded messages → boundary watermark 0 → cursor bootstrap, not hot.
        staged = mock.MagicMock()
        staged.generation = 1
        staged.session_id = 'sid'
        staged._tool_profile = dr.DAILY_TOOL_PROFILE
        result = {
            'target_context_id': int(self.ctx['id']),
            'target_context_epoch': int(self.ctx['context_epoch']),
            'resident_generation': 1,
            'selected_message_ids': [],
            'boundary_message_id': 0,
            'claude_session_id': 'sid',
        }
        binding = dr.bind_target_resident_after_switch(
            staged_resident=staged,
            result=result,
            tool_profile=dr.DAILY_TOOL_PROFILE,
            db_path=self.db,
        )
        self.assertIsNone(binding.bound_cursor_message_id)
        self.assertIsNone(dc.get_resident_history_cursor(
            int(self.ctx['id']), 1, db_path=self.db,
        ))
        plan_like = dr.DailyTurnPlan(
            request_id='r', chat_id='default', local_day='2026-07-27',
            context_id=int(self.ctx['id']),
            context_epoch=int(self.ctx['context_epoch']),
            resident_generation=1,
            resident_key=binding.resident_key,
            user_message_id=1, epoch_token={}, lease_owner='o',
            is_cold=True, is_respawn=False, cursor_before=None,
            assembly={}, manifest={}, db_path=self.db,
            tool_profile=dr.DAILY_TOOL_PROFILE,
        )
        staged.generation = 1
        self.assertFalse(dr._can_hot_turn(
            plan=plan_like, resident=staged, db_cursor=None,
        ))


class PostCommitRecoveryTests(unittest.TestCase):
    """C: pre/post-commit exception boundaries."""

    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-post-')
        self.base_hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        _seed_rounds(self.db, self.ctx, 1)
        dr.reset_bindings_for_tests()
        self._flag_patch = mock.patch('chat.daily_context.enabled', return_value=True)
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_C1_take_handoff_fail_after_db_commit_keeps_pending(self):
        req = str(uuid.uuid4())
        discarded = {'n': 0}

        def prepare_staged(intent, forge_path):
            return self.base_hooks.prepare_staged(intent, forge_path)

        def take_handoff(staged, result):
            raise RuntimeError('take_handoff boom')

        def discard_staged(staged):
            discarded['n'] += 1
            self.base_hooks.discard_staged(staged)

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=self.base_hooks.forge_cwd,
            claude_home=self.base_hooks.claude_home,
        )
        with self.assertRaises(Exception) as ar:
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=hooks,
            )
        # Post-commit failures are re-raised as-is (or wrapped); intent stays pending.
        self.assertIn('take_handoff boom', str(ar.exception))
        conn = dc._connect(self.db)
        try:
            intent = conn.execute(
                'SELECT status, orphan_jsonl_state FROM context_switch_intents WHERE request_id=?',
                (req,),
            ).fetchone()
            self.assertEqual(intent['status'], cw.INTENT_HANDOFF_PENDING)
            target = conn.execute(
                'SELECT * FROM daily_contexts WHERE switch_request_id=?', (req,),
            ).fetchone()
            source = conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?', (int(self.ctx['id']),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(target)
        self.assertIsNotNone(source['closed_at'])
        files = list(Path(hooks.claude_home).rglob('*.jsonl'))
        self.assertTrue(files)
        self.assertTrue(files[0].is_file())
        # Formal message still gated.
        u = _insert(self.db, 'hayana', 'blocked', '2026-07-27 17:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError):
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        # Same request recovers without re-Forge.
        forge_calls = {'n': 0}
        import chat.context_window_forge as forge_mod
        real = forge_mod.forge_target_session_from_db

        def counting(*a, **k):
            forge_calls['n'] += 1
            return real(*a, **k)

        recover_hooks = cw.offline_switch_hooks(tempfile.mkdtemp(prefix='rec-'))
        # Point recovery at same forge cwd/home so JSONL is found.
        recover_hooks = cw.SwitchHooks(
            prepare_staged=self.base_hooks.prepare_staged,
            take_handoff=lambda s, r: dr.bind_target_resident_after_switch(
                staged_resident=s, result=r,
                tool_profile=dr.DAILY_TOOL_PROFILE, db_path=self.db,
            ),
            discard_staged=self.base_hooks.discard_staged,
            forge_cwd=self.base_hooks.forge_cwd,
            claude_home=self.base_hooks.claude_home,
        )
        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting):
            out = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=recover_hooks,
            )
        self.assertEqual(forge_calls['n'], 0)
        self.assertEqual(int(out['target_context_id']), int(target['id']))
        conn = dc._connect(self.db)
        try:
            st = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?', (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st, cw.INTENT_COMMITTED)

    def test_C2_mark_committed_fail_keeps_staged_retry_only_commits(self):
        req = str(uuid.uuid4())
        staged_box = {'obj': None}
        old = mock.MagicMock()
        old_kill = mock.Mock()
        old._kill = old_kill
        kill_staged = mock.Mock()

        def prepare_staged(intent, forge_path):
            s = self.base_hooks.prepare_staged(intent, forge_path)
            s._kill = kill_staged
            s.generation = 4
            s._tool_profile = dr.DAILY_TOOL_PROFILE
            staged_box['obj'] = s
            return s

        def take_handoff(s, result):
            dr.bind_target_resident_after_switch(
                staged_resident=s,
                result=result,
                tool_profile=dr.DAILY_TOOL_PROFILE,
                db_path=self.db,
            )
            return old

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=lambda s: kill_staged(quiet=True),
            forge_cwd=self.base_hooks.forge_cwd,
            claude_home=self.base_hooks.claude_home,
        )

        real_mark = cw.mark_intent_committed
        calls = {'n': 0}

        def boom_then_ok(request_id, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('mark committed boom')
            return real_mark(request_id, **kwargs)

        with mock.patch.object(cw, 'mark_intent_committed', side_effect=boom_then_ok):
            with self.assertRaises(Exception):
                cw.switch_context_window(
                    source_context_id=int(self.ctx['id']),
                    source_context_epoch=int(self.ctx['context_epoch']),
                    count=0,
                    request_id=req,
                    db_path=self.db,
                    hooks=hooks,
                )
            # After first failure: staged not killed, old not closed, pending.
            kill_staged.assert_not_called()
            old_kill.assert_not_called()
            conn = dc._connect(self.db)
            try:
                st = conn.execute(
                    'SELECT status FROM context_switch_intents WHERE request_id=?',
                    (req,),
                ).fetchone()['status']
            finally:
                conn.close()
            self.assertEqual(st, cw.INTENT_HANDOFF_PENDING)
            self.assertIsNotNone(dr.get_local_binding())
            files = list(Path(hooks.claude_home).rglob('*.jsonl'))
            self.assertTrue(files[0].is_file())

            forge_calls = {'n': 0}
            spawn_calls = {'n': 0}
            import chat.context_window_forge as forge_mod
            real_forge = forge_mod.forge_target_session_from_db

            def counting_forge(*a, **k):
                forge_calls['n'] += 1
                return real_forge(*a, **k)

            def prepare_again(intent, forge_path):
                spawn_calls['n'] += 1
                return prepare_staged(intent, forge_path)

            # Live formal holder + full match → recovery only marks committed.
            class _LiveHolder:
                def get(self):
                    return staged_box['obj']

            retry_hooks = cw.SwitchHooks(
                prepare_staged=prepare_again,
                take_handoff=take_handoff,
                discard_staged=lambda s: None,
                forge_cwd=hooks.forge_cwd,
                claude_home=hooks.claude_home,
                formal_holder=_LiveHolder(),
            )
            with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting_forge):
                out = cw.switch_context_window(
                    source_context_id=int(self.ctx['id']),
                    source_context_epoch=int(self.ctx['context_epoch']),
                    count=0,
                    request_id=req,
                    db_path=self.db,
                    hooks=retry_hooks,
                )
        self.assertEqual(forge_calls['n'], 0)
        self.assertEqual(spawn_calls['n'], 0)  # binding matched → skip prepare
        self.assertEqual(calls['n'], 2)
        old_kill.assert_called()  # closed only after committed
        conn = dc._connect(self.db)
        try:
            st = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?', (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st, cw.INTENT_COMMITTED)
        self.assertEqual(int(out['target_context_id']), int(
            dc.get_latest_active_context('default', db_path=self.db)['id']
        ))

    def test_D_hooks_none_raises(self):
        with self.assertRaises(cw.SwitchHooksRequiredError):
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=str(uuid.uuid4()),
                db_path=self.db,
                hooks=None,
            )


class _SwappableResident:
    """Behavior-equivalent holder (mirrors gateway._SwappableResident)."""

    __slots__ = ('_inner', '_lock')

    def __init__(self, inner):
        object.__setattr__(self, '_inner', inner)
        object.__setattr__(self, '_lock', __import__('threading').RLock())

    def get(self):
        return object.__getattribute__(self, '_inner')

    def swap(self, new_inner):
        lock = object.__getattribute__(self, '_lock')
        with lock:
            old = object.__getattribute__(self, '_inner')
            object.__setattr__(self, '_inner', new_inner)
            return old

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_inner'), name)


class _AliveResident:
    def __init__(self, *, session_id='old-sid', generation=1, label='old'):
        self.session_id = session_id
        self.generation = generation
        self._tool_profile = dr.DAILY_TOOL_PROFILE
        self.label = label
        self._alive_flag = True
        self.kill_calls = 0

    @property
    def tool_profile(self):
        return self._tool_profile

    def _alive(self):
        return self._alive_flag

    def _kill(self, quiet=True):
        self.kill_calls += 1
        self._alive_flag = False


class PartialSwapAtomicityTests(unittest.TestCase):
    """P1/P2: post-swap metadata failure must roll back holder atomically."""

    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-pswap-')
        self.base_hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        _seed_rounds(self.db, self.ctx, 2)
        dr.reset_bindings_for_tests()
        self._flag_patch = mock.patch('chat.daily_context.enabled', return_value=True)
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()
        self.old = _AliveResident(session_id='old-sid', generation=2, label='old')
        self.holder = _SwappableResident(self.old)
        # Source binding before switch.
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=dc.make_resident_key(
                chat_id='default',
                context_epoch=int(self.ctx['context_epoch']),
                resident_generation=1,
            ),
            context_id=int(self.ctx['id']),
            context_epoch=int(self.ctx['context_epoch']),
            resident_generation=1,
            bound_cursor_message_id=None,
            process_generation=2,
            tool_profile=dr.DAILY_TOOL_PROFILE,
            claude_session_id='old-sid',
        ))
        self.prev_binding = dr.get_local_binding()

    def tearDown(self):
        dr.set_owner_cursor_write_hook_for_tests(None)
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _make_hooks(self, *, fail_mode: str):
        """fail_mode: owner | cursor."""
        staged_box = {'obj': None, 'prepare_n': 0, 'discard_n': 0}
        forge_home = self.base_hooks.claude_home
        forge_cwd = self.base_hooks.forge_cwd

        def prepare_staged(intent, forge_path):
            staged_box['prepare_n'] += 1
            staged = _AliveResident(
                session_id=str(intent['target_session_id']),
                generation=5,
                label='staged-%d' % staged_box['prepare_n'],
            )
            staged.jsonl_path = forge_path
            staged_box['obj'] = staged
            return staged

        def take_handoff(staged, result):
            with dr.handoff_lock():
                return dr.install_target_resident_after_swap(
                    holder=self.holder,
                    staged_resident=staged,
                    result=result,
                    tool_profile=dr.DAILY_TOOL_PROFILE,
                    db_path=self.db,
                )

        def discard_staged(staged):
            staged_box['discard_n'] += 1
            if staged is not None and self.holder.get() is staged:
                raise AssertionError('discard_staged while staged is formal holder')
            if staged is not None:
                staged._kill(quiet=True)

        if fail_mode == 'owner':
            def hook(phase):
                if phase == 'after_owner':
                    raise RuntimeError('owner write boom')
            # Raise before owner upsert completes: fail at start of write.
            real_write = dr.write_target_resident_db_metadata

            def boom_write(binding, *, db_path=None):
                raise RuntimeError('owner write boom')

            write_patch = mock.patch.object(
                dr, 'write_target_resident_db_metadata', side_effect=boom_write,
            )
        else:
            def hook(phase):
                if phase == 'after_owner':
                    raise RuntimeError('cursor write boom')
            write_patch = None
            dr.set_owner_cursor_write_hook_for_tests(hook)

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=forge_cwd,
            claude_home=forge_home,
        )
        return hooks, staged_box, write_patch

    def _assert_pending_and_jsonl(self, req, hooks):
        conn = dc._connect(self.db)
        try:
            intent = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?',
                (req,),
            ).fetchone()
            self.assertEqual(intent['status'], cw.INTENT_HANDOFF_PENDING)
            target = conn.execute(
                'SELECT * FROM daily_contexts WHERE switch_request_id=?', (req,),
            ).fetchone()
            source = conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?', (int(self.ctx['id']),),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(target)
        self.assertIsNotNone(source['closed_at'])
        files = list(Path(hooks.claude_home).rglob('*.jsonl'))
        self.assertTrue(files and files[0].is_file())
        u = _insert(self.db, 'hayana', 'blocked', '2026-07-27 18:00:00')
        with self.assertRaises(dr.SwitchInProgressRuntimeError):
            dr.prepare_daily_turn(user_message_id=u, db_path=self.db)
        return int(target['id']), int(target['resident_generation'])

    def test_P1_swap_then_owner_write_fail_rolls_back_holder(self):
        req = str(uuid.uuid4())
        hooks, staged_box, write_patch = self._make_hooks(fail_mode='owner')
        with write_patch:
            with self.assertRaises(Exception) as ar:
                cw.switch_context_window(
                    source_context_id=int(self.ctx['id']),
                    source_context_epoch=int(self.ctx['context_epoch']),
                    count=0,
                    request_id=req,
                    db_path=self.db,
                    hooks=hooks,
                )
        self.assertIn('owner write boom', str(ar.exception))
        # Holder restored to old; old not killed; staged discarded only after restore.
        self.assertIs(self.holder.get(), self.old)
        self.assertEqual(self.old.kill_calls, 0)
        self.assertTrue(self.old._alive())
        self.assertEqual(dr.get_local_binding(), self.prev_binding)
        target_id, target_gen = self._assert_pending_and_jsonl(req, hooks)
        self.assertIsNone(dc.get_resident_owner(target_id, target_gen, db_path=self.db))
        self.assertIsNone(dc.get_resident_history_cursor(
            target_id, target_gen, db_path=self.db,
        ))
        first_staged = staged_box['obj']
        self.assertIsNotNone(first_staged)
        self.assertGreaterEqual(first_staged.kill_calls, 1)
        self.assertGreaterEqual(staged_box['discard_n'], 1)

        # Retry: no re-Forge; re-prepare staged; commit; close old only after committed.
        forge_calls = {'n': 0}
        import chat.context_window_forge as forge_mod
        real_forge = forge_mod.forge_target_session_from_db

        def counting(*a, **k):
            forge_calls['n'] += 1
            return real_forge(*a, **k)

        # Clear fail patch for retry (new hooks without boom).
        hooks2, staged_box2, _ = self._make_hooks(fail_mode='cursor')
        dr.set_owner_cursor_write_hook_for_tests(None)
        # Rebuild clean hooks (no fail).
        def prepare_staged(intent, forge_path):
            staged_box2['prepare_n'] += 1
            staged = _AliveResident(
                session_id=str(intent['target_session_id']),
                generation=8,
                label='retry-staged',
            )
            staged_box2['obj'] = staged
            return staged

        def take_handoff(staged, result):
            with dr.handoff_lock():
                return dr.install_target_resident_after_swap(
                    holder=self.holder,
                    staged_resident=staged,
                    result=result,
                    tool_profile=dr.DAILY_TOOL_PROFILE,
                    db_path=self.db,
                )

        def discard_staged(staged):
            if staged is not None and self.holder.get() is staged:
                raise AssertionError('discard while holder')
            if staged is not None:
                staged._kill(quiet=True)

        retry_hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=hooks.forge_cwd,
            claude_home=hooks.claude_home,
        )
        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting):
            out = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=retry_hooks,
            )
        self.assertEqual(forge_calls['n'], 0)
        self.assertGreaterEqual(staged_box2['prepare_n'], 1)
        self.assertIs(self.holder.get(), staged_box2['obj'])
        self.assertEqual(self.old.kill_calls, 1)  # closed after committed
        conn = dc._connect(self.db)
        try:
            st = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?',
                (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st, cw.INTENT_COMMITTED)
        self.assertTrue(dr.target_resident_binding_matches(
            out,
            session_id=out['claude_session_id'],
            holder=self.holder,
            expected_resident=staged_box2['obj'],
            db_path=self.db,
        ))

    def test_P2_owner_ok_cursor_fail_no_half_write(self):
        req = str(uuid.uuid4())
        hooks, staged_box, _ = self._make_hooks(fail_mode='cursor')
        with self.assertRaises(Exception) as ar:
            cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=hooks,
            )
        self.assertIn('cursor write boom', str(ar.exception))
        dr.set_owner_cursor_write_hook_for_tests(None)
        self.assertIs(self.holder.get(), self.old)
        self.assertEqual(self.old.kill_calls, 0)
        self.assertEqual(dr.get_local_binding(), self.prev_binding)
        target_id, target_gen = self._assert_pending_and_jsonl(req, hooks)
        # No owner/cursor inconsistency.
        self.assertIsNone(dc.get_resident_owner(target_id, target_gen, db_path=self.db))
        self.assertIsNone(dc.get_resident_history_cursor(
            target_id, target_gen, db_path=self.db,
        ))
        self.assertGreaterEqual(staged_box['obj'].kill_calls, 1)

        forge_calls = {'n': 0}
        import chat.context_window_forge as forge_mod
        real_forge = forge_mod.forge_target_session_from_db

        def counting(*a, **k):
            forge_calls['n'] += 1
            return real_forge(*a, **k)

        staged_box2 = {'obj': None, 'prepare_n': 0}

        def prepare_staged(intent, forge_path):
            staged_box2['prepare_n'] += 1
            staged = _AliveResident(
                session_id=str(intent['target_session_id']),
                generation=9,
                label='retry-staged',
            )
            staged_box2['obj'] = staged
            return staged

        def take_handoff(staged, result):
            with dr.handoff_lock():
                return dr.install_target_resident_after_swap(
                    holder=self.holder,
                    staged_resident=staged,
                    result=result,
                    tool_profile=dr.DAILY_TOOL_PROFILE,
                    db_path=self.db,
                )

        retry_hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=lambda s: None,
            forge_cwd=hooks.forge_cwd,
            claude_home=hooks.claude_home,
        )
        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting):
            out = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=retry_hooks,
            )
        self.assertEqual(forge_calls['n'], 0)
        self.assertGreaterEqual(staged_box2['prepare_n'], 1)
        self.assertIs(self.holder.get(), staged_box2['obj'])
        self.assertEqual(self.old.kill_calls, 1)
        owner = dc.get_resident_owner(
            int(out['target_context_id']),
            int(out['resident_generation']),
            db_path=self.db,
        )
        cursor = dc.get_resident_history_cursor(
            int(out['target_context_id']),
            int(out['resident_generation']),
            db_path=self.db,
        )
        self.assertIsNotNone(owner)
        self.assertEqual(int(cursor), int(out['boundary_message_id']))
        self.assertEqual(
            int(dr.get_local_binding().bound_cursor_message_id), int(cursor),
        )
        self.assertTrue(dr.target_resident_binding_matches(
            out,
            session_id=out['claude_session_id'],
            holder=self.holder,
            expected_resident=staged_box2['obj'],
            db_path=self.db,
        ))


class DeadStagedRecoveryTests(unittest.TestCase):
    """Original FAIL: mark_committed fail then holder staged dies must re-prepare."""

    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-dead-')
        self.base_hooks = cw.offline_switch_hooks(self.forge_root)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        _seed_rounds(self.db, self.ctx, 2)
        dr.reset_bindings_for_tests()
        self._flag_patch = mock.patch('chat.daily_context.enabled', return_value=True)
        self._flag_patch.start()
        self._cw_flag = mock.patch('chat.context_window.enabled', return_value=True)
        self._cw_flag.start()

    def tearDown(self):
        self._flag_patch.stop()
        self._cw_flag.stop()
        dr.reset_bindings_for_tests()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def test_dead_staged_after_mark_committed_failure_reprepares_before_commit(self):
        req = str(uuid.uuid4())
        old = _AliveResident(session_id='old-sid', generation=1, label='old')
        holder = _SwappableResident(old)
        staged_box = {'obj': None, 'prepare_n': 0}
        forge_calls = {'n': 0}

        def prepare_staged(intent, forge_path):
            staged_box['prepare_n'] += 1
            s = _AliveResident(
                session_id=str(intent['target_session_id']),
                generation=5 + staged_box['prepare_n'],
                label='staged-%d' % staged_box['prepare_n'],
            )
            s.jsonl_path = forge_path
            staged_box['obj'] = s
            return s

        def take_handoff(staged, result):
            with dr.handoff_lock():
                return dr.install_target_resident_after_swap(
                    holder=holder,
                    staged_resident=staged,
                    result=result,
                    tool_profile=dr.DAILY_TOOL_PROFILE,
                    db_path=self.db,
                )

        def discard_staged(staged):
            if staged is not None and holder.get() is staged:
                raise AssertionError('discard while staged is formal holder')
            if staged is not None:
                staged._kill(quiet=True)

        hooks = cw.SwitchHooks(
            prepare_staged=prepare_staged,
            take_handoff=take_handoff,
            discard_staged=discard_staged,
            forge_cwd=self.base_hooks.forge_cwd,
            claude_home=self.base_hooks.claude_home,
            formal_holder=holder,
        )

        import chat.context_window_forge as forge_mod
        real_forge = forge_mod.forge_target_session_from_db

        def counting_forge(*a, **k):
            forge_calls['n'] += 1
            return real_forge(*a, **k)

        real_mark = cw.mark_intent_committed
        mark_calls = {'n': 0}

        def boom_then_ok(request_id, **kwargs):
            mark_calls['n'] += 1
            if mark_calls['n'] == 1:
                raise RuntimeError('mark committed boom')
            return real_mark(request_id, **kwargs)

        with mock.patch.object(forge_mod, 'forge_target_session_from_db', counting_forge), \
             mock.patch.object(cw, 'mark_intent_committed', side_effect=boom_then_ok):
            with self.assertRaises(Exception) as ar:
                cw.switch_context_window(
                    source_context_id=int(self.ctx['id']),
                    source_context_epoch=int(self.ctx['context_epoch']),
                    count=0,
                    request_id=req,
                    db_path=self.db,
                    hooks=hooks,
                )
            self.assertIn('mark committed boom', str(ar.exception))

            binding = dr.get_local_binding()
            self.assertIsNotNone(binding)
            target_id = int(binding.context_id)
            target_gen = int(binding.resident_generation)
            owner = dc.get_resident_owner(target_id, target_gen, db_path=self.db)
            cursor = dc.get_resident_history_cursor(target_id, target_gen, db_path=self.db)
            self.assertIsNotNone(owner)
            self.assertIsNotNone(cursor)
            first_staged = staged_box['obj']
            self.assertIs(holder.get(), first_staged)
            self.assertTrue(first_staged._alive())
            self.assertEqual(old.kill_calls, 0)

            conn = dc._connect(self.db)
            try:
                st = conn.execute(
                    'SELECT status FROM context_switch_intents WHERE request_id=?',
                    (req,),
                ).fetchone()['status']
            finally:
                conn.close()
            self.assertEqual(st, cw.INTENT_HANDOFF_PENDING)
            files = list(Path(hooks.claude_home).rglob('*.jsonl'))
            self.assertTrue(files and files[0].is_file())
            jsonl_bytes = files[0].read_bytes()

            # Kill staged inside holder after successful handoff + mark fail.
            first_staged._kill(quiet=True)
            self.assertFalse(first_staged._alive())
            self.assertIs(holder.get(), first_staged)

            result_probe = {
                'target_context_id': target_id,
                'target_context_epoch': int(binding.context_epoch),
                'resident_generation': target_gen,
                'selected_message_ids': [],
                'boundary_message_id': int(cursor),
                'claude_session_id': str(binding.claude_session_id),
            }
            # Document the old bug: matcher without holder still True.
            self.assertTrue(dr.target_resident_binding_matches(
                result_probe,
                session_id=str(binding.claude_session_id),
                db_path=self.db,
            ))
            # Fixed recovery path must not use that result: with holder → False.
            self.assertFalse(dr.target_resident_binding_matches(
                result_probe,
                session_id=str(binding.claude_session_id),
                holder=holder,
                db_path=self.db,
            ))

            prepare_before = staged_box['prepare_n']
            forge_before = forge_calls['n']
            self.assertEqual(prepare_before, 1)

            out = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=0,
                request_id=req,
                db_path=self.db,
                hooks=hooks,
            )

        # Old bug was prepare_n 1→1; fixed must be 1→2.
        self.assertEqual(forge_calls['n'], forge_before)
        self.assertEqual(forge_calls['n'], 1)
        self.assertEqual(staged_box['prepare_n'], 2)
        new_staged = staged_box['obj']
        self.assertIsNot(new_staged, first_staged)
        self.assertTrue(new_staged._alive())
        self.assertIs(holder.get(), new_staged)
        self.assertEqual(old.kill_calls, 1)
        self.assertEqual(mark_calls['n'], 2)

        conn = dc._connect(self.db)
        try:
            st2 = conn.execute(
                'SELECT status FROM context_switch_intents WHERE request_id=?',
                (req,),
            ).fetchone()['status']
        finally:
            conn.close()
        self.assertEqual(st2, cw.INTENT_COMMITTED)
        self.assertEqual(files[0].read_bytes(), jsonl_bytes)
        self.assertTrue(dr.target_resident_binding_matches(
            out,
            session_id=out['claude_session_id'],
            holder=holder,
            expected_resident=new_staged,
            db_path=self.db,
        ))

        # Formal first turn on target must be hot.
        u = _insert(self.db, 'hayana', 'after-recovery', '2026-07-27 20:00:00')
        with mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            plan = dr.prepare_daily_turn(
                user_message_id=u,
                db_path=self.db,
                resident=new_staged,
                static_system='S',
                now=datetime.datetime(2026, 7, 27, 20, 0, 0),
                wall_now=datetime.datetime(2026, 7, 27, 20, 0, 0),
            )
        self.assertFalse(plan.is_cold)
        self.assertEqual(plan.manifest.get('turn_kind'), 'hot')


class FrontendRequestIdTests(unittest.TestCase):
    def test_controller_reuses_request_id_source(self):
        # Source-level assertion: pendingRequestId field exists in controller source.
        src = Path(ROOT) / 'app/src/lib/manualContextWindowController.ts'
        text = src.read_text(encoding='utf-8')
        self.assertIn('pendingRequestId', text)
        self.assertIn('if (!this.pendingRequestId)', text)
        self.assertIn('crypto.randomUUID()', text)


class UnforgeableOrderTests(unittest.TestCase):
    def test_trailing_user_unforgeable(self):
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        u = _insert(db, 'hayana', 'only-user', '2026-07-27 10:00:00')
        conn = dc._connect(db)
        try:
            with self.assertRaises(CarryoverUnforgeableError):
                build_events_from_selected_messages(
                    conn,
                    selected_message_ids=[u],
                    target_session_id=str(uuid.uuid4()),
                    cwd='/tmp',
                )
        finally:
            conn.close()
            os.unlink(db)


class ForgeDbMultimodalCarryoverTests(unittest.TestCase):
    """DB-authoritative manual window Forge must preserve vision blocks."""

    def setUp(self):
        self.db = _tmp_db()
        _init_chat_messages(self.db)
        dc.ensure_schema(self.db)
        self.forge_root = tempfile.mkdtemp(prefix='forge-vision-')
        self.hooks = cw.offline_switch_hooks(self.forge_root)
        self.upload_dir = Path(self.forge_root) / 'uploads'
        self.upload_dir.mkdir(parents=True)
        self.pixel_marker = _random_vision_marker()
        self.png_name = 'img_%s.png' % uuid.uuid4().hex
        _make_vision_png(self.upload_dir / self.png_name, self.pixel_marker)
        self.image_ref = '/static/uploads/%s' % self.png_name
        self.assertNotIn(self.pixel_marker, self.png_name)
        self.assertNotIn(self.pixel_marker, self.image_ref)
        self.ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=self.db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        self._resolve_patch = mock.patch(
            'chat.cc_vision_bridge.resolve_image_bytes',
            side_effect=lambda ref, **kw: resolve_image_bytes(
                ref,
                upload_dir=str(self.upload_dir),
                attach_dir=str(self.upload_dir),
            ),
        )
        self._resolve_patch.start()

    def tearDown(self):
        self._resolve_patch.stop()
        try:
            os.unlink(self.db)
        except OSError:
            pass

    def _forge_one_round(self, user_content: str, *, image_url: str = ''):
        ts_u = '2026-07-27 10:00:00'
        ts_a = '2026-07-27 10:01:00'
        if image_url:
            u = _insert_with_image(self.db, 'hayana', user_content, image_url, ts_u)
        else:
            u = _insert(self.db, 'hayana', user_content, ts_u)
        a = _insert(self.db, 'fyodor', ASSISTANT_IMAGE_ACK, ts_a)
        _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), u, 'user')
        _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), a, 'assistant')
        conn = dc._connect(self.db)
        try:
            return forge_target_session_from_db(
                conn,
                selected_message_ids=[u, a],
                cwd=self.hooks.forge_cwd,
                claude_home=self.hooks.claude_home,
            )
        finally:
            conn.close()

    def test_A_db_forge_text_plus_image_carryover(self):
        forged = self._forge_one_round('看看这个', image_url=self.image_ref)
        events = load_jsonl(forged.jsonl_path)
        user_content = events[0]['message']['content']
        self.assertIsInstance(user_content, list)
        text_blocks = [b for b in user_content if b.get('type') == 'text']
        img_blocks = [b for b in user_content if b.get('type') == 'image']
        self.assertEqual(len(text_blocks), 1)
        self.assertEqual(text_blocks[0]['text'], '看看这个')
        self.assertEqual(len(img_blocks), 1)
        self.assertEqual(img_blocks[0]['source']['type'], 'base64')
        self.assertTrue(img_blocks[0]['source']['data'])
        _assert_marker_absent_from_textual_metadata(
            self,
            marker=self.pixel_marker,
            events=events,
            path=forged.jsonl_path,
        )
        vr = validate_forged_transcript(events, session_id=forged.target_session_id)
        self.assertTrue(vr.ok, vr.errors)

    def test_B_db_forge_image_only_carryover(self):
        forged = self._forge_one_round('', image_url=self.image_ref)
        events = load_jsonl(forged.jsonl_path)
        user_content = events[0]['message']['content']
        self.assertIsInstance(user_content, list)
        self.assertEqual(len(user_content), 1)
        self.assertEqual(user_content[0]['type'], 'image')
        self.assertTrue(user_content[0]['source']['data'])
        self.assertNotIn({'type': 'text', 'text': ''}, user_content)
        _assert_marker_absent_from_textual_metadata(
            self,
            marker=self.pixel_marker,
            events=events,
            path=forged.jsonl_path,
        )

    def test_C_db_forge_text_only_regression(self):
        forged = self._forge_one_round('今天天气怎么样')
        events = load_jsonl(forged.jsonl_path)
        self.assertEqual(events[0]['message']['content'], '今天天气怎么样')
        self.assertIsInstance(events[0]['message']['content'], str)
        self.assertEqual(
            events[1]['message']['content'],
            [{'type': 'text', 'text': ASSISTANT_IMAGE_ACK}],
        )

    def test_D_db_forge_bad_image_degrades_or_fails_closed(self):
        bad_ref = '/static/uploads/missing.png'
        forged = self._forge_one_round('看看这个', image_url=bad_ref)
        events = load_jsonl(forged.jsonl_path)
        self.assertEqual(events[0]['message']['content'], '看看这个')

        with self.assertRaises(CarryoverUnforgeableError):
            self._forge_one_round('', image_url=bad_ref)

    def test_E_switch_seam_uses_db_forge_multimodal(self):
        base = datetime.datetime(2026, 7, 27, 10, 0, 0)
        for i in range(2):
            ts_u = (base + datetime.timedelta(minutes=i * 2)).strftime('%Y-%m-%d %H:%M:%S')
            ts_a = (base + datetime.timedelta(minutes=i * 2 + 1)).strftime('%Y-%m-%d %H:%M:%S')
            u = _insert(self.db, 'hayana', 'user-%d' % i, ts_u)
            a = _insert(self.db, 'fyodor', 'asst-%d' % i, ts_a)
            _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), u, 'user')
            _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), a, 'assistant')
        ts_u = (base + datetime.timedelta(minutes=4)).strftime('%Y-%m-%d %H:%M:%S')
        ts_a = (base + datetime.timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')
        u = _insert_with_image(self.db, 'hayana', '看看这个', self.image_ref, ts_u)
        a = _insert(self.db, 'fyodor', ASSISTANT_IMAGE_ACK, ts_a)
        _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), u, 'user')
        _map(self.db, int(self.ctx['id']), int(self.ctx['context_epoch']), a, 'assistant')

        with mock.patch('chat.context_window.enabled', return_value=True):
            out = cw.switch_context_window(
                source_context_id=int(self.ctx['id']),
                source_context_epoch=int(self.ctx['context_epoch']),
                count=3,
                request_id=str(uuid.uuid4()),
                db_path=self.db,
                hooks=self.hooks,
            )
        jsonl_files = list(Path(self.hooks.claude_home).rglob('*.jsonl'))
        self.assertTrue(jsonl_files)
        events = load_jsonl(jsonl_files[0])
        image_events = [
            evt['message']['content']
            for evt in events
            if evt.get('type') == 'user' and isinstance(evt['message']['content'], list)
            and any(b.get('type') == 'image' for b in evt['message']['content'])
        ]
        self.assertEqual(len(image_events), 1)
        user_content = image_events[0]
        self.assertTrue(any(
            b.get('type') == 'text' and b.get('text') == '看看这个'
            for b in user_content
        ))
        self.assertIn('claude_session_id', out)


def _live_resume_claude_env(claude_home: Path) -> dict[str, str]:
    """Seed host subscription auth into isolated claude_home for VPS manual gate."""
    from scripts.spike_claude_forge_resume import isolated_claude_env

    fake_home = claude_home.parent / 'fake-home'
    fake_home.mkdir(parents=True, exist_ok=True)
    host = Path(os.environ.get('HOME', '/root'))
    host_claude = host / '.claude'
    creds = host_claude / '.credentials.json'
    if creds.is_file():
        shutil.copy2(creds, claude_home / '.credentials.json')
    host_json = host / '.claude.json'
    if host_json.is_file():
        shutil.copy2(host_json, fake_home / '.claude.json')
    env = isolated_claude_env(claude_home)
    env['HOME'] = str(fake_home)
    return env


@unittest.skipUnless(
    os.environ.get('HAYA_VISION_LIVE') == '1' and shutil.which('claude'),
    'live claude --resume probe requires HAYA_VISION_LIVE=1 and claude CLI',
)
class ForgeDbMultimodalLiveResumeTests(unittest.TestCase):
    """Real --resume from DB-forged JSONL (manual / VPS only).

    Pixel canary is randomized at runtime and must appear only in PNG pixels.
    Round-2 differential gates showed native --resume PASS but DB-forged
    text-only resume FAIL (DB_FORGE_BASELINE); this opt-in gate remains the
    final vision acceptance check once that baseline is fixed outside this PR.
    """

    def test_live_resume_reads_db_forged_image(self):
        import hashlib

        from scripts.spike_claude_forge_resume import _run_claude
        from tools.claude_forge_live_gate import (
            parse_stdout_events,
            verify_jsonl_prefix_unchanged,
        )

        marker = _random_vision_marker()
        root = tempfile.mkdtemp(prefix='forge-vision-live-')
        upload_dir = Path(root) / 'uploads'
        upload_dir.mkdir(parents=True)
        png_name = 'img_%s.png' % uuid.uuid4().hex
        png_path = upload_dir / png_name
        _make_vision_png(png_path, marker)
        png_sha = hashlib.sha256(png_path.read_bytes()).hexdigest()
        image_ref = '/static/uploads/%s' % png_name
        self.assertNotIn(marker, png_name)
        self.assertNotIn(marker, image_ref)
        hooks = cw.offline_switch_hooks(root)
        db = _tmp_db()
        _init_chat_messages(db)
        dc.ensure_schema(db)
        ctx = dc.get_or_create_daily_context(
            local_day='2026-07-27',
            db_path=db,
            now=datetime.datetime(2026, 7, 27, 10, 0, 0),
        )
        ts_u = '2026-07-27 10:00:00'
        ts_a = '2026-07-27 10:01:00'
        u = _insert_with_image(db, 'hayana', '看看这个', image_ref, ts_u)
        a = _insert(db, 'fyodor', ASSISTANT_IMAGE_ACK, ts_a)
        self.assertNotIn(marker, ASSISTANT_IMAGE_ACK)
        _map(db, int(ctx['id']), int(ctx['context_epoch']), u, 'user')
        _map(db, int(ctx['id']), int(ctx['context_epoch']), a, 'assistant')
        with mock.patch(
            'chat.cc_vision_bridge.resolve_image_bytes',
            side_effect=lambda ref, **kw: resolve_image_bytes(
                ref, upload_dir=str(upload_dir), attach_dir=str(upload_dir),
            ),
        ):
            conn = dc._connect(db)
            try:
                forged = forge_target_session_from_db(
                    conn,
                    selected_message_ids=[u, a],
                    cwd=hooks.forge_cwd,
                    claude_home=hooks.claude_home,
                )
            finally:
                conn.close()
        path = forged.jsonl_path
        before_bytes = path.read_bytes()
        events = load_jsonl(path)
        user_content = events[0]['message']['content']
        self.assertIsInstance(user_content, list)
        types = [b.get('type') for b in user_content if isinstance(b, dict)]
        self.assertEqual(types, ['text', 'image'])
        img = next(b for b in user_content if b.get('type') == 'image')
        self.assertTrue(img['source']['data'])
        decoded = base64.b64decode(img['source']['data'])
        self.assertEqual(hashlib.sha256(decoded).hexdigest(), png_sha)
        _assert_marker_absent_from_textual_metadata(
            self, marker=marker, events=events, path=path,
        )
        env = _live_resume_claude_env(Path(hooks.claude_home))
        prompt = '上一窗口那张图片中央写了什么？只回答图片中的文字。'
        self.assertNotIn(marker, prompt)
        payload = json.dumps(
            {'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            ensure_ascii=False,
        ) + '\n'
        cmd = [
            'claude', '-p',
            '--resume', forged.target_session_id,
            '--input-format', 'stream-json',
            '--output-format', 'stream-json',
            '--verbose',
            '--include-partial-messages',
            '--max-turns', '3',
            '--tools', '',
            '--allowedTools', '',
        ]
        # Keep default modest: longer timeouts cannot locate DB Forge baseline hangs.
        timeout = float(os.environ.get('HAYA_VISION_PROBE_TIMEOUT', '90'))
        run = _run_claude(
            cmd=cmd,
            cwd=hooks.forge_cwd,
            env=env,
            stdin_payload=payload,
            timeout_seconds=timeout,
        )
        raw = parse_stdout_events(run.stdout_lines)
        after_bytes = path.read_bytes()
        self.assertTrue(run.process_started, run.stderr_text)
        self.assertFalse(run.timed_out, run.stderr_text)
        self.assertEqual(run.exit_code, 0, run.stderr_text)
        self.assertTrue(raw.saw_text_delta, raw)
        self.assertTrue(raw.result_ok, raw)
        self.assertTrue(verify_jsonl_prefix_unchanged(before_bytes, after_bytes))
        self.assertGreater(len(after_bytes), len(before_bytes))
        self.assertEqual(
            raw.assistant_text.strip(),
            marker,
            {
                'assistant_text': raw.assistant_text,
                'pixel_sha256': png_sha,
                'stderr': run.stderr_text[:500],
            },
        )


if __name__ == '__main__':
    unittest.main()
