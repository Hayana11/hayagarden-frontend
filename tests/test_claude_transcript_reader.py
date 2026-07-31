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
    def test_read_plain_candidate_user_not_confirmed(self) -> None:
        path = FIXTURE / 'plain_two_rounds.jsonl'
        before = snapshot_source(path)
        graph = read_transcript(path)
        assert_source_unchanged(before)
        after = snapshot_source(path)
        self.assertEqual(before.sha256, after.sha256)
        self.assertEqual(before.size, after.size)
        self.assertEqual(file_sha256(path), before.sha256)

        self.assertEqual(len(graph.events), 4)
        self.assertEqual(len(graph.candidate_rounds), 2)
        self.assertEqual(graph.events[0].event_role, EventRole.CANDIDATE_USER)
        self.assertEqual(graph.events[1].event_role, EventRole.ASSISTANT)
        # Reader must not invent REAL_USER — only candidates
        roles = {e.event_role for e in graph.events}
        self.assertNotIn('real_user', {r.value for r in roles})

    def test_tool_result_not_candidate_user(self) -> None:
        graph = read_transcript(FIXTURE / 'tool_round.jsonl')
        roles = [e.event_role for e in graph.events]
        self.assertEqual(
            roles,
            [
                EventRole.CANDIDATE_USER,
                EventRole.ASSISTANT,
                EventRole.TOOL_RESULT_USER,
                EventRole.ASSISTANT,
            ],
        )
        self.assertEqual(len(graph.candidate_rounds), 1)
        self.assertEqual(len(graph.candidate_rounds[0].event_uuids), 4)

    def test_sidechain_marks_whole_round_impact(self) -> None:
        graph = read_transcript(FIXTURE / 'sidechain.jsonl')
        self.assertEqual(len(graph.candidate_rounds), 2)
        impacted, clean = graph.candidate_rounds
        self.assertTrue(impacted.has_sidechain_impact)
        self.assertGreaterEqual(len(impacted.sidechain_impact_uuids), 1)
        # sidechain UUIDs are NOT folded into main event_uuids
        for uid in impacted.sidechain_impact_uuids:
            self.assertNotIn(uid, impacted.event_uuids)
        self.assertFalse(clean.has_sidechain_impact)

    def test_system_not_in_round_event_uuids(self) -> None:
        graph = read_transcript(FIXTURE / 'system_in_round.jsonl')
        self.assertEqual(len(graph.system_uuids), 1)
        self.assertEqual(len(graph.candidate_rounds), 1)
        rnd = graph.candidate_rounds[0]
        self.assertNotIn(graph.system_uuids[0], rnd.event_uuids)
        self.assertEqual(len(rnd.event_uuids), 2)  # user + assistant only

    def test_pseudo_user_is_only_candidate(self) -> None:
        graph = read_transcript(FIXTURE / 'pseudo_user_wake.jsonl')
        self.assertEqual(graph.events[0].event_role, EventRole.CANDIDATE_USER)
        self.assertEqual(len(graph.candidate_rounds), 1)

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


if __name__ == '__main__':
    unittest.main()
