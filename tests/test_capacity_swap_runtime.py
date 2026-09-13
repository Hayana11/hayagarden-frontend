"""P-CONTEXT-WINDOW Step 8-B — Capacity Swap Runtime contract tests.

Minimal matrix only (A–E). No new test infrastructure / no live Claude.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident
import config_store
from chat import daily_context as dc
from chat import daily_runtime as dr
from chat.capacity_swap import (
    CAPACITY_SWAP_REASONS,
    AnchorStatus,
    CapacitySwapCandidate,
)
from chat.capacity_swap_runtime import (
    CAPACITY_BOUNDARY_REPRESENTATION,
    CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1,
    CAPACITY_SWAP_REGISTRY_SOURCE,
    CapacitySwapRuntimeError,
    CapacitySwapRuntimeResult,
    clear_same_context_last_good_for_tests,
    effective_static_system_for_registry,
    forbid_resend_after_stdin_flush,
    is_capacity_swap_reason,
    prepare_capacity_swap_for_context_plan,
    resolve_finalize_registry_source,
    try_restore_same_context_last_good,
    with_capacity_boundary_suffix,
)
from chat.session_registry import (
    get_context_claude_session,
    register_context_claude_session,
)
from tools.cc_jsonl_usage import session_jsonl_path

_FIXED_NOW = datetime.datetime(2026, 7, 27, 10, 0, 0)
_REAL_PREPARE = dr.prepare_daily_turn


def _init_chat_messages(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT, content TEXT, thinking TEXT DEFAULT '',
            tool_calls TEXT DEFAULT '', cache_info TEXT DEFAULT '',
            choices TEXT DEFAULT '', image_url TEXT DEFAULT '',
            created_at TEXT
        )'''
    )
    conn.commit()
    conn.close()
    dc.ensure_schema(db_path)


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


def _prepare(db: str, uid: int, **kwargs):
    with mock.patch.object(config_store, 'get_bool', return_value=True), \
         mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
        return _REAL_PREPARE(
            user_message_id=uid,
            db_path=db,
            now=_FIXED_NOW,
            wall_now=_FIXED_NOW,
            static_system=kwargs.pop('static_system', 'STATIC_PERSONA'),
            **kwargs,
        )


def _candidate(**overrides) -> CapacitySwapCandidate:
    raw = '{"type":"user","message":{"content":"kept"}}\n'
    base = dict(
        source_context_id=1,
        source_context_epoch=1,
        source_resident_generation=1,
        source_claude_session_id='sid-old',
        source_transcript_path='/tmp/old.jsonl',
        source_scan_offset=10,
        source_sha256='abc',
        target_resident_generation=2,
        candidate_session_id='sid-new-cap',
        trigger_reason='soft_context',
        anchor_status=AnchorStatus.ANCHOR_UNAVAILABLE,
        anchor_message_id=0,
        anchor_event_uuid=None,
        selected_round_count=1,
        selected_message_ids=(1,),
        estimated_tokens=10,
        serialized_bytes=len(raw.encode('utf-8')),
        event_count=1,
        output_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
        serialized_jsonl=raw,
        boundary_required=True,
        warnings=(),
    )
    base.update(overrides)
    return CapacitySwapCandidate(**base)


class _DoorResident:
    """Minimal resident for door-lock / capacity routing tests."""

    def __init__(self, peek_reason: Optional[str], *, session_id: str = 'sid-old'):
        self._peek_reason = peek_reason
        self.session_id = session_id
        self.generation = 1
        self.cwd = '/tmp'
        self.tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
        self.sent: list[str] = []
        self._alive = True
        self._spawn_args: list = []
        self.ensure_alive_systems: list[str] = []
        self.send_count = 0

    def peek_respawn_reason(self, system_text, *, tool_profile=None):
        return self._peek_reason

    def ensure_alive(self, system_text, env, tool_profile=None):
        self.ensure_alive_systems.append(str(system_text))
        self._spawn_args.append((system_text, tool_profile))
        self._alive = True
        return False

    def send_turn(self, content, commit_meta=None, on_stdin_flushed=None, turn_lease=None):
        self.send_count += 1
        self.sent.append(str(content))
        if on_stdin_flushed is not None:
            on_stdin_flushed()
        yield ('text', 'ok')
        yield ('done', ('ok', '', {'input_tokens': 1, 'output_tokens': 1}, {}))

    def mark_generation(self, gen: int) -> None:
        self.generation = int(gen)

    def _kill(self, quiet=True):
        self._alive = False


class CapacitySwapRuntimeContractTests(unittest.TestCase):
    def setUp(self):
        dr.reset_bindings_for_tests()
        clear_same_context_last_good_for_tests()
        self.tmp = tempfile.mkdtemp(prefix='cap-swap-rt-')
        self.db = os.path.join(self.tmp, 'test.db')
        _init_chat_messages(self.db)

    def tearDown(self):
        clear_same_context_last_good_for_tests()
        dr.reset_bindings_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ----- Test A: capacity reason routing -----

    def test_a_capacity_reasons_allowlisted(self):
        for reason in ('soft_context', 'hard_context', 'turn_limit'):
            self.assertTrue(is_capacity_swap_reason(reason), reason)
            self.assertIn(reason, CAPACITY_SWAP_REASONS)
        for reason in ('system_changed', 'process_dead', 'idle', 'history_rewrite', 'unknown'):
            self.assertFalse(is_capacity_swap_reason(reason), reason)

    def test_a_door_marks_capacity_swap_only_for_allowlist(self):
        uid = _insert(self.db, 'hayana', 'route-a', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id='sid-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        res_cap = _DoorResident('soft_context')
        door = dr.peek_registered_respawn_decision(plan, res_cap, 'STATIC_PERSONA')
        self.assertTrue(door['requires_respawn'])
        self.assertTrue(door['capacity_swap'])
        self.assertEqual(door['reason'], 'soft_context')

        res_sys = _DoorResident('system_changed')
        door2 = dr.peek_registered_respawn_decision(plan, res_sys, 'STATIC_PERSONA')
        self.assertTrue(door2['requires_respawn'])
        self.assertFalse(door2['capacity_swap'])
        self.assertEqual(door2['reason'], 'system_changed')

    def test_a_non_capacity_uses_generic_reprepare_not_capacity_swap(self):
        uid = _insert(self.db, 'hayana', 'route-generic', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id='sid-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        resident = _DoorResident('system_changed')
        calls = {'capacity': 0, 'generic': 0}

        def _cap(*_a, **_k):
            calls['capacity'] += 1
            return {'ok': False}

        def _generic(p, **_k):
            calls['generic'] += 1
            resident._peek_reason = None
            return p

        with mock.patch.object(dr, '_attempt_capacity_swap_before_stdin', side_effect=_cap), \
             mock.patch.object(dr, 'reprepare_after_registered_session_change', side_effect=_generic):
            list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))
        self.assertEqual(calls['capacity'], 0)
        self.assertEqual(calls['generic'], 1)
        self.assertEqual(resident.send_count, 1)

    def test_a_soft_context_enters_capacity_swap(self):
        uid = _insert(self.db, 'hayana', 'route-soft', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id='sid-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        resident = _DoorResident('soft_context')
        calls = {'capacity': 0}

        def _cap(p, **kwargs):
            calls['capacity'] += 1
            self.assertEqual(kwargs.get('trigger_reason'), 'soft_context')
            return {'ok': False, 'error_code': 'staged_health_failed'}

        def _generic(p, **_k):
            resident._peek_reason = None
            return p

        with mock.patch.object(dr, '_attempt_capacity_swap_before_stdin', side_effect=_cap), \
             mock.patch.object(dr, 'reprepare_after_registered_session_change', side_effect=_generic):
            list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))
        self.assertEqual(calls['capacity'], 1)
        self.assertEqual(resident.send_count, 1)

    # ----- Test B: same-context identity -----

    def test_b_same_context_identity_on_successful_swap(self):
        uid = _insert(self.db, 'hayana', 'id-b', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        src_ctx = int(plan.context_id)
        src_epoch = int(plan.context_epoch)
        src_gen = int(plan.resident_generation)
        register_context_claude_session(
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            chat_id=plan.chat_id,
            claude_session_id='sid-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        resident = _DoorResident('soft_context', session_id='sid-old')
        resident.cwd = self.tmp

        new_sid = '11111111-1111-1111-1111-111111111111'
        staged = _DoorResident(None, session_id=new_sid)
        staged.generation = 2
        cand = _candidate(
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
        )
        jsonl_path = session_jsonl_path(self.tmp, new_sid)
        assert jsonl_path is not None
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(cand.serialized_jsonl, encoding='utf-8')
        handoff = CapacitySwapRuntimeResult(
            ok=True,
            candidate=cand,
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            jsonl_path=str(jsonl_path),
            jsonl_sha256='deadbeef',
            effective_system=with_capacity_boundary_suffix('STATIC_PERSONA'),
        )
        setattr(handoff, 'staged_resident', staged)

        def _fake_reprepare(p, **_k):
            refreshed = dc.get_daily_context_by_id(p.context_id, db_path=self.db) or {}
            p.resident_generation = int(refreshed.get('resident_generation') or 0)
            p.context_epoch = int(refreshed.get('context_epoch') or p.context_epoch)
            p.epoch_token = dict(p.epoch_token)
            p.epoch_token['resident_generation'] = p.resident_generation
            p.epoch_token['context_epoch'] = p.context_epoch
            p.manifest = dict(p.manifest)
            p.is_cold = False
            p.is_respawn = False
            return p

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff), \
             mock.patch.object(dr, 'reprepare_after_capacity_swap', side_effect=_fake_reprepare):
            out = dr._attempt_capacity_swap_before_stdin(
                plan,
                resident=resident,
                static_system='STATIC_PERSONA',
                env={},
                trigger_reason='soft_context',
            )
        self.assertTrue(out.get('ok'), out)
        self.assertEqual(int(plan.context_id), src_ctx)
        self.assertEqual(int(plan.context_epoch), src_epoch)
        self.assertEqual(int(plan.resident_generation), src_gen + 1)
        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertIsNotNone(reg)
        self.assertEqual(reg['source'], CAPACITY_SWAP_REGISTRY_SOURCE)
        self.assertEqual(reg['claude_session_id'], new_sid)
        self.assertEqual(
            plan.manifest.get('capacity_boundary_representation'),
            CAPACITY_BOUNDARY_REPRESENTATION,
        )

    def test_real_seam_binding_survives_reprepare_to_recursive_door(self):
        """LIVE_FAIL regression: gen1 binding must not kill staged gen2 before send.

        Real Runtime seam — mock ONLY run_capacity_swap_handoff. Do not mock
        reprepare_after_capacity_swap / prepare_daily_turn / stale close / door.
        """
        uid = _insert(self.db, 'hayana', 'real-seam-bind', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        src_ctx = int(plan.context_id)
        src_epoch = int(plan.context_epoch)
        src_gen = int(plan.resident_generation)
        register_context_claude_session(
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            chat_id=plan.chat_id,
            claude_session_id='sid-seam-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        # Source gen1 LocalResidentBinding — the LIVE_FAIL precondition.
        dr.set_local_binding(dr.LocalResidentBinding(
            resident_key=plan.resident_key,
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            bound_cursor_message_id=plan.cursor_before,
            process_generation=1,
            tool_profile=plan.tool_profile,
            claude_session_id='sid-seam-old',
        ))
        self.assertEqual(dr.get_local_binding().resident_generation, src_gen)

        class _Proc:
            def __init__(self):
                self.alive = True
                self.killed = False

            def poll(self):
                return None if self.alive else 0

            def kill(self):
                self.killed = True
                self.alive = False

        old_proc = _Proc()
        new_proc = _Proc()
        resident = _DoorResident('turn_limit', session_id='sid-seam-old')
        resident.cwd = self.tmp
        resident._proc = old_proc
        resident.generation = 1

        new_sid = '77777777-7777-7777-7777-777777777777'
        staged = _DoorResident(None, session_id=new_sid)
        staged.generation = 2
        staged._proc = new_proc
        cand = _candidate(
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            trigger_reason='turn_limit',
        )
        jsonl_path = session_jsonl_path(self.tmp, new_sid)
        assert jsonl_path is not None
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(cand.serialized_jsonl, encoding='utf-8')
        handoff = CapacitySwapRuntimeResult(
            ok=True,
            candidate=cand,
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            jsonl_path=str(jsonl_path),
            jsonl_sha256='deadbeef',
            effective_system=with_capacity_boundary_suffix('STATIC_PERSONA'),
        )
        setattr(handoff, 'staged_resident', staged)

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff):
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))

        self.assertTrue(any(e[0] == 'done' for e in events))
        self.assertEqual(resident.send_count, 1)
        # New staged process must survive until CURRENT user send (not stale-killed).
        self.assertFalse(new_proc.killed)
        self.assertTrue(bool(getattr(resident, '_alive', True)))
        self.assertEqual(int(plan.resident_generation), src_gen + 1)
        self.assertEqual(int(plan.context_id), src_ctx)
        self.assertEqual(int(plan.context_epoch), src_epoch)

        binding = dr.get_local_binding()
        self.assertIsNotNone(binding)
        self.assertEqual(int(binding.resident_generation), src_gen + 1)
        self.assertEqual(binding.resident_key, plan.resident_key)
        self.assertEqual(binding.claude_session_id, new_sid)
        self.assertEqual(int(binding.process_generation), int(resident.generation))
        self.assertEqual(int(resident.generation), 2)
        self.assertEqual(resident.session_id, new_sid)

        reg = get_context_claude_session(src_ctx, src_gen + 1, db_path=self.db)
        self.assertIsNotNone(reg)
        self.assertEqual(reg['source'], CAPACITY_SWAP_REGISTRY_SOURCE)
        self.assertNotEqual(
            plan.manifest.get('error_code'),
            'registered_session_generation_mismatch',
        )
        self.assertNotEqual(plan.manifest.get('error_code'), 'process_dead')

    # ----- Test C: boundary persistence -----

    def test_c_suffix_persistent_and_absent_from_jsonl(self):
        base = 'persona-body'
        first = with_capacity_boundary_suffix(base)
        second = with_capacity_boundary_suffix(first)
        self.assertEqual(first, second)
        self.assertIn(CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1, first)
        self.assertEqual(first.count('[容量边界]'), 1)

        reg = {'source': CAPACITY_SWAP_REGISTRY_SOURCE}
        eff = effective_static_system_for_registry(base, reg)
        self.assertEqual(eff, first)
        eff2 = effective_static_system_for_registry(base, reg)
        self.assertEqual(eff, eff2)

        blob = '{"type":"user","message":{"content":"hello"}}\n'
        self.assertNotIn('[容量边界]', blob)
        self.assertNotIn('context-window-boundary', blob)

    # ----- Test D: CURRENT user exactly-once -----

    def test_d_successful_capacity_path_sends_user_once(self):
        uid = _insert(self.db, 'hayana', 'once-d', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id='sid-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        resident = _DoorResident('soft_context', session_id='sid-old')

        def _cap(p, **_k):
            # Swap success: capacity cleared; keep session_id aligned with registry
            # so recursive door-lock does not trip identity mismatch.
            resident._peek_reason = None
            p.manifest['capacity_swap'] = True
            return {
                'ok': True,
                'effective_system': with_capacity_boundary_suffix('STATIC_PERSONA'),
            }

        with mock.patch.object(dr, '_attempt_capacity_swap_before_stdin', side_effect=_cap):
            list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))
        self.assertEqual(resident.send_count, 1)

    def test_d_pre_flush_fallback_safe_post_flush_forbid(self):
        forbid_resend_after_stdin_flush(current_user_sent=False, action='test')
        with self.assertRaises(CapacitySwapRuntimeError) as ctx:
            forbid_resend_after_stdin_flush(current_user_sent=True, action='fallback_resend')
        self.assertEqual(ctx.exception.error_code, 'current_user_already_sent')

        uid = _insert(self.db, 'hayana', 'flush-d', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        with self.assertRaises(CapacitySwapRuntimeError):
            try_restore_same_context_last_good(
                plan=plan,
                live_resident=_DoorResident(None),
                current_user_stdin_flushed=True,
            )

    # ----- Test E: registry source preservation -----

    def test_e_finalize_preserves_capacity_swap_source(self):
        self.assertEqual(
            resolve_finalize_registry_source({'source': 'capacity_swap'}),
            'capacity_swap',
        )
        self.assertEqual(resolve_finalize_registry_source(None), 'daily_runtime')
        self.assertEqual(resolve_finalize_registry_source({'source': ''}), 'daily_runtime')

        uid = _insert(self.db, 'hayana', 'src-e', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        dc.respawn_daily_resident(plan.context_id, db_path=self.db)
        refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=self.db)
        plan.resident_generation = int(refreshed['resident_generation'])
        plan.epoch_token = dict(plan.epoch_token)
        plan.epoch_token['resident_generation'] = plan.resident_generation

        sid = '22222222-2222-2222-2222-222222222222'
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id=sid,
            cwd=self.tmp,
            source=CAPACITY_SWAP_REGISTRY_SOURCE,
            scan_offset=0,
            process_generation=2,
            db_path=self.db,
        )
        derived = session_jsonl_path(self.tmp, sid)
        assert derived is not None
        Path(derived).parent.mkdir(parents=True, exist_ok=True)
        Path(derived).write_text('{"type":"user"}\n', encoding='utf-8')

        plan.transcript_claude_session_id = sid
        plan.transcript_cwd = self.tmp
        plan.transcript_path = str(derived)
        plan.transcript_start_offset = 0
        plan.transcript_end_offset = Path(derived).stat().st_size
        plan.transcript_process_generation = 2

        aid = _insert(self.db, 'assistant', 'y', '2026-07-27 10:01:00')
        with mock.patch('chat.daily_runtime.run_mapping_pass') as mp:
            mp.return_value = mock.Mock(
                ok=False, error_code='mapping_blocked', mapped_event_uuids=[],
                registry=get_context_claude_session(
                    plan.context_id, plan.resident_generation, db_path=self.db,
                ),
                scan_offset=0,
            )
            dr.finalize_transcript_mapping_after_success(plan, assistant_message_id=aid)

        reg = get_context_claude_session(
            plan.context_id, plan.resident_generation, db_path=self.db,
        )
        self.assertEqual(reg['source'], CAPACITY_SWAP_REGISTRY_SOURCE)

    def test_e_finalize_still_defaults_daily_runtime_for_new(self):
        self.assertEqual(
            resolve_finalize_registry_source(None, default='daily_runtime'),
            'daily_runtime',
        )

    # ----- Narrow-fix regressions (Owner review blockers) -----

    def test_cursor_seed_failure_fail_closed(self):
        """Cursor/owner seed failure must not continue into hot-like reprepare."""
        uid = _insert(self.db, 'hayana', 'seed-fail', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        dc.respawn_daily_resident(plan.context_id, db_path=self.db)
        with mock.patch.object(dc, 'upsert_resident_owner', side_effect=RuntimeError('boom')):
            with self.assertRaises(dr.DailyRuntimeError) as ctx:
                dr.reprepare_after_capacity_swap(
                    plan,
                    resident=_DoorResident(None),
                    static_system='STATIC_PERSONA',
                    cursor_watermark=0,
                )
        self.assertEqual(ctx.exception.error_code, 'capacity_swap_cursor_seed_failed')

        # _attempt must roll back to old resident when post-install seed fails.
        src_ctx = int(plan.context_id)
        src_epoch = int(plan.context_epoch)
        src_gen = int(plan.resident_generation)
        # Reset gen for a clean attempt: use current after previous bump.
        refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=self.db) or {}
        plan.resident_generation = int(refreshed['resident_generation'])
        plan.epoch_token = dict(plan.epoch_token)
        plan.epoch_token['resident_generation'] = plan.resident_generation
        src_gen = int(plan.resident_generation)

        register_context_claude_session(
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            chat_id=plan.chat_id,
            claude_session_id='sid-old-seed',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )

        class _Proc:
            def __init__(self):
                self.alive = True
                self.killed = False

            def poll(self):
                return None if self.alive else 0

            def kill(self):
                self.killed = True
                self.alive = False

        old_proc = _Proc()
        new_proc = _Proc()
        resident = _DoorResident('soft_context', session_id='sid-old-seed')
        resident.cwd = self.tmp
        resident._proc = old_proc

        new_sid = '33333333-3333-3333-3333-333333333333'
        staged = _DoorResident(None, session_id=new_sid)
        staged.generation = 2
        staged._proc = new_proc
        cand = _candidate(
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
        )
        jsonl_path = session_jsonl_path(self.tmp, new_sid)
        assert jsonl_path is not None
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(cand.serialized_jsonl, encoding='utf-8')
        handoff = CapacitySwapRuntimeResult(
            ok=True,
            candidate=cand,
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            jsonl_path=str(jsonl_path),
            jsonl_sha256='deadbeef',
            effective_system=with_capacity_boundary_suffix('STATIC_PERSONA'),
        )
        setattr(handoff, 'staged_resident', staged)

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff), \
             mock.patch.object(dc, 'upsert_resident_owner', side_effect=RuntimeError('seed-boom')):
            out = dr._attempt_capacity_swap_before_stdin(
                plan,
                resident=resident,
                static_system='STATIC_PERSONA',
                env={},
                trigger_reason='soft_context',
            )
        self.assertFalse(out.get('ok'), out)
        self.assertEqual(out.get('error_code'), 'capacity_swap_cursor_seed_failed')
        self.assertTrue(out.get('rolled_back_to_last_good'))
        self.assertTrue(out.get('target_generation_unregistered'))
        self.assertIs(resident._proc, old_proc)
        self.assertFalse(old_proc.killed)
        self.assertTrue(new_proc.killed)
        # Registry must not exist for the bumped target generation.
        target_gen = int(out.get('target_generation') or (src_gen + 1))
        self.assertIsNone(
            get_context_claude_session(src_ctx, target_gen, db_path=self.db),
        )

    def test_install_defers_old_proc_kill_until_explicit_close(self):
        """Old last-good proc must survive install; only deferred close kills it."""

        class _Proc:
            def __init__(self):
                self.killed = False

            def poll(self):
                return None

            def kill(self):
                self.killed = True

        old_proc = _Proc()
        new_proc = _Proc()
        live = _DoorResident(None, session_id='old')
        live._proc = old_proc
        live.session_id = 'old'
        staged = _DoorResident(None, session_id='new')
        staged._proc = new_proc
        staged.session_id = 'new'
        staged.generation = 9

        state = dr.install_capacity_swap_into_live_resident(
            live_resident=live, staged_resident=staged,
        )
        self.assertIs(state['old_proc'], old_proc)
        self.assertFalse(old_proc.killed)
        self.assertIs(live._proc, new_proc)
        self.assertEqual(live.session_id, 'new')
        self.assertIsNone(staged._proc)

        # Rollback restores old and kills new.
        dr.rollback_capacity_swap_install(live_resident=live, install_state=state)
        self.assertIs(live._proc, old_proc)
        self.assertEqual(live.session_id, 'old')
        self.assertTrue(new_proc.killed)
        self.assertFalse(old_proc.killed)

        # Deferred close is the only intentional kill of old.
        live2 = _DoorResident(None, session_id='old2')
        old2 = _Proc()
        new2 = _Proc()
        live2._proc = old2
        staged2 = _DoorResident(None, session_id='new2')
        staged2._proc = new2
        state2 = dr.install_capacity_swap_into_live_resident(
            live_resident=live2, staged_resident=staged2,
        )
        self.assertFalse(old2.killed)
        dr.close_deferred_capacity_swap_old_proc(state2['old_proc'])
        self.assertTrue(old2.killed)

    def test_capacity_swap_publish_mode_0600_and_path_fence(self):
        from chat.capacity_swap_runtime import publish_capacity_swap_candidate_jsonl

        home = Path(self.tmp) / '.claude'
        cwd = str(Path(self.tmp) / 'proj')
        os.makedirs(cwd, exist_ok=True)
        cand = _candidate(candidate_session_id='44444444-4444-4444-4444-444444444444')
        path, digest = publish_capacity_swap_candidate_jsonl(
            cand, cwd=cwd, claude_home=str(home),
        )
        self.assertTrue(path.exists())
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))
        self.assertTrue(digest)

        # Path fence: refuse escape outside allowed claude home.
        with mock.patch(
            'chat.capacity_swap_runtime.derive_transcript_path',
            return_value=str(Path(self.tmp) / 'escape' / 'evil.jsonl'),
        ):
            with self.assertRaises(CapacitySwapRuntimeError) as ctx:
                publish_capacity_swap_candidate_jsonl(
                    cand, cwd=cwd, claude_home=str(home),
                )
        self.assertEqual(ctx.exception.error_code, 'publish_path_fence_failed')

    def test_full_chain_seed_fail_cold_fallback_sends_once(self):
        """soft_context → staged ok → seed fail → rollback → cold → send once."""
        uid = _insert(self.db, 'hayana', 'full-seed', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        src_ctx = int(plan.context_id)
        src_epoch = int(plan.context_epoch)
        src_gen = int(plan.resident_generation)
        register_context_claude_session(
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            chat_id=plan.chat_id,
            claude_session_id='sid-full-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )

        class _Proc:
            def __init__(self):
                self.alive = True
                self.killed = False

            def poll(self):
                return None if self.alive else 0

            def kill(self):
                self.killed = True
                self.alive = False

        old_proc = _Proc()
        new_proc = _Proc()
        resident = _DoorResident('soft_context', session_id='sid-full-old')
        resident.cwd = self.tmp
        resident._proc = old_proc
        resident.generation = 1

        new_sid = '55555555-5555-5555-5555-555555555555'
        staged = _DoorResident(None, session_id=new_sid)
        staged.generation = 2
        staged._proc = new_proc
        cand = _candidate(
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
        )
        jsonl_path = session_jsonl_path(self.tmp, new_sid)
        assert jsonl_path is not None
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(cand.serialized_jsonl, encoding='utf-8')
        handoff = CapacitySwapRuntimeResult(
            ok=True,
            candidate=cand,
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            jsonl_path=str(jsonl_path),
            jsonl_sha256='deadbeef',
            effective_system=with_capacity_boundary_suffix('STATIC_PERSONA'),
        )
        setattr(handoff, 'staged_resident', staged)

        def _seed_boom(*_a, **_k):
            raise dr.DailyRuntimeError(
                'seed fail', error_code='capacity_swap_cursor_seed_failed',
            )

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff), \
             mock.patch.object(dr, 'reprepare_after_capacity_swap', side_effect=_seed_boom):
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))

        self.assertTrue(any(e[0] == 'done' for e in events))
        self.assertEqual(resident.send_count, 1)
        # Bumped target generation must not carry a capacity_swap Registry ghost.
        self.assertIsNone(
            get_context_claude_session(src_ctx, src_gen + 1, db_path=self.db),
        )
        # No second-door mismatch blow-up.
        self.assertNotEqual(
            plan.manifest.get('error_code'),
            'registered_session_generation_mismatch',
        )

    def test_full_chain_pre_flush_send_failure_continues_fallback_send_once(self):
        """CapSwap success → pre-flush send fail → rollback → cold → send once."""
        uid = _insert(self.db, 'hayana', 'full-flush', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        src_ctx = int(plan.context_id)
        src_epoch = int(plan.context_epoch)
        src_gen = int(plan.resident_generation)
        register_context_claude_session(
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            chat_id=plan.chat_id,
            claude_session_id='sid-flush-old',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )
        # Source gen1 binding — rollback must restore this with old resident.
        source_binding = dr.LocalResidentBinding(
            resident_key=plan.resident_key,
            context_id=src_ctx,
            context_epoch=src_epoch,
            resident_generation=src_gen,
            bound_cursor_message_id=plan.cursor_before,
            process_generation=1,
            tool_profile=plan.tool_profile,
            claude_session_id='sid-flush-old',
        )
        dr.set_local_binding(source_binding)

        class _Proc:
            def __init__(self):
                self.alive = True
                self.killed = False

            def poll(self):
                return None if self.alive else 0

            def kill(self):
                self.killed = True
                self.alive = False

        class _PreFlushThenOkResident(_DoorResident):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.fail_preflush_once = True
                self.attempt_count = 0
                self.binding_at_first_send = None

            def send_turn(self, content, commit_meta=None, on_stdin_flushed=None, turn_lease=None):
                self.attempt_count += 1
                if self.fail_preflush_once:
                    self.fail_preflush_once = False
                    self.binding_at_first_send = dr.get_local_binding()
                    proc = getattr(self, '_proc', None)
                    if proc is not None:
                        proc.kill()
                    raise BrokenPipeError('stdin broken before flush')
                self.send_count += 1
                self.sent.append(str(content))
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                yield ('text', 'ok-after-fallback')
                yield ('done', ('ok-after-fallback', '', {'input_tokens': 1, 'output_tokens': 1}, {}))

        old_proc = _Proc()
        new_proc = _Proc()
        resident = _PreFlushThenOkResident('soft_context', session_id='sid-flush-old')
        resident.cwd = self.tmp
        resident._proc = old_proc

        new_sid = '66666666-6666-6666-6666-666666666666'
        staged = _DoorResident(None, session_id=new_sid)
        staged.generation = 2
        staged._proc = new_proc
        cand = _candidate(
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
        )
        jsonl_path = session_jsonl_path(self.tmp, new_sid)
        assert jsonl_path is not None
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).write_text(cand.serialized_jsonl, encoding='utf-8')
        handoff = CapacitySwapRuntimeResult(
            ok=True,
            candidate=cand,
            source_context_id=src_ctx,
            source_context_epoch=src_epoch,
            source_resident_generation=src_gen,
            target_resident_generation=src_gen + 1,
            candidate_session_id=new_sid,
            jsonl_path=str(jsonl_path),
            jsonl_sha256='deadbeef',
            effective_system=with_capacity_boundary_suffix('STATIC_PERSONA'),
        )
        setattr(handoff, 'staged_resident', staged)

        rollback_obs: dict = {}
        _orig_rollback = dr._rollback_capacity_swap_if_unflushed

        def _observe_rollback(p, *, resident):
            rollback_obs['binding_before'] = dr.get_local_binding()
            rollback_obs['session_before'] = getattr(resident, 'session_id', None)
            out = _orig_rollback(p, resident=resident)
            rollback_obs['binding_after'] = dr.get_local_binding()
            rollback_obs['session_after'] = getattr(resident, 'session_id', None)
            rollback_obs['proc_after'] = getattr(resident, '_proc', None)
            return out

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})), \
             mock.patch.object(dr, 'run_capacity_swap_handoff', return_value=handoff), \
             mock.patch.object(dr, '_rollback_capacity_swap_if_unflushed', side_effect=_observe_rollback):
            events = list(dr.stream_daily_resident_turn(
                plan, resident=resident, env={}, static_system='STATIC_PERSONA',
            ))

        self.assertTrue(any(e[0] == 'done' for e in events))
        self.assertEqual(resident.send_count, 1)
        self.assertGreaterEqual(resident.attempt_count, 2)
        self.assertTrue(plan.manifest.get('capacity_swap_pre_flush_rollback'))
        self.assertTrue(plan.manifest.get('capacity_swap_pre_flush_cold_fallback'))
        self.assertTrue(new_proc.killed)

        # CapSwap had advanced binding to gen2 before CURRENT user pre-flush fail.
        self.assertIsNotNone(resident.binding_at_first_send)
        self.assertEqual(
            int(resident.binding_at_first_send.resident_generation), src_gen + 1,
        )
        self.assertEqual(resident.binding_at_first_send.claude_session_id, new_sid)
        # Rollback restores old resident + old gen1 binding together.
        self.assertTrue(rollback_obs)
        before_b = rollback_obs['binding_before']
        after_b = rollback_obs['binding_after']
        self.assertIsNotNone(before_b)
        self.assertEqual(int(before_b.resident_generation), src_gen + 1)
        self.assertIsNotNone(after_b)
        self.assertEqual(int(after_b.resident_generation), src_gen)
        self.assertEqual(after_b.claude_session_id, 'sid-flush-old')
        self.assertEqual(after_b.resident_key, source_binding.resident_key)
        self.assertEqual(rollback_obs['session_after'], 'sid-flush-old')
        self.assertIs(rollback_obs['proc_after'], old_proc)

        # Identity: CapSwap target Registry may remain as history, but must not be current.
        cap_reg = get_context_claude_session(src_ctx, src_gen + 1, db_path=self.db)
        self.assertIsNotNone(cap_reg)
        self.assertEqual(cap_reg['source'], CAPACITY_SWAP_REGISTRY_SOURCE)
        self.assertEqual(int(plan.context_id), src_ctx)
        self.assertEqual(int(plan.context_epoch), src_epoch)
        self.assertGreater(int(plan.resident_generation), src_gen + 1)
        self.assertNotEqual(int(plan.resident_generation), src_gen + 1)
        current_reg = get_context_claude_session(
            src_ctx, int(plan.resident_generation), db_path=self.db,
        )
        # Current cold gen has no CapSwap Registry ghost as the live target.
        if current_reg is not None:
            self.assertNotEqual(current_reg.get('source'), CAPACITY_SWAP_REGISTRY_SOURCE)
        self.assertNotEqual(
            plan.manifest.get('error_code'),
            'registered_session_generation_mismatch',
        )

    def test_post_flush_failure_fail_closed_no_resend(self):
        """After stdin flush, failure must not rollback/resend CURRENT user."""
        uid = _insert(self.db, 'hayana', 'post-flush', '2026-07-27 10:00:00')
        plan = _prepare(self.db, uid)
        register_context_claude_session(
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            chat_id=plan.chat_id,
            claude_session_id='sid-post',
            cwd=self.tmp,
            source='daily_runtime',
            scan_offset=10,
            process_generation=1,
            db_path=self.db,
        )

        class _PostFlushBoom(_DoorResident):
            def send_turn(self, content, commit_meta=None, on_stdin_flushed=None, turn_lease=None):
                self.send_count += 1
                self.sent.append(str(content))
                if on_stdin_flushed is not None:
                    on_stdin_flushed()
                yield ('text', 'partial')
                raise RuntimeError('provider boom after flush')

        resident = _PostFlushBoom(None, session_id='sid-post')
        # Pretend CapSwap installed and awaiting flush commit.
        old_proc = mock.Mock()
        old_proc.poll.return_value = None
        plan._capacity_swap_install_state = {  # type: ignore[attr-defined]
            'old_proc': old_proc,
            'old_attrs': {'_proc': old_proc, 'session_id': 'old'},
        }
        plan._capacity_swap_deferred_old_proc = old_proc  # type: ignore[attr-defined]

        with mock.patch.object(config_store, 'get_bool', return_value=True), \
             mock.patch('chat.daily_history._build_state_text', return_value=('', 'none', {})):
            with self.assertRaises(RuntimeError):
                list(dr.stream_daily_resident_turn(
                    plan, resident=resident, env={}, static_system='STATIC_PERSONA',
                ))

        self.assertEqual(resident.send_count, 1)
        self.assertTrue(bool(getattr(plan, '_current_user_stdin_flushed', False)))
        self.assertIsNone(getattr(plan, '_capacity_swap_install_state', 'missing'))
        old_proc.kill.assert_called()
        self.assertFalse(plan.manifest.get('capacity_swap_pre_flush_cold_fallback'))

    def test_adoption_does_not_leak_old_generation_metadata(self):
        """staged→live adoption resets generation-scoped meta via _reset_session_meta."""

        class _Proc:
            def poll(self):
                return None

            def kill(self):
                pass

        class _MetaResident:
            def __init__(self, sid: str):
                self.cwd = self.tmp if hasattr(self, 'tmp') else '/tmp'
                self.session_id = sid
                self.generation = 1
                self.tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
                self._proc = _Proc()
                self._system_text = 'old-sys'
                self._session_id = sid
                self._cold = False
                self._generation = 1
                self._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
                self._model_identity = 'old-model'
                self._history_rewrite_epoch = 'old-epoch'
                self._last_used = 1.0
                self._last_state_snapshot = {'leak': True}
                self._last_state_send_snapshot = {'leak': True}
                self._last_successful_lean_state = True
                self._last_state_anchor_generation = 7
                self._last_state_schema_version = 'v-old'
                self._turns_since_state_anchor = 9
                self._state_delta_chars_since_anchor = 99
                self._last_state_anchor_version = 'av-old'
                self._committed_file_hashes = {'deadbeef'}
                self._pending_file_hashes = {'cafe'}
                self._last_group_message_id = 42
                self._group_cursor_initialized = True
                self._last_rel_fingerprint = 'rel-old'
                self._turns_since_rel_sent = 3
                self._last_rel_mood = 'mood-old'
                self._keepwarm_lease_expires_at = 12345.0
                self._tool_surface_snapshot = {'old': True}
                self._pending_respawn_reason = None
                self._resident_turn_count = 5
                self._last_round_context = 1
                self._max_round_context = 2
                self._turns_since_respawn = 4

            def _reset_session_meta(self, *, respawn_reason):
                # Mirror ResidentSession._reset_session_meta contract.
                self._resident_turn_count = 0
                self._last_round_context = 0
                self._max_round_context = 0
                self._pending_respawn_reason = respawn_reason
                self._turns_since_respawn = 0
                self._last_state_snapshot = {}
                self._last_state_send_snapshot = {}
                self._last_successful_lean_state = False
                self._last_state_anchor_generation = -1
                self._last_state_schema_version = None
                self._turns_since_state_anchor = 0
                self._state_delta_chars_since_anchor = 0
                self._last_state_anchor_version = None
                self._committed_file_hashes = set()
                self._pending_file_hashes = set()
                self._last_group_message_id = 0
                self._group_cursor_initialized = False
                self._last_rel_fingerprint = None
                self._turns_since_rel_sent = 0
                self._last_rel_mood = None
                self._keepwarm_lease_expires_at = None
                self._tool_surface_snapshot = {}

        live = _MetaResident('old-sid')
        staged = _MetaResident('new-sid')
        staged.generation = 2
        staged._generation = 2
        staged._system_text = 'new-sys'
        staged._pending_respawn_reason = 'capacity_swap'
        staged._tool_surface_snapshot = {'staged': True}
        # staged is clean (as after spawn_resumable)
        staged._reset_session_meta(respawn_reason='capacity_swap')
        staged._tool_surface_snapshot = {'staged': True}

        state = dr.install_capacity_swap_into_live_resident(
            live_resident=live, staged_resident=staged,
        )
        self.assertEqual(live.session_id, 'new-sid')
        self.assertEqual(live._system_text, 'new-sys')
        self.assertEqual(live._last_state_snapshot, {})
        self.assertEqual(live._committed_file_hashes, set())
        self.assertEqual(live._pending_file_hashes, set())
        self.assertFalse(live._group_cursor_initialized)
        self.assertIsNone(live._last_rel_fingerprint)
        self.assertIsNone(live._keepwarm_lease_expires_at)
        self.assertEqual(live._last_state_anchor_generation, -1)
        self.assertEqual(live._tool_surface_snapshot, {'staged': True})
        self.assertEqual(live._pending_respawn_reason, 'capacity_swap')
        self.assertIsNotNone(state['old_attrs'].get('_committed_file_hashes'))
        self.assertIn('deadbeef', state['old_attrs']['_committed_file_hashes'])


class NeverUsedIdleReapContractTests(unittest.TestCase):
    """LIVE_FAIL #2: never-used (_last_used=0) must not trip idle reap."""

    def _alive_matched_session(self, *, last_used: float) -> cc_resident.ResidentSession:
        sess = cc_resident.ResidentSession(self.tmp, '', os.path.join(self.tmp, 'cc-tools.json'))
        proc = mock.Mock()
        proc.poll.return_value = None
        sess._proc = proc
        sess._cold = False
        sess._system_text = 'STATIC_PERSONA'
        sess._tool_profile = cc_resident.TOOL_PROFILE_TEXT_ONLY
        sess._model_identity = None
        sess._history_rewrite_epoch = ''
        sess._last_used = float(last_used)
        sess._resident_turn_count = 0
        sess._last_round_context = 0
        sess._turns_since_respawn = 0
        sess._pending_respawn_reason = 'capacity_swap'
        return sess

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cap-swap-idle-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_never_used_last_used_zero_is_not_idle(self):
        sess = self._alive_matched_session(last_used=0.0)
        self.assertIsNone(sess.peek_idle_seconds())
        with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: d), \
             mock.patch(
                 'chat.cc_history_rewrite.current_history_rewrite_epoch',
                 return_value='',
             ):
            reason = sess.peek_respawn_reason(
                'STATIC_PERSONA',
                tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY,
            )
        self.assertNotEqual(reason, 'idle')
        self.assertIsNone(reason)

    def test_real_idle_after_successful_use_still_reaps(self):
        aged = time.time() - cc_resident.IDLE_REAP_SECONDS - 1.0
        sess = self._alive_matched_session(last_used=aged)
        self.assertIsNotNone(sess.peek_idle_seconds())
        self.assertGreater(sess.peek_idle_seconds(), cc_resident.IDLE_REAP_SECONDS)
        with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: d), \
             mock.patch(
                 'chat.cc_history_rewrite.current_history_rewrite_epoch',
                 return_value='',
             ):
            reason = sess.peek_respawn_reason(
                'STATIC_PERSONA',
                tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY,
            )
        self.assertEqual(reason, 'idle')


class ContextPlanCapacityCarrierTests(unittest.TestCase):
    def test_zero_raw_blocks_before_legacy_selector(self):
        plan = mock.Mock(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            user_message_id=9,
            db_path=None,
        )
        context_plan = mock.Mock(representations=())
        with mock.patch(
            'chat.capacity_swap_runtime.prepare_capacity_swap_for_plan',
        ) as legacy_prepare:
            result = prepare_capacity_swap_for_context_plan(
                plan=plan,
                context_plan=context_plan,
                trigger_reason='soft_context',
                static_system='STATIC_PERSONA',
            )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.error_code,
            'context_plan_capacity_resume_seed_missing',
        )
        legacy_prepare.assert_not_called()


    def test_context_plan_raw_selection_preserves_tool_events(self):
        from chat.claude_transcript_model import (
            CandidateConversationRound,
            EventRole,
            EventType,
            TranscriptEvent,
            TranscriptGraph,
        )
        from chat.claude_transcript_transform import (
            SelectionPolicy,
            SidechainPolicy,
            SummaryPolicy,
            ThinkingPolicy,
            transform_transcript as real_transform_transcript,
        )

        def event(uid, role, event_type, raw, line):
            return TranscriptEvent(
                event_uuid=uid,
                parent_uuid=None,
                session_id='source-session',
                event_role=role,
                event_type=event_type,
                raw=raw,
                line_number=line,
            )

        source_events = [
            event(
                'u1', EventRole.CANDIDATE_USER, EventType.USER,
                {'type': 'user', 'message': {
                    'role': 'user', 'content': 'old-one',
                }},
                1,
            ),
            event(
                'a1', EventRole.ASSISTANT, EventType.ASSISTANT,
                {'type': 'assistant', 'message': {
                    'role': 'assistant', 'content': [{
                        'type': 'text', 'text': 'answer-one',
                    }],
                }},
                2,
            ),
            event(
                'u2', EventRole.CANDIDATE_USER, EventType.USER,
                {'type': 'user', 'message': {
                    'role': 'user', 'content': 'old-two',
                }},
                3,
            ),
            event(
                'a2', EventRole.ASSISTANT, EventType.ASSISTANT,
                {'type': 'assistant', 'message': {
                    'role': 'assistant', 'content': [{
                        'type': 'text', 'text': 'answer-two',
                    }],
                }},
                4,
            ),
            event(
                'u3', EventRole.CANDIDATE_USER, EventType.USER,
                {'type': 'user', 'message': {
                    'role': 'user', 'content': 'old-tool-user',
                }},
                5,
            ),
            event(
                'a3', EventRole.ASSISTANT, EventType.ASSISTANT,
                {'type': 'assistant', 'message': {
                    'role': 'assistant', 'content': [{
                        'type': 'tool_use',
                        'id': 'tool-3',
                        'name': 'lookup',
                        'input': {'q': 'x'},
                    }],
                }},
                6,
            ),
            event(
                't3', EventRole.TOOL_RESULT_USER, EventType.USER,
                {'type': 'user', 'message': {
                    'role': 'user', 'content': [{
                        'type': 'tool_result',
                        'tool_use_id': 'tool-3',
                        'content': 'TOOL_OUTCOME',
                    }],
                }, 'sourceToolUseID': 'tool-3'},
                7,
            ),
        ]
        graph = TranscriptGraph(
            session_id='source-session',
            events=source_events,
            by_uuid={item.event_uuid: item for item in source_events},
            candidate_rounds=[
                CandidateConversationRound(
                    candidate_user_event_uuid='u1',
                    event_uuids=('u1', 'a1'),
                    has_assistant=True,
                ),
                CandidateConversationRound(
                    candidate_user_event_uuid='u2',
                    event_uuids=('u2', 'a2'),
                    has_assistant=True,
                ),
                CandidateConversationRound(
                    candidate_user_event_uuid='u3',
                    event_uuids=('u3', 'a3', 't3'),
                    tool_use_ids=('tool-3',),
                    has_assistant=True,
                ),
            ],
        )
        member_one = types.SimpleNamespace(
            seq=1,
            source_ref='turn:1:2',
            source_revision='rev-one',
            source_kind='completed_turn',
            content_hash='hash-one',
            span_start=None,
            span_end=None,
            branch_id='active',
        )
        member_tool = types.SimpleNamespace(
            seq=3,
            source_ref='turn:5:6',
            source_revision='rev-tool',
            source_kind='completed_turn',
            content_hash='hash-tool',
            span_start=None,
            span_end=None,
            branch_id='active',
        )
        context_plan = types.SimpleNamespace(
            plan_hash='plan-hash',
            representations=(types.SimpleNamespace(
                kind='raw',
                representation_id='raw:exact',
                source_members=(member_one, member_tool),
            ),),
        )
        plan = types.SimpleNamespace(
            context_id=7,
            context_epoch=3,
            resident_generation=1,
            user_message_id=99,
            cursor_before=10,
            db_path='db',
            chat_id='default',
            transcript_cwd='/tmp',
            plan_hash='ignored-plan-field',
        )
        registry = {
            'scan_status': 'ready',
            'transcript_path': '/tmp/source.jsonl',
            'claude_session_id': 'source-session',
            'scan_offset': 123,
            'last_mapped_message_id': 10,
        }
        fake_conn = mock.Mock()
        formal = [{'id': value} for value in (1, 2, 3, 4, 5, 6, 7)]
        canonical = {
            'u1': 'CANONICAL_ONE',
            'u2': 'CANONICAL_TWO',
            'u3': 'CANONICAL_TOOL_USER',
        }
        mid_to_event = {1: 'u1', 3: 'u2', 5: 'u3'}
        event_to_mid = {
            'u1': 1, 'a1': 2, 'u2': 3, 'a2': 4,
            'u3': 5, 'a3': 6, 't3': 7,
        }
        source_members = {
            'turn:1:2': member_one,
            'turn:5:6': member_tool,
        }

        with mock.patch(
            'chat.capacity_swap_runtime.get_context_claude_session',
            return_value=registry,
        ), mock.patch(
            'chat.capacity_swap_runtime.latest_complete_assistant_watermark',
            return_value=10,
        ), mock.patch(
            'chat.capacity_swap_runtime.registry_mapping_lags_watermark',
            return_value=False,
        ), mock.patch(
            'chat.capacity_swap_runtime._snapshot_prefix_sha256',
            return_value='a' * 64,
        ), mock.patch(
            'chat.capacity_swap_runtime.read_transcript_range',
            return_value=graph,
        ), mock.patch.object(
            dc, '_connect', return_value=fake_conn,
        ), mock.patch(
            'chat.capacity_swap_runtime._load_formal_messages_excluding_current',
            return_value=formal,
        ), mock.patch(
            'chat.capacity_swap_runtime._load_mapping_for_messages',
            return_value=(canonical, mid_to_event, event_to_mid),
        ), mock.patch(
            'chat.capacity_swap_runtime._capacity_context_source_member_from_db',
            side_effect=lambda **kwargs: source_members[
                'turn:%d:%d' % (kwargs['user_id'], kwargs['assistant_id'])
            ],
        ), mock.patch(
            'chat.capacity_swap_runtime.prepare_capacity_swap_for_plan',
        ) as legacy_prepare, mock.patch(
            'chat.capacity_swap_runtime.transform_transcript',
            wraps=real_transform_transcript,
        ) as transform:
            result = dr.prepare_capacity_swap_for_context_plan(
                plan=plan,
                context_plan=context_plan,
                trigger_reason='soft_context',
                static_system='STATIC_PERSONA',
            )

        self.assertTrue(result.ok, result.warnings)
        candidate = result.candidate
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.selected_round_count, 2)
        self.assertEqual(
            candidate.selected_message_ids,
            (1, 2, 5, 6, 7),
        )
        self.assertIn('CANONICAL_ONE', candidate.serialized_jsonl)
        self.assertIn('CANONICAL_TOOL_USER', candidate.serialized_jsonl)
        self.assertIn('TOOL_OUTCOME', candidate.serialized_jsonl)
        self.assertNotIn('CANONICAL_TWO', candidate.serialized_jsonl)
        self.assertNotIn('old-two', candidate.serialized_jsonl)
        self.assertEqual(
            candidate.serialized_jsonl.count('"type":"tool_use"'),
            1,
        )
        self.assertEqual(
            candidate.serialized_jsonl.count('"type":"tool_result"'),
            1,
        )
        request = transform.call_args.args[1]
        self.assertEqual(request.selection_policy, SelectionPolicy.FIXED_ROUND_COUNT)
        self.assertEqual(request.keep_rounds, 2)
        self.assertEqual(
            request.exclude_round_candidate_uuids,
            frozenset({'u2'}),
        )
        self.assertEqual(request.thinking_policy, ThinkingPolicy.DROP)
        self.assertEqual(request.sidechain_policy, SidechainPolicy.EXCLUDE)
        self.assertEqual(request.summary_policy, SummaryPolicy.DROP)
        legacy_prepare.assert_not_called()


if __name__ == '__main__':
    unittest.main()
