"""Focused tests for Claude transcript transform (v0.2)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.claude_transcript_model import (
    SidechainPolicy,
    ThinkingPolicy,
)
from chat.claude_transcript_reader import read_transcript
from chat.claude_transcript_transform import (
    TransformError,
    TransformErrorCode,
    TransformRequest,
    serialize_events,
    transform_deterministic_hash,
    transform_transcript,
)
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript'


def _mapping_for(graph, overrides=None):
    mapping = {}
    for rnd in graph.real_rounds:
        evt = graph.by_uuid[rnd.real_user_event_uuid]
        content = evt.raw.get('message', {}).get('content')
        if isinstance(content, str):
            mapping[evt.event_uuid] = content
    if overrides:
        mapping.update(overrides)
    return mapping


class TranscriptTransformTests(unittest.TestCase):
    def test_determinism_three_runs(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=2,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        hashes = transform_deterministic_hash(graph, req, runs=3)
        self.assertEqual(len(hashes), 3)
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[1], hashes[2])
        print('DETERMINISM_SHA=', hashes[0])

    def test_tail_rounds_and_love_text_kept(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        mapping = _mapping_for(graph)
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(result.selected_round_count, 1)
        self.assertEqual(result.events[0]['message']['content'], '今天想你了')
        # apology round excluded by tail window, not by semantics
        texts = [
            e['message']['content']
            for e in result.events
            if e['type'] == 'user' and isinstance(e['message']['content'], str)
        ]
        self.assertNotIn('对不起，我刚才语气不好', texts)

    def test_never_copy_legacy_user_payload(self) -> None:
        graph = read_transcript(FIXTURE / 'legacy_injection.jsonl')
        uid = graph.real_rounds[0].real_user_event_uuid
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid={uid: '今天天气怎么样'},
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(result.events[0]['message']['content'], '今天天气怎么样')
        self.assertNotIn('【昨日延续对话】', result.events[0]['message']['content'])

    def test_mapping_missing_fails(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid={},
            thinking_policy=ThinkingPolicy.DROP,
        )
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, req)
        self.assertEqual(ctx.exception.code, TransformErrorCode.MAPPING_MISSING)

    def test_tool_pair_and_id_remap(self) -> None:
        graph = read_transcript(FIXTURE / 'tool_round.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(len(result.events), 4)
        use_id = result.events[1]['message']['content'][0]['id']
        result_id = result.events[2]['message']['content'][0]['tool_use_id']
        self.assertEqual(use_id, result_id)
        self.assertNotEqual(use_id, 'toolu_spike_success_001')
        validation = validate_transcript_events(
            result.events,
            ValidatorOptions(
                session_id=req.new_session_id,
                thinking_policy=ThinkingPolicy.DROP,
                expected_round_count=1,
                old_uuids=set(graph.by_uuid),
            ),
        )
        self.assertTrue(validation.ok, validation.errors)

    def test_orphan_tool_use_rejected(self) -> None:
        graph = read_transcript(FIXTURE / 'orphan_tool_use.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, req)
        self.assertEqual(ctx.exception.code, TransformErrorCode.TOOL_ORPHAN)

    def test_thinking_drop_and_keep(self) -> None:
        graph = read_transcript(FIXTURE / 'signed_thinking.jsonl')
        mapping = _mapping_for(graph)
        drop_req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
        )
        dropped = transform_transcript(graph, drop_req)
        types = [b['type'] for b in dropped.events[1]['message']['content']]
        self.assertNotIn('thinking', types)

        keep_req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.KEEP,
        )
        kept = transform_transcript(graph, keep_req)
        types = [b['type'] for b in kept.events[1]['message']['content']]
        self.assertIn('thinking', types)

    def test_empty_thinking_keep_fails_validation(self) -> None:
        # construct keep output then empty the thinking block
        graph = read_transcript(FIXTURE / 'signed_thinking.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.KEEP,
        )
        result = transform_transcript(graph, req)
        result.events[1]['message']['content'][0]['thinking'] = '   '
        validation = validate_transcript_events(
            result.events,
            ValidatorOptions(
                session_id=req.new_session_id,
                thinking_policy=ThinkingPolicy.KEEP,
            ),
        )
        self.assertFalse(validation.ok)
        self.assertTrue(any('empty_thinking' in e for e in validation.errors))

    def test_sidechain_excluded(self) -> None:
        graph = read_transcript(FIXTURE / 'sidechain.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
            sidechain_policy=SidechainPolicy.EXCLUDE,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.events[0]['message']['content'], '主链消息')
        self.assertTrue(all(e.get('isSidechain') is not True for e in result.events))

    def test_summary_and_meta_dropped(self) -> None:
        graph = read_transcript(FIXTURE / 'summary_and_meta.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        types = [e['type'] for e in result.events]
        self.assertNotIn('summary', types)
        self.assertNotIn('queue-operation', types)
        self.assertNotIn('last-prompt', types)
        self.assertIn('普通争吵后和好了', result.events[0]['message']['content'])

    def test_zero_rounds_boundary_primer(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=0,
            user_canonical_by_event_uuid={},
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(result.selected_round_count, 0)
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.events[0]['message']['content'], '[context-window-boundary]')

    def test_graph_not_mutated(self) -> None:
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        snapshot = json.loads(json.dumps([e.raw_copy() for e in graph.events]))
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=2,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        transform_transcript(graph, req)
        after = [e.raw_copy() for e in graph.events]
        self.assertEqual(after, snapshot)
        # serialize helper stable
        self.assertTrue(serialize_events([{'a': 1}]).endswith('\n'))


if __name__ == '__main__':
    unittest.main()
