"""Focused tests for Claude transcript reader (v0.2)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chat.claude_transcript_model import EventRole
from chat.claude_transcript_reader import (
    ReaderErrorCode,
    TranscriptReaderError,
    assert_source_unchanged,
    file_sha256,
    read_transcript,
    snapshot_source,
)

FIXTURE = ROOT / 'tests' / 'fixtures' / 'claude_transcript'


class TranscriptReaderTests(unittest.TestCase):
    def test_read_plain_two_rounds_and_source_immutable(self) -> None:
        path = FIXTURE / 'plain_two_rounds.jsonl'
        before = snapshot_source(path)
        graph = read_transcript(path)
        assert_source_unchanged(before)
        after = snapshot_source(path)
        self.assertEqual(before.sha256, after.sha256)
        self.assertEqual(before.size, after.size)
        with path.open('rb') as handle:
            raw = handle.read()
        self.assertEqual(file_sha256(path), before.sha256)
        self.assertEqual(len(raw), before.size)

        self.assertEqual(len(graph.events), 4)
        self.assertEqual(len(graph.real_rounds), 2)
        self.assertEqual(graph.events[0].event_role, EventRole.REAL_USER)
        self.assertEqual(graph.events[1].event_role, EventRole.ASSISTANT)
        self.assertIn(graph.events[0].event_uuid, graph.by_uuid)
        self.assertEqual(
            graph.children_by_parent[None],
            [graph.events[0].event_uuid],
        )

    def test_tool_result_not_real_user(self) -> None:
        graph = read_transcript(FIXTURE / 'tool_round.jsonl')
        roles = [e.event_role for e in graph.events]
        self.assertEqual(
            roles,
            [
                EventRole.REAL_USER,
                EventRole.ASSISTANT,
                EventRole.TOOL_RESULT_USER,
                EventRole.ASSISTANT,
            ],
        )
        self.assertEqual(len(graph.real_rounds), 1)
        self.assertEqual(len(graph.real_rounds[0].event_uuids), 4)
        self.assertIn('toolu_spike_success_001', graph.tool_uses)
        self.assertIn('toolu_spike_success_001', graph.tool_results)

    def test_sidechain_and_summary_identified(self) -> None:
        side = read_transcript(FIXTURE / 'sidechain.jsonl')
        self.assertEqual(len(side.sidechain_uuids), 2)
        self.assertEqual(len(side.real_rounds), 1)

        summ = read_transcript(FIXTURE / 'summary_and_meta.jsonl')
        self.assertEqual(len(summ.summary_uuids), 1)
        meta_roles = [e.event_role for e in summ.events if e.event_role == EventRole.META]
        self.assertGreaterEqual(len(meta_roles), 2)

    def test_invalid_json_enum_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.jsonl'
            path.write_text('{not-json\n', encoding='utf-8')
            with self.assertRaises(TranscriptReaderError) as ctx:
                read_transcript(path)
            self.assertEqual(ctx.exception.code, ReaderErrorCode.INVALID_JSON)

    def test_duplicate_uuid_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dup.jsonl'
            uid = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
            rows = [
                {
                    'type': 'user',
                    'uuid': uid,
                    'parentUuid': None,
                    'sessionId': 's',
                    'message': {'role': 'user', 'content': 'a'},
                },
                {
                    'type': 'assistant',
                    'uuid': uid,
                    'parentUuid': uid,
                    'sessionId': 's',
                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'b'}]},
                },
            ]
            path.write_text(
                ''.join(json.dumps(r) + '\n' for r in rows),
                encoding='utf-8',
            )
            with self.assertRaises(TranscriptReaderError) as ctx:
                read_transcript(path)
            self.assertEqual(ctx.exception.code, ReaderErrorCode.DUPLICATE_UUID)

    def test_unknown_event_classified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unknown.jsonl'
            rows = [
                {
                    'type': 'weird-future-type',
                    'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                    'sessionId': 's',
                },
                {
                    'type': 'user',
                    'uuid': 'cccccccc-cccc-cccc-cccc-cccccccccccc',
                    'parentUuid': None,
                    'sessionId': 's',
                    'message': {'role': 'user', 'content': 'hi'},
                },
            ]
            path.write_text(
                ''.join(json.dumps(r) + '\n' for r in rows),
                encoding='utf-8',
            )
            graph = read_transcript(path)
            self.assertEqual(graph.events[0].event_role, EventRole.UNKNOWN)
            self.assertIn(graph.events[0].event_uuid, graph.unknown_uuids)


if __name__ == '__main__':
    unittest.main()
