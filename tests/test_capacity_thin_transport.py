"""Thin Capacity Swap: ContextPlan selects WHAT; Capacity only transports it.

Each raw representation travels as native source rounds when every member is
mapped to this transcript generation, otherwise as one readable continuity
materialization. Broken native mappings fail closed instead of downgrading.
"""
from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from chat import daily_context as dc
from chat import daily_runtime
from chat.capacity_swap_runtime import (
    CapacitySwapStagedHooks,
    prepare_capacity_swap_for_context_plan,
    run_capacity_swap_handoff,
)
from chat.session_registry import SCAN_STATUS_BLOCKED
from chat.claude_transcript_model import (
    CandidateConversationRound,
    EventRole,
    EventType,
    TranscriptEvent,
    TranscriptGraph,
)
from continuity.materialization import materialize_source_members
from continuity.sources import (
    build_source_snapshot,
    derive_autonomous_events,
    derive_completed_turns,
)

SID = 'source-session'
WAKE_CACHE = json.dumps({
    'wake_mode': 'normal', 'canonical_chat_history': True,
    'unified_chat_resident': True, 'b3_authority': True,
    'source': 'wake', 'provider': 'claude_code',
})


def _row(mid, author, content, *, wake=False):
    return {
        'id': mid, 'author': author, 'content': content, 'thinking': '',
        'source_kind': 'wake' if wake else 'chat',
        'created_at': '2026-11-07 10:%02d:00' % (mid % 60),
        'attachments': '[]', 'cache_info': WAKE_CACHE if wake else '',
        'tool_calls': '', 'branches': '', 'branch_idx': 0,
    }


# The production incident shape: completed turn, two legacy Wakes, turn.
ROWS = [
    _row(14716, 'hayana', 'TURN_ONE_USER'),
    _row(14717, 'assistant', 'TURN_ONE_ASSISTANT'),
    _row(14718, 'assistant', 'WAKE_14718_BODY', wake=True),
    _row(14719, 'assistant', 'WAKE_14719_BODY', wake=True),
    _row(14720, 'hayana', 'TURN_TWO_USER'),
    _row(14721, 'assistant', 'TURN_TWO_ASSISTANT'),
]


def _members():
    snapshot = build_source_snapshot(
        turns=derive_completed_turns(ROWS),
        events=derive_autonomous_events(ROWS),
        local_day='2026-11-07', source_watermark=14721,
        created_at='2026-11-07T12:00:00Z',
    )
    return tuple(snapshot.members)


def _event(uid, role, etype, content, offset, **extra):
    raw = {'type': etype.value, 'message': {
        'role': 'user' if etype == EventType.USER else 'assistant',
        'content': content,
    }}
    raw.update(extra)
    return TranscriptEvent(
        event_uuid=uid, parent_uuid=None, session_id=SID, event_role=role,
        event_type=etype, raw=raw, line_number=offset // 10, byte_offset=offset,
    )


def _graph(*, wake_native: bool):
    events = [
        _event('t1u', EventRole.CANDIDATE_USER, EventType.USER, 'TURN_ONE_USER', 10),
        _event('t1a', EventRole.ASSISTANT, EventType.ASSISTANT,
               [{'type': 'text', 'text': 'TURN_ONE_ASSISTANT'}], 20),
    ]
    rounds = [CandidateConversationRound(
        candidate_user_event_uuid='t1u', event_uuids=('t1u', 't1a'), has_assistant=True,
    )]
    if wake_native:
        events += [
            _event('w1i', EventRole.CANDIDATE_USER, EventType.USER, 'NATIVE_WAKE_PROMPT', 30),
            _event('w1t', EventRole.ASSISTANT, EventType.ASSISTANT, [{
                'type': 'tool_use', 'id': 'tool-w', 'name': 'lookup', 'input': {'q': 'x'},
            }], 40),
            _event('w1r', EventRole.TOOL_RESULT_USER, EventType.USER, [{
                'type': 'tool_result', 'tool_use_id': 'tool-w', 'content': 'WAKE_TOOL_OUTCOME',
            }], 50, sourceToolUseID='tool-w'),
            _event('w1a', EventRole.ASSISTANT, EventType.ASSISTANT,
                   [{'type': 'text', 'text': 'WAKE_14718_BODY'}], 60),
        ]
        rounds.append(CandidateConversationRound(
            candidate_user_event_uuid='w1i', event_uuids=('w1i', 'w1t', 'w1r', 'w1a'),
            tool_use_ids=('tool-w',), has_assistant=True,
        ))
    events += [
        _event('t2u', EventRole.CANDIDATE_USER, EventType.USER, 'TURN_TWO_USER', 70),
        _event('t2a', EventRole.ASSISTANT, EventType.ASSISTANT,
               [{'type': 'text', 'text': 'TURN_TWO_ASSISTANT'}], 80),
    ]
    rounds.append(CandidateConversationRound(
        candidate_user_event_uuid='t2u', event_uuids=('t2u', 't2a'), has_assistant=True,
    ))
    return TranscriptGraph(
        session_id=SID, events=events,
        by_uuid={item.event_uuid: item for item in events}, candidate_rounds=rounds,
    )


def _mapping(uid, mid, role, offset, **override):
    row = {
        'event_uuid': uid, 'message_id': mid, 'role': role,
        'claude_session_id': SID, 'context_id': 7, 'context_epoch': 3,
        'resident_generation': 1, 'jsonl_byte_offset': offset,
    }
    row.update(override)
    return row


TURN_ROWS = (
    _mapping('t1u', 14716, 'user', 10),
    _mapping('t1a', 14717, 'assistant', 20),
    _mapping('t2u', 14720, 'user', 70),
    _mapping('t2a', 14721, 'assistant', 80),
)
WAKE_ROWS = (
    _mapping('w1i', 14718, 'provider_input', 30),
    _mapping('w1t', 14718, 'tool_use', 40),
    _mapping('w1r', 14718, 'tool_result_user', 50),
    _mapping('w1a', 14718, 'assistant', 60),
)


def _context_plan(members, *, anchor_status='ANCHOR_UNAVAILABLE', chunk=False):
    representations = []
    if chunk:
        representations.append(types.SimpleNamespace(
            kind='chunk', representation_id='chunk:older', source_members=(),
        ))
    representations.append(types.SimpleNamespace(
        kind='raw', representation_id='raw:recent', source_members=tuple(members),
    ))
    return types.SimpleNamespace(
        plan_hash='plan-hash', window_anchor=None,
        window_anchor_status=anchor_status, representations=tuple(representations),
    )


def _plan(user_message_id=14800):
    return types.SimpleNamespace(
        context_id=7, context_epoch=3, resident_generation=1,
        user_message_id=user_message_id, cursor_before=14799, db_path='db',
        chat_id='default', transcript_cwd='/tmp',
    )


class ThinCapacityPrepareTests(unittest.TestCase):
    def _prepare(self, members, *, graph, rows, plan=None, registry=None):
        registry = registry or {
            'scan_status': 'ready', 'transcript_path': '/tmp/source.jsonl',
            'claude_session_id': SID, 'scan_offset': 1000,
            'last_mapped_message_id': 14799,
        }
        canonical = {'t1u': 'TURN_ONE_USER', 't2u': 'TURN_TWO_USER'}
        with mock.patch(
            'chat.capacity_swap_runtime.get_context_claude_session', return_value=registry,
        ), mock.patch(
            'chat.capacity_swap_runtime.latest_complete_assistant_watermark', return_value=14799,
        ), mock.patch(
            'chat.capacity_swap_runtime.registry_mapping_lags_watermark', return_value=False,
        ), mock.patch(
            'chat.capacity_swap_runtime._snapshot_prefix_sha256', return_value='a' * 64,
        ), mock.patch(
            'chat.capacity_swap_runtime.read_transcript_range', return_value=graph,
        ), mock.patch.object(
            dc, '_connect', return_value=types.SimpleNamespace(close=lambda: None),
        ), mock.patch(
            'chat.capacity_swap_runtime._load_formal_messages_excluding_current',
            return_value=[{'id': 14716}, {'id': 14717}, {'id': 14720}, {'id': 14721}],
        ), mock.patch(
            'chat.capacity_swap_runtime._load_mapping_for_messages',
            return_value=(canonical, {14716: 't1u', 14720: 't2u'}, {}),
        ), mock.patch(
            'chat.capacity_swap_runtime._load_mapping_rows_for_messages', return_value=rows,
        ), mock.patch(
            'chat.capacity_swap_runtime.prepare_capacity_swap_for_plan',
        ) as legacy:
            result = prepare_capacity_swap_for_context_plan(
                plan=plan or _plan(), context_plan=_context_plan(members),
                trigger_reason='soft_context', static_system='STATIC',
            )
        legacy.assert_not_called()
        return result

    def test_case1_legacy_wakes_send_the_whole_representation_readable(self):
        members = _members()
        self.assertEqual(
            [member.source_ref for member in members],
            ['turn:14716:14717', 'wake:14718', 'wake:14719', 'turn:14720:14721'],
        )
        result = self._prepare(members, graph=_graph(wake_native=False), rows=TURN_ROWS)

        self.assertTrue(result.ok, (result.error_code, result.warnings))
        self.assertNotEqual(result.error_code, 'context_plan_capacity_raw_source_invalid')
        candidate = result.candidate
        self.assertEqual(candidate.readable_raw_representation_ids, ('raw:recent',))
        # The mapped turns beside the Wakes are not installed natively too.
        self.assertEqual(candidate.event_count, 0)
        self.assertEqual(candidate.serialized_jsonl, '')
        self.assertEqual(candidate.selected_message_ids, ())

        body = materialize_source_members(members, ROWS).body
        positions = [body.index(text) for text in (
            'TURN_ONE_USER', 'WAKE_14718_BODY', 'WAKE_14719_BODY', 'TURN_TWO_ASSISTANT',
        )]
        self.assertEqual(positions, sorted(positions))
        for text in ('WAKE_14718_BODY', 'WAKE_14719_BODY', 'TURN_ONE_USER'):
            self.assertEqual(body.count(text), 1)

    def test_case2_mapped_wake_travels_natively_with_all_its_events(self):
        members = [m for m in _members() if m.source_ref != 'wake:14719']
        result = self._prepare(
            members, graph=_graph(wake_native=True), rows=TURN_ROWS + WAKE_ROWS,
        )

        self.assertTrue(result.ok, (result.error_code, result.warnings))
        candidate = result.candidate
        self.assertEqual(candidate.readable_raw_representation_ids, ())
        self.assertEqual(candidate.selected_round_count, 3)
        jsonl = candidate.serialized_jsonl
        order = [jsonl.index(text) for text in (
            'TURN_ONE_USER', 'NATIVE_WAKE_PROMPT', 'WAKE_TOOL_OUTCOME',
            'WAKE_14718_BODY', 'TURN_TWO_ASSISTANT',
        )]
        self.assertEqual(order, sorted(order))
        # The provider input is carried verbatim, not swapped for a chat row.
        self.assertEqual(jsonl.count('NATIVE_WAKE_PROMPT'), 1)
        self.assertEqual(jsonl.count('"type":"tool_use"'), 1)
        self.assertEqual(jsonl.count('"type":"tool_result"'), 1)
        self.assertEqual(
            candidate.selected_message_ids, (14716, 14717, 14718, 14720, 14721),
        )

    def test_case3_pure_completed_turns_stay_native(self):
        members = [m for m in _members() if m.source_kind == 'completed_turn']
        result = self._prepare(members, graph=_graph(wake_native=False), rows=TURN_ROWS)
        self.assertTrue(result.ok, (result.error_code, result.warnings))
        self.assertEqual(result.candidate.readable_raw_representation_ids, ())
        self.assertEqual(result.candidate.selected_round_count, 2)
        self.assertEqual(
            result.candidate.selected_message_ids, (14716, 14717, 14720, 14721),
        )

    def test_mapping_of_another_generation_is_not_native_here(self):
        rows = tuple(dict(row, resident_generation=0) for row in TURN_ROWS)
        members = [m for m in _members() if m.source_kind == 'completed_turn']
        result = self._prepare(members, graph=_graph(wake_native=False), rows=rows)
        self.assertTrue(result.ok, result.error_code)
        self.assertEqual(result.candidate.readable_raw_representation_ids, ('raw:recent',))

    def test_case5_broken_mapping_fails_closed_instead_of_downgrading(self):
        members = _members()  # the legacy Wakes alone would allow readable
        broken = {
            'uuid_not_in_transcript': TURN_ROWS[:3] + (
                _mapping('ghost', 14721, 'assistant', 80),),
            'session_mismatch': TURN_ROWS[:3] + (
                _mapping('t2a', 14721, 'assistant', 80, claude_session_id='other'),),
            'offset_beyond_prefix': TURN_ROWS[:3] + (
                _mapping('t2a', 14721, 'assistant', 1000),),
            'offset_differs': TURN_ROWS[:3] + (
                _mapping('t2a', 14721, 'assistant', 81),),
            'duplicate_uuid': TURN_ROWS + (_mapping('t2a', 14721, 'assistant', 80),),
            'partly_mapped_turn': TURN_ROWS[:3],
        }
        for name, rows in broken.items():
            with self.subTest(name):
                result = self._prepare(members, graph=_graph(wake_native=False), rows=rows)
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.error_code, 'context_plan_capacity_raw_mapping_invalid',
                )

    def test_case5_blocked_registry_fails_closed(self):
        result = self._prepare(
            _members(), graph=_graph(wake_native=False), rows=TURN_ROWS,
            registry={
                'scan_status': SCAN_STATUS_BLOCKED, 'transcript_path': '/tmp/source.jsonl',
                'claude_session_id': SID, 'scan_offset': 1000,
                'last_mapped_message_id': 14799,
            },
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'registry_blocked')

    def test_native_round_carrying_an_unselected_message_fails_closed(self):
        members = [m for m in _members() if m.source_ref == 'turn:14716:14717']
        graph = _graph(wake_native=False)
        graph.candidate_rounds[0] = CandidateConversationRound(
            candidate_user_event_uuid='t1u', event_uuids=('t1u', 't1a', 't2a'),
            has_assistant=True,
        )
        result = self._prepare(members, graph=graph, rows=TURN_ROWS)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'context_plan_capacity_raw_round_foreign')

    def test_case6_current_user_never_enters_either_carrier(self):
        for rows in (TURN_ROWS, TURN_ROWS + WAKE_ROWS):
            with self.subTest(native_wake=len(rows) > 4):
                result = self._prepare(
                    _members(), graph=_graph(wake_native=len(rows) > 4), rows=rows,
                    plan=_plan(user_message_id=14720),
                )
                self.assertFalse(result.ok)
                self.assertEqual(
                    result.error_code, 'context_plan_capacity_current_user_in_candidate',
                )


class ThinCapacityBootstrapTests(unittest.TestCase):
    def test_case4_chunk_and_readable_raw_install_once_in_plan_order(self):
        members = _members()
        context_plan = _context_plan(members, chunk=True)
        plan = types.SimpleNamespace(
            capacity_context_plan=context_plan,
            capacity_context_chunk_bodies={'older': 'OLDER_CHUNK'},
            assembly={'layers': [], 'manifest': {}},
            capacity_context_bootstrap={},
            user_message_id=14800,
        )
        materialized = materialize_source_members(members, ROWS)
        projected_history = {
            'layers': [], 'manifest': {},
            'context_plan_representation_blocks': [
                {'kind': 'chunk', 'representation_id': 'chunk:older', 'body': 'OLDER_CHUNK'},
                {'kind': 'raw', 'representation_id': 'raw:recent',
                 'body': materialized.body},
            ],
        }
        with mock.patch.object(
            daily_runtime, '_project_context_plan_history', return_value=projected_history,
        ):
            plan.capacity_context_bootstrap['readable_raw_blocks'] = (
                daily_runtime._capacity_readable_raw_blocks(
                    plan, context_plan=context_plan, chunk_bodies={},
                    readable_ids=('raw:recent',),
                )
            )
            assembly = daily_runtime._project_capacity_context_bootstrap(plan)

        blocks = assembly['context_plan_representation_blocks']
        self.assertEqual(
            [block['representation_id'] for block in blocks],
            ['chunk:older', 'raw:recent'],
        )
        rendered = daily_runtime._format_context_plan_representation_blocks(blocks)
        for text in ('OLDER_CHUNK', 'WAKE_14718_BODY', 'WAKE_14719_BODY', 'TURN_TWO_USER'):
            self.assertEqual(rendered.count(text), 1)
        self.assertTrue(assembly['manifest']['context_plan_capacity_raw_in_first_stdin'])

    def test_native_raw_stays_out_of_first_stdin(self):
        context_plan = _context_plan(_members(), chunk=True)
        plan = types.SimpleNamespace(
            capacity_context_plan=context_plan,
            capacity_context_chunk_bodies={},
            assembly={'layers': [], 'manifest': {}},
            capacity_context_bootstrap={},
        )
        with mock.patch.object(
            daily_runtime, '_project_context_plan_history', return_value={
                'layers': [], 'manifest': {},
                'context_plan_representation_blocks': [
                    {'kind': 'chunk', 'representation_id': 'chunk:older', 'body': 'C'},
                    {'kind': 'raw', 'representation_id': 'raw:recent', 'body': 'NATIVE'},
                ],
            },
        ):
            assembly = daily_runtime._project_capacity_context_bootstrap(plan)
        self.assertEqual(
            [block['kind'] for block in assembly['context_plan_representation_blocks']],
            ['chunk'],
        )
        self.assertFalse(assembly['manifest']['context_plan_capacity_raw_in_first_stdin'])

    def test_unrenderable_readable_representation_fails_before_handoff(self):
        with mock.patch.object(
            daily_runtime, '_project_context_plan_history', return_value={
                'context_plan_representation_blocks': [],
            },
        ):
            with self.assertRaises(daily_runtime.DailyRuntimeError) as raised:
                daily_runtime._capacity_readable_raw_blocks(
                    types.SimpleNamespace(), context_plan=None, chunk_bodies={},
                    readable_ids=('raw:recent',),
                )
        self.assertEqual(raised.exception.error_code, 'context_plan_source_unavailable')


class ThinCapacityHandoffTests(unittest.TestCase):
    def _prep(self, event_count, readable=('raw:recent',)):
        candidate = types.SimpleNamespace(
            event_count=event_count, candidate_session_id='cand-session',
            target_resident_generation=2, serialized_jsonl='',
            readable_raw_representation_ids=readable,
        )
        return types.SimpleNamespace(
            ok=True, candidate=candidate, source_context_id=7, source_context_epoch=3,
            source_resident_generation=1, warnings=['context_plan_raw_readable:1'],
        )

    def test_fully_readable_candidate_starts_a_fresh_named_session(self):
        resume = mock.Mock(side_effect=AssertionError('nothing to resume'))
        fresh = mock.Mock(return_value='staged')
        with mock.patch(
            'chat.capacity_swap_runtime.publish_capacity_swap_candidate_jsonl',
            side_effect=AssertionError('no forged JSONL for a fresh session'),
        ):
            result = run_capacity_swap_handoff(
                plan=types.SimpleNamespace(transcript_cwd='/tmp'),
                trigger_reason='soft_context', static_system='STATIC', env={},
                live_resident=types.SimpleNamespace(cwd='/tmp'),
                staged_hooks=CapacitySwapStagedHooks(
                    spawn_and_health=resume, spawn_fresh_and_health=fresh,
                ),
                prepare_result=self._prep(0),
            )
        self.assertTrue(result.ok, result.error_code)
        self.assertIsNone(result.jsonl_path)
        self.assertEqual(result.target_resident_generation, 2)
        self.assertEqual(fresh.call_args.kwargs, {'session_id': 'cand-session'})
        self.assertEqual(result.staged_resident, 'staged')

    def test_empty_candidate_without_readable_raw_still_fails_at_publish(self):
        fresh = mock.Mock(side_effect=AssertionError('legacy empty is not fresh'))
        result = run_capacity_swap_handoff(
            plan=types.SimpleNamespace(transcript_cwd='/tmp'),
            trigger_reason='soft_context', static_system='STATIC', env={},
            live_resident=types.SimpleNamespace(cwd='/tmp'),
            staged_hooks=CapacitySwapStagedHooks(
                spawn_and_health=mock.Mock(), spawn_fresh_and_health=fresh,
            ),
            prepare_result=self._prep(0, readable=()),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'empty_candidate')
        fresh.assert_not_called()

    def test_fresh_session_without_a_fresh_hook_fails_closed(self):
        result = run_capacity_swap_handoff(
            plan=types.SimpleNamespace(transcript_cwd='/tmp'),
            trigger_reason='soft_context', static_system='STATIC', env={},
            live_resident=types.SimpleNamespace(cwd='/tmp'),
            staged_hooks=CapacitySwapStagedHooks(spawn_and_health=mock.Mock()),
            prepare_result=self._prep(0),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'staged_fresh_unsupported')


if __name__ == '__main__':
    unittest.main()
