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
    SummaryPolicy,
    ThinkingPolicy,
)
from dataclasses import replace
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
    for rnd in graph.candidate_rounds:
        evt = graph.by_uuid[rnd.candidate_user_event_uuid]
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
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(hashes[1], hashes[2])
        print('DETERMINISM_SHA=', hashes[0])

    def test_unmapped_candidate_not_treated_as_real(self) -> None:
        graph = read_transcript(FIXTURE / 'pseudo_user_wake.jsonl')
        # Empty mapping: Wake/pseudo user stays unconfirmed
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid={},
            thinking_policy=ThinkingPolicy.DROP,
        )
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, req)
        self.assertEqual(ctx.exception.code, TransformErrorCode.EMPTY_SELECTION)

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

    def test_never_copy_legacy_user_payload(self) -> None:
        graph = read_transcript(FIXTURE / 'legacy_injection.jsonl')
        uid = graph.candidate_rounds[0].candidate_user_event_uuid
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
        use_id = result.events[1]['message']['content'][0]['id']
        result_id = result.events[2]['message']['content'][0]['tool_use_id']
        self.assertEqual(use_id, result_id)
        self.assertNotEqual(use_id, 'toolu_spike_success_001')

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

    def test_thinking_drop_and_keep_with_signature(self) -> None:
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
        block = kept.events[1]['message']['content'][0]
        self.assertEqual(block['type'], 'thinking')
        self.assertTrue(str(block.get('signature') or '').strip())

    def test_keep_thinking_missing_signature_fails(self) -> None:
        graph = read_transcript(FIXTURE / 'signed_thinking.jsonl')
        # mutate source copy via graph event raw is Mapping - rebuild local graph path:
        # use transform after stripping signature from a deepcopy via custom graph emit
        # Easiest: take KEEP path and manually clear signature in source by writing temp
        import tempfile
        raw = (FIXTURE / 'signed_thinking.jsonl').read_text(encoding='utf-8')
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        for b in rows[1]['message']['content']:
            if b.get('type') == 'thinking':
                b.pop('signature', None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'nosig.jsonl'
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
            g2 = read_transcript(path)
            req = TransformRequest(
                new_session_id='newnewne-newn-newn-newn-newnewnewnew',
                cwd='/tmp/out',
                keep_rounds=1,
                user_canonical_by_event_uuid=_mapping_for(g2),
                thinking_policy=ThinkingPolicy.KEEP,
            )
            with self.assertRaises(TransformError) as ctx:
                transform_transcript(g2, req)
            self.assertEqual(ctx.exception.code, TransformErrorCode.THINKING_INVALID)

    def test_sidechain_policy_keep_not_executable(self) -> None:
        """CASE S1: v0.2 has no executable Sidechain KEEP; illegal policy fails closed."""
        self.assertEqual(list(SidechainPolicy), [SidechainPolicy.EXCLUDE])
        self.assertFalse(hasattr(SidechainPolicy, 'KEEP'))
        graph = read_transcript(FIXTURE / 'plain_two_rounds.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
            sidechain_policy=SidechainPolicy.EXCLUDE,
        )
        # Forge a non-EXCLUDE policy without relying on a removed enum member
        bad = replace(req, sidechain_policy='keep')  # type: ignore[arg-type]
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, bad)
        self.assertEqual(ctx.exception.code, TransformErrorCode.INVALID_POLICY)
        self.assertIn('sidechain', str(ctx.exception))

    def test_summary_policy_keep_not_executable(self) -> None:
        """v0.2 has no executable Summary KEEP; \"keep\" is not silently accepted."""
        self.assertEqual(list(SummaryPolicy), [SummaryPolicy.DROP])
        self.assertFalse(hasattr(SummaryPolicy, 'KEEP'))
        graph = read_transcript(FIXTURE / 'summary_and_meta.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
            summary_policy=SummaryPolicy.DROP,
        )
        bad = replace(req, summary_policy='keep')  # type: ignore[arg-type]
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, bad)
        self.assertEqual(ctx.exception.code, TransformErrorCode.INVALID_POLICY)
        self.assertIn('summary', str(ctx.exception))

    def test_sidechain_excludes_entire_affected_round(self) -> None:
        graph = read_transcript(FIXTURE / 'sidechain.jsonl')
        mapping = _mapping_for(graph)
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=2,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
            sidechain_policy=SidechainPolicy.EXCLUDE,
        )
        result = transform_transcript(graph, req)
        # Whole impacted round dropped; only clean second round remains
        self.assertEqual(result.selected_round_count, 1)
        self.assertEqual(len(result.dropped_sidechain_round_user_uuids), 1)
        texts = [
            e['message']['content']
            for e in result.events
            if e['type'] == 'user' and isinstance(e['message']['content'], str)
        ]
        self.assertEqual(texts, ['干净主链第二轮'])
        self.assertNotIn('主链消息-受sidechain影响', texts)
        self.assertTrue(all(e.get('isSidechain') is not True for e in result.events))

    def test_delayed_sidechain_excludes_only_parent_round(self) -> None:
        """CASE S2/S3: delayed sidechain + descendant → drop round 1, keep round 2."""
        graph = read_transcript(FIXTURE / 'sidechain_delayed.jsonl')
        mapping = _mapping_for(graph)
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=2,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        self.assertEqual(result.selected_round_count, 1)
        self.assertEqual(
            result.dropped_sidechain_round_user_uuids,
            ['d1111111-1111-1111-1111-111111111111'],
        )
        texts = [
            e['message']['content']
            for e in result.events
            if e['type'] == 'user' and isinstance(e['message']['content'], str)
        ]
        self.assertEqual(texts, ['第二轮干净消息'])
        self.assertNotIn('第一轮真实消息', texts)

    def test_sidechain_only_rounds_yield_empty_selection(self) -> None:
        """If every mapped round is sidechain-impacted, do not keep pruned main user."""
        graph = read_transcript(FIXTURE / 'sidechain.jsonl')
        impacted = graph.candidate_rounds[0]
        mapping = {
            impacted.candidate_user_event_uuid: '主链消息-受sidechain影响',
        }
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=mapping,
            thinking_policy=ThinkingPolicy.DROP,
            sidechain_policy=SidechainPolicy.EXCLUDE,
        )
        with self.assertRaises(TransformError) as ctx:
            transform_transcript(graph, req)
        self.assertEqual(ctx.exception.code, TransformErrorCode.EMPTY_SELECTION)

    def test_system_events_never_migrated(self) -> None:
        graph = read_transcript(FIXTURE / 'system_in_round.jsonl')
        req = TransformRequest(
            new_session_id='newnewne-newn-newn-newn-newnewnewnew',
            cwd='/tmp/out',
            keep_rounds=1,
            user_canonical_by_event_uuid=_mapping_for(graph),
            thinking_policy=ThinkingPolicy.DROP,
        )
        result = transform_transcript(graph, req)
        types = [e['type'] for e in result.events]
        self.assertNotIn('system', types)
        self.assertEqual(result.dropped_system_uuids, graph.system_uuids)
        self.assertEqual([e['type'] for e in result.events], ['user', 'assistant'])

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

    def test_zero_rounds_native_cold_empty(self) -> None:
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
        self.assertEqual(result.events, [])
        self.assertIn('native_cold_empty_transcript', result.notes)
        # Must not fabricate ordinary user boundary messages
        payload = serialize_events(result.events)
        self.assertNotIn('context-window-boundary', payload)
        self.assertNotIn('context-window-ready', payload)

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


if __name__ == '__main__':
    unittest.main()
