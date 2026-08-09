"""Capacity Swap Core tests (Step 8-A minimal matrix)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.capacity_swap import (
    CAPACITY_SWAP_REASONS,
    AnchorStatus,
    CapacitySwapStatus,
    prepare_capacity_swap_candidate,
)
from chat.claude_transcript_model import ThinkingPolicy
from chat.claude_transcript_reader import read_transcript
from chat.claude_transcript_transform import (
    SelectionPolicy,
    TransformRequest,
    transform_transcript,
)
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript'
CWD = '/tmp/claude-transcript-core-synth'
CANDIDATE_SID = 'cccccccc-cccc-cccc-cccc-cccccccccccc'


def _mapping_for(graph, overrides=None):
    mapping = {}
    for rnd in graph.candidate_rounds:
        evt = graph.by_uuid[rnd.candidate_user_event_uuid]
        content = evt.raw.get('message', {}).get('content')
        if isinstance(content, str):
            mapping[evt.event_uuid] = content
    if overrides:
        mapping.update(overrides)
    return mapping


def _formal_messages_plain_two_rounds():
    return [
        {
            'id': 1,
            'author': 'hayana',
            'content': '对不起，我刚才语气不好',
            'image_url': '',
        },
        {
            'id': 2,
            'author': 'assistant',
            'content': '没关系，我在。',
            'image_url': '',
        },
        {
            'id': 3,
            'author': 'hayana',
            'content': '今天想你了',
            'image_url': '',
        },
        {
            'id': 4,
            'author': 'assistant',
            'content': '我也想你。',
            'image_url': '',
        },
    ]


def _mapping_ids_plain_two_rounds(graph):
    event_to_mid = {
        '11111111-1111-1111-1111-111111111111': 1,
        '22222222-2222-2222-2222-222222222222': 2,
        '33333333-3333-3333-3333-333333333333': 3,
        '44444444-4444-4444-4444-444444444444': 4,
    }
    mid_to_event = {v: k for k, v in event_to_mid.items()}
    canonical = _mapping_for(graph)
    return canonical, mid_to_event, event_to_mid


def _formal_messages_tool_round():
    return [
        {'id': 10, 'author': 'hayana', 'content': '读取测试文件', 'image_url': ''},
        {'id': 11, 'author': 'assistant', 'content': '文件内容是 hello', 'image_url': ''},
    ]


def _mapping_ids_tool_round(graph):
    event_to_mid = {
        'aaaaaaa1-aaaa-aaaa-aaaa-aaaaaaaaaaa1': 10,
        'aaaaaaa2-aaaa-aaaa-aaaa-aaaaaaaaaaa2': 11,
        'aaaaaaa3-aaaa-aaaa-aaaa-aaaaaaaaaaa3': 12,
        'aaaaaaa4-aaaa-aaaa-aaaa-aaaaaaaaaaa4': 11,
    }
    mid_to_event = {10: 'aaaaaaa1-aaaa-aaaa-aaaa-aaaaaaaaaaa1'}
    canonical = _mapping_for(graph)
    return canonical, mid_to_event, event_to_mid


class CapacitySwapCoreTests(unittest.TestCase):
    def test_non_capacity_reason_rejected(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        canonical, mid_to_event, event_to_mid = _mapping_ids_plain_two_rounds(graph)
        result = prepare_capacity_swap_candidate(
            graph=graph,
            trigger_reason='process_dead',
            source_context_id=1,
            source_context_epoch=1,
            source_resident_generation=2,
            source_claude_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            source_transcript_path=str(FIXTURE / 'plain_two_rounds.jsonl'),
            source_scan_offset=1000,
            source_sha256='abc',
            formal_messages=_formal_messages_plain_two_rounds(),
            user_canonical_by_event_uuid=canonical,
            mapping_event_uuid_by_message_id=mid_to_event,
            mapping_message_id_by_event_uuid=event_to_mid,
            retained_transcript_token_budget=5000,
            anchor_token_budget=5000,
            cwd=CWD,
            candidate_session_id=CANDIDATE_SID,
        )
        self.assertEqual(result.status, CapacitySwapStatus.TRIGGER_NOT_CAPACITY)
        self.assertIsNone(result.candidate)

    def test_case_a_normal_budget_anchor_and_partial_tail(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        canonical, mid_to_event, event_to_mid = _mapping_ids_plain_two_rounds(graph)
        result = prepare_capacity_swap_candidate(
            graph=graph,
            trigger_reason='soft_context',
            source_context_id=42,
            source_context_epoch=3,
            source_resident_generation=5,
            source_claude_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            source_transcript_path=str(FIXTURE / 'plain_two_rounds.jsonl'),
            source_scan_offset=2048,
            source_sha256='deadbeef',
            formal_messages=_formal_messages_plain_two_rounds(),
            user_canonical_by_event_uuid=canonical,
            mapping_event_uuid_by_message_id=mid_to_event,
            mapping_message_id_by_event_uuid=event_to_mid,
            retained_transcript_token_budget=200,
            anchor_token_budget=5000,
            cwd=CWD,
            candidate_session_id=CANDIDATE_SID,
        )
        self.assertEqual(result.status, CapacitySwapStatus.READY)
        cand = result.candidate
        self.assertIsNotNone(cand)
        assert cand is not None
        self.assertEqual(cand.trigger_reason, 'soft_context')
        self.assertEqual(cand.source_context_id, 42)
        self.assertEqual(cand.source_context_epoch, 3)
        self.assertEqual(cand.target_resident_generation, 6)
        self.assertEqual(cand.anchor_status, AnchorStatus.ANCHOR_RETAINED)
        self.assertEqual(cand.anchor_message_id, 1)
        self.assertEqual(cand.selected_round_count, 1)
        self.assertIn(3, cand.selected_message_ids)
        self.assertIn('今天想你了', cand.serialized_jsonl)
        self.assertIn('对不起，我刚才语气不好', cand.serialized_jsonl)
        import json
        events = [
            json.loads(line) for line in cand.serialized_jsonl.splitlines() if line.strip()
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(
                session_id=CANDIDATE_SID,
                thinking_policy=ThinkingPolicy.DROP,
                forbid_sidechain=True,
                forbid_summary=True,
                old_uuids=set(graph.by_uuid.keys()),
            ),
        )
        self.assertTrue(validation.ok)
        self.assertGreater(cand.estimated_tokens, 0)

    def test_case_b_tool_round_not_cut_mid_chain(self) -> None:
        graph = read_transcript(FIXTURE / 'tool_round.jsonl')
        canonical, _, event_to_mid = _mapping_ids_tool_round(graph)
        # Anchor unavailable (no mapping) so the single tool round stays in tail.
        result = prepare_capacity_swap_candidate(
            graph=graph,
            trigger_reason='hard_context',
            source_context_id=1,
            source_context_epoch=1,
            source_resident_generation=1,
            source_claude_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            source_transcript_path=str(FIXTURE / 'tool_round.jsonl'),
            source_scan_offset=500,
            source_sha256='toolhash',
            formal_messages=[
                {'id': 99, 'author': 'hayana', 'content': '旧开场', 'image_url': ''},
            ],
            user_canonical_by_event_uuid=canonical,
            mapping_event_uuid_by_message_id={},
            mapping_message_id_by_event_uuid=event_to_mid,
            retained_transcript_token_budget=5000,
            anchor_token_budget=5000,
            cwd=CWD,
            candidate_session_id=CANDIDATE_SID,
        )
        self.assertEqual(result.status, CapacitySwapStatus.READY)
        cand = result.candidate
        assert cand is not None
        self.assertEqual(cand.anchor_status, AnchorStatus.ANCHOR_UNAVAILABLE)
        self.assertEqual(cand.selected_round_count, 1)
        import json
        types: list[str] = []
        for line in cand.serialized_jsonl.splitlines():
            if not line.strip():
                continue
            evt = json.loads(line)
            message = evt.get('message') or {}
            for block in message.get('content') or []:
                if isinstance(block, dict):
                    types.append(str(block.get('type')))
        self.assertIn('tool_use', types)
        self.assertIn('tool_result', types)

    def test_case_c_tail_budget_exceeded_no_truncation(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        canonical, mid_to_event, event_to_mid = _mapping_ids_plain_two_rounds(graph)
        result = prepare_capacity_swap_candidate(
            graph=graph,
            trigger_reason='turn_limit',
            source_context_id=9,
            source_context_epoch=2,
            source_resident_generation=3,
            source_claude_session_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            source_transcript_path=str(FIXTURE / 'plain_two_rounds.jsonl'),
            source_scan_offset=99,
            source_sha256='casec',
            formal_messages=_formal_messages_plain_two_rounds(),
            user_canonical_by_event_uuid=canonical,
            mapping_event_uuid_by_message_id=mid_to_event,
            mapping_message_id_by_event_uuid=event_to_mid,
            retained_transcript_token_budget=100,
            anchor_token_budget=5000,
            cwd=CWD,
            candidate_session_id=CANDIDATE_SID,
        )
        self.assertEqual(result.status, CapacitySwapStatus.TAIL_BUDGET_EXCEEDED)
        cand = result.candidate
        assert cand is not None
        self.assertEqual(cand.selected_round_count, 0)
        self.assertEqual(cand.anchor_status, AnchorStatus.ANCHOR_RETAINED)
        self.assertNotIn('今天想你了', cand.serialized_jsonl)
        self.assertIn('对不起，我刚才语气不好', cand.serialized_jsonl)
        import json
        for line in cand.serialized_jsonl.splitlines():
            if line.strip():
                json.loads(line)

    def test_token_budget_tail_selection_in_transform(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        mapping = _mapping_for(graph)
        req = TransformRequest(
            new_session_id=CANDIDATE_SID,
            cwd=CWD,
            keep_rounds=0,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
            selection_policy=SelectionPolicy.TOKEN_BUDGET_TAIL,
            tail_token_budget=200,
            exclude_round_candidate_uuids=frozenset({'11111111-1111-1111-1111-111111111111'}),
        )
        result = transform_transcript(graph, req)
        self.assertEqual(result.selected_round_count, 1)
        self.assertIn('今天想你了', result.events[0]['message']['content'])

    def test_capacity_reasons_frozen_set(self) -> None:
        self.assertEqual(
            CAPACITY_SWAP_REASONS,
            frozenset({'soft_context', 'hard_context', 'turn_limit'}),
        )


if __name__ == '__main__':
    unittest.main()
