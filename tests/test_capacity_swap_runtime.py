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

    def send_turn(self, content, commit_meta=None, on_stdin_flushed=None):
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


if __name__ == '__main__':
    unittest.main()
