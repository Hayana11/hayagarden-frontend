"""Focused tests for Claude transcript validator (v0.2)."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.claude_transcript_model import ThinkingPolicy
from chat.claude_transcript_reader import read_transcript
from chat.claude_transcript_transform import TransformRequest, transform_transcript
from chat.claude_transcript_validator import (
    ValidatorErrorCode,
    ValidatorOptions,
    validate_transcript_events,
)

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript'


def _mapping_for(graph):
    mapping = {}
    for rnd in graph.candidate_rounds:
        evt = graph.by_uuid[rnd.candidate_user_event_uuid]
        content = evt.raw.get('message', {}).get('content')
        if isinstance(content, str):
            mapping[evt.event_uuid] = content
    return mapping


class TranscriptValidatorTests(unittest.TestCase):
    def test_valid_transform_passes_structure_only(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=2,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        validation = validate_transcript_events(
            result.events,
            ValidatorOptions(
                session_id=req.new_session_id,
                thinking_policy=ThinkingPolicy.DROP,
                expected_round_count=2,
                max_round_count=2,
                old_uuids=set(graph.by_uuid),
            ),
        )
        self.assertTrue(validation.ok, validation.errors)
        self.assertEqual(validation.proof_kind, 'local_structure_contract')

    def test_keep_thinking_requires_signature(self) -> None:
        sid = 'newnewne-newn-newn-newn-newnewnewnew'
        events = [
            {
                'type': 'user',
                'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'parentUuid': None,
                'sessionId': sid,
                'message': {'role': 'user', 'content': 'a'},
            },
            {
                'type': 'assistant',
                'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'parentUuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'sessionId': sid,
                'message': {
                    'role': 'assistant',
                    'content': [
                        {'type': 'thinking', 'thinking': '思考中', 'signature': ''},
                        {'type': 'text', 'text': 'y'},
                    ],
                },
            },
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(session_id=sid, thinking_policy=ThinkingPolicy.KEEP),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any('missing_signature' in e for e in validation.errors))

    def test_keep_thinking_with_signature_ok(self) -> None:
        graph = read_transcript(FIXTURE / 'signed_thinking.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.KEEP,
        )
        result = transform_transcript(graph, req)
        validation = validate_transcript_events(
            result.events,
            ValidatorOptions(
                session_id=req.new_session_id,
                thinking_policy=ThinkingPolicy.KEEP,
                expected_round_count=1,
            ),
        )
        self.assertTrue(validation.ok, validation.errors)

    def test_duplicate_uuid_fails(self) -> None:
        sid = 'newnewne-newn-newn-newn-newnewnewnew'
        uid = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        events = [
            {
                'type': 'user',
                'uuid': uid,
                'parentUuid': None,
                'sessionId': sid,
                'message': {'role': 'user', 'content': 'a'},
            },
            {
                'type': 'assistant',
                'uuid': uid,
                'parentUuid': uid,
                'sessionId': sid,
                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'b'}]},
            },
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(session_id=sid),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any(ValidatorErrorCode.UUID.value in e for e in validation.errors))

    def test_broken_parent_fails(self) -> None:
        sid = 'newnewne-newn-newn-newn-newnewnewnew'
        events = [
            {
                'type': 'user',
                'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'parentUuid': None,
                'sessionId': sid,
                'message': {'role': 'user', 'content': 'a'},
            },
            {
                'type': 'assistant',
                'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'parentUuid': 'cccccccc-cccc-cccc-cccc-cccccccccccc',
                'sessionId': sid,
                'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'b'}]},
            },
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(session_id=sid),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any(ValidatorErrorCode.PARENT.value in e for e in validation.errors))

    def test_sidechain_and_summary_forbidden(self) -> None:
        sid = 'newnewne-newn-newn-newn-newnewnewnew'
        events = [
            {
                'type': 'user',
                'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'parentUuid': None,
                'sessionId': sid,
                'isSidechain': True,
                'message': {'role': 'user', 'content': 'a'},
            },
            {
                'type': 'summary',
                'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'sessionId': sid,
                'summary': 'x',
            },
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(session_id=sid),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any(ValidatorErrorCode.SIDECHAIN.value in e for e in validation.errors))
        self.assertTrue(any(ValidatorErrorCode.SUMMARY.value in e for e in validation.errors))

    def test_old_uuid_residual_in_structural_field_rejected(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        poisoned = copy.deepcopy(result.events)
        old_uid = next(iter(graph.by_uuid))
        poisoned[1]['parentUuid'] = old_uid
        validation = validate_transcript_events(
            poisoned,
            ValidatorOptions(
                session_id=req.new_session_id,
                old_uuids=set(graph.by_uuid),
            ),
        )
        self.assertFalse(validation.ok)

    def test_uuid_shaped_chat_text_not_false_positive(self) -> None:
        graph = read_transcript(FIXTURE / 'uuid_in_chat_text.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        mention = '77777777-7777-7777-7777-777777777777'
        validation = validate_transcript_events(
            result.events,
            ValidatorOptions(
                session_id=req.new_session_id,
                old_uuids=set(graph.by_uuid) | {mention},
                expected_round_count=1,
            ),
        )
        self.assertTrue(validation.ok, validation.errors)

    def test_thinking_present_under_drop_fails(self) -> None:
        sid = 'newnewne-newn-newn-newn-newnewnewnew'
        events = [
            {
                'type': 'user',
                'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'parentUuid': None,
                'sessionId': sid,
                'message': {'role': 'user', 'content': 'a'},
            },
            {
                'type': 'assistant',
                'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                'parentUuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                'sessionId': sid,
                'message': {
                    'role': 'assistant',
                    'content': [
                        {'type': 'thinking', 'thinking': 'x', 'signature': 's'},
                        {'type': 'text', 'text': 'y'},
                    ],
                },
            },
        ]
        validation = validate_transcript_events(
            events,
            ValidatorOptions(session_id=sid, thinking_policy=ThinkingPolicy.DROP),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any('thinking_present_under_drop' in e for e in validation.errors))

    def test_native_cold_empty_is_not_validated_as_resume(self) -> None:
        validation = validate_transcript_events(
            [],
            ValidatorOptions(session_id='newnewne-newn-newn-newn-newnewnewnew'),
        )
        self.assertFalse(validation.ok)
        self.assertEqual(validation.proof_kind, 'local_structure_contract')


if __name__ == '__main__':
    unittest.main()
