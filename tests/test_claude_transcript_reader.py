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

    def test_delayed_sidechain_pollutes_parent_round_not_later(self) -> None:
        """CASE S2/S3: sidechain after round 2 still attributes to round 1 via parent graph."""
        graph = read_transcript(FIXTURE / 'sidechain_delayed.jsonl')
        self.assertEqual(len(graph.candidate_rounds), 2)
        r1, r2 = graph.candidate_rounds
        self.assertTrue(r1.has_sidechain_impact)
        self.assertFalse(r2.has_sidechain_impact)
        # A + descendant B both on round 1
        self.assertEqual(len(r1.sidechain_impact_uuids), 2)
        self.assertEqual(
            list(r1.sidechain_impact_uuids),
            [
                'd5555555-5555-5555-5555-555555555555',
                'd6666666-6666-6666-6666-666666666666',
            ],
        )

    def test_unattributed_and_cycle_sidechain_warnings(self) -> None:
        """CASE S4: missing parent / cycle → warnings, no unrelated contamination."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad_side.jsonl'
            rows = [
                {
                    'type': 'user',
                    'uuid': 'e1111111-1111-1111-1111-111111111111',
                    'parentUuid': None,
                    'sessionId': 's',
                    'message': {'role': 'user', 'content': '干净轮'},
                },
                {
                    'type': 'assistant',
                    'uuid': 'e2222222-2222-2222-2222-222222222222',
                    'parentUuid': 'e1111111-1111-1111-1111-111111111111',
                    'sessionId': 's',
                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'ok'}]},
                },
                # missing parent
                {
                    'type': 'assistant',
                    'uuid': 'e3333333-3333-3333-3333-333333333333',
                    'parentUuid': 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',
                    'sessionId': 's',
                    'isSidechain': True,
                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'orphan'}]},
                },
                # cycle: A→B→A
                {
                    'type': 'assistant',
                    'uuid': 'e4444444-4444-4444-4444-444444444444',
                    'parentUuid': 'e5555555-5555-5555-5555-555555555555',
                    'sessionId': 's',
                    'isSidechain': True,
                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'cycleA'}]},
                },
                {
                    'type': 'assistant',
                    'uuid': 'e5555555-5555-5555-5555-555555555555',
                    'parentUuid': 'e4444444-4444-4444-4444-444444444444',
                    'sessionId': 's',
                    'isSidechain': True,
                    'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'cycleB'}]},
                },
            ]
            path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
            graph = read_transcript(path)
            self.assertEqual(len(graph.candidate_rounds), 1)
            self.assertFalse(graph.candidate_rounds[0].has_sidechain_impact)
            self.assertIn(
                'unattributed_sidechain:e3333333-3333-3333-3333-333333333333',
                graph.warnings,
            )
            self.assertTrue(
                any(w.startswith('sidechain_parent_cycle:') for w in graph.warnings)
            )

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

    def test_raw_bookkeeping_projected_without_type_allowlist(self) -> None:
        """A: uuid-less non-conversation observations project before formal ingest.

        Includes a type deliberately outside the terminated allowlist to prove
        the shared boundary is structural, not a metadata type registry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'boundary.jsonl'
            u = 'b1111111-1111-1111-1111-111111111111'
            a = 'b2222222-2222-2222-2222-222222222222'
            rows = [
                # Not on the terminated allowlist — must still project away.
                {'type': 'future-claude-telemetry-v9', 'payload': {'x': 1}},
                {'type': 'last-prompt', 'prompt': 'seed'},
                {
                    'type': 'assistant',
                    'requestId': 'req-usage-obs',
                    'message': {
                        'role': 'assistant',
                        'usage': {'input_tokens': 3, 'output_tokens': 1},
                    },
                },
                {
                    'type': 'user',
                    'uuid': u,
                    'parentUuid': None,
                    'sessionId': 's-meta',
                    'message': {'role': 'user', 'content': 'hello after bookkeeping'},
                },
                {
                    'type': 'assistant',
                    'uuid': a,
                    'parentUuid': u,
                    'sessionId': 's-meta',
                    'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'ok'}],
                    },
                },
            ]
            path.write_text(
                ''.join(json.dumps(r) + '\n' for r in rows),
                encoding='utf-8',
            )
            graph = read_transcript(path)
            self.assertEqual(len(graph.events), 2)
            self.assertEqual(len(graph.candidate_rounds), 1)
            self.assertEqual(list(graph.candidate_rounds[0].event_uuids), [u, a])
            ignored = [
                w for w in graph.warnings if w.startswith('ignored_raw_observation:')
            ]
            self.assertGreaterEqual(len(ignored), 3)
            self.assertTrue(
                any('future-claude-telemetry-v9' in w for w in ignored),
                ignored,
            )

    def test_conversation_shaped_missing_uuid_rejected(self) -> None:
        """B: conversation-shaped uuid-less rows remain fail-closed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad_user.jsonl'
            path.write_text(
                json.dumps({
                    'type': 'user',
                    'parentUuid': None,
                    'sessionId': 's',
                    'message': {'role': 'user', 'content': 'no uuid'},
                }) + '\n',
                encoding='utf-8',
            )
            with self.assertRaises(TranscriptReaderError) as ctx:
                read_transcript(path)
            self.assertEqual(ctx.exception.code, ReaderErrorCode.MISSING_UUID)

            path2 = Path(tmp) / 'bad_assistant.jsonl'
            path2.write_text(
                json.dumps({
                    'type': 'assistant',
                    'sessionId': 's',
                    'message': {
                        'role': 'assistant',
                        'content': [{'type': 'text', 'text': 'no uuid'}],
                    },
                }) + '\n',
                encoding='utf-8',
            )
            with self.assertRaises(TranscriptReaderError) as ctx2:
                read_transcript(path2)
            self.assertEqual(ctx2.exception.code, ReaderErrorCode.MISSING_UUID)


if __name__ == '__main__':
    unittest.main()
