"""CONTINUITY-R1 — canonical source and exact coverage contracts."""
from __future__ import annotations

import copy
import json
import unittest

from continuity.contracts import SourceMember
from continuity.coverage import validate_exact_coverage
from continuity.sources import (
    build_source_snapshot,
    derive_autonomous_events,
    derive_completed_turns,
    row_revision,
)


def row(mid: int, author: str, content: str, **extra):
    base = {
        'id': mid,
        'author': author,
        'content': content,
        'thinking': '',
        'source_kind': 'chat',
        'tool_calls': '',
        'branches': '',
        'branch_idx': 0,
        'cache_info': '',
        'attachments': '[]',
        'image_url': '',
        'file_url': '',
        'file_name': '',
        'created_at': f'2026-09-08 10:{mid:02d}:00',
    }
    base.update(extra)
    return base


class CanonicalTurnTests(unittest.TestCase):
    def test_tool_outcomes_stay_inside_one_turn_and_thinking_is_not_source(self):
        tools = json.dumps([
            {'name': 'memory.search', 'args': {'q': 'x'}, 'result': 'A', 'success': True},
            {'name': 'todo.write', 'args': {'x': 1}, 'result': 'B', 'success': True},
        ])
        rows = [
            row(1, 'hayana', '开始'),
            row(2, 'assistant', '完成', thinking='private-a', tool_calls=tools),
        ]
        turns = derive_completed_turns(rows)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].turn_id, 'turn:1:2')
        self.assertEqual(len(turns[0].assistant_committed_output_refs), 1)
        self.assertEqual(len(turns[0].tool_outcome_refs), 2)

        changed = copy.deepcopy(rows)
        changed[1]['thinking'] = 'private-b'
        self.assertEqual(
            derive_completed_turns(changed)[0].source_revision,
            turns[0].source_revision,
        )

    def test_missing_multiple_or_explicit_incomplete_assistant_fail_closed(self):
        rows = [
            row(1, 'hayana', 'no answer'),
            row(2, 'hayana', 'partial'),
            row(3, 'assistant', 'half', cache_info=json.dumps({
                'turn_incomplete': True,
                'partial_rescue': True,
                'stream_interrupted': True,
            })),
            row(4, 'hayana', 'ambiguous'),
            row(5, 'assistant', 'a'),
            row(6, 'assistant', 'b'),
            row(7, 'hayana', 'good'),
            row(8, 'assistant', 'ok'),
        ]
        turns = derive_completed_turns(rows)
        self.assertEqual([turn.turn_id for turn in turns], ['turn:7:8'])

    def test_regen_active_branch_changes_revision_but_branch_thinking_does_not(self):
        branches = [
            {'content': 'old', 'thinking': 'secret-old', 'tool_calls': ''},
            {'content': 'new', 'thinking': 'secret-new', 'tool_calls': ''},
        ]
        base = [
            row(1, 'hayana', 'redo'),
            row(2, 'assistant', 'old', branches=json.dumps(branches), branch_idx=0),
        ]
        old = derive_completed_turns(base)[0]

        switched = copy.deepcopy(base)
        switched[1]['content'] = 'new'
        switched[1]['branch_idx'] = 1
        new = derive_completed_turns(switched)[0]
        self.assertNotEqual(old.branch_id, new.branch_id)
        self.assertNotEqual(old.source_revision, new.source_revision)

        thinking_only = copy.deepcopy(switched)
        parsed = json.loads(thinking_only[1]['branches'])
        parsed[1]['thinking'] = 'changed-secret-only'
        thinking_only[1]['branches'] = json.dumps(parsed)
        self.assertEqual(
            derive_completed_turns(thinking_only)[0].source_revision,
            new.source_revision,
        )

    def test_attachment_revision_change_stales_row(self):
        original = row(1, 'hayana', 'file', attachments=json.dumps([
            {'type': 'file', 'url': '/static/uploads/files/aaaaaaaa_a.txt', 'name': 'a.txt'},
        ]))
        changed = copy.deepcopy(original)
        changed['attachments'] = json.dumps([
            {'type': 'file', 'url': '/static/uploads/files/bbbbbbbb_a.txt', 'name': 'a.txt'},
        ])
        self.assertNotEqual(row_revision(original), row_revision(changed))


class AutonomousEventTests(unittest.TestCase):
    def test_only_committed_unified_normal_wake_is_canonical(self):
        good_cache = {
            'wake_mode': 'normal',
            'canonical_chat_history': True,
            'unified_chat_resident': True,
            'b3_authority': True,
            'source': 'wake',
            'provider': 'claude_code',
        }
        rows = [
            row(10, 'assistant', 'canonical', source_kind='wake', cache_info=json.dumps(good_cache)),
            row(11, 'assistant', 'legacy', source_kind='wake', cache_info=json.dumps({
                **good_cache, 'canonical_chat_history': False,
            })),
            row(12, 'assistant', 'dream', source_kind='wake', cache_info=json.dumps({
                **good_cache, 'wake_mode': 'dream',
            })),
        ]
        events = derive_autonomous_events(rows)
        self.assertEqual([event.event_id for event in events], ['wake:10'])


class SourceSnapshotAndCoverageTests(unittest.TestCase):
    def _units(self):
        chat_rows = [row(1, 'hayana', 'u'), row(2, 'assistant', 'a')]
        wake_cache = json.dumps({
            'wake_mode': 'normal',
            'canonical_chat_history': True,
            'unified_chat_resident': True,
            'b3_authority': True,
            'source': 'wake',
            'provider': 'claude_code',
        })
        wake_rows = [row(3, 'assistant', 'w', source_kind='wake', cache_info=wake_cache)]
        return derive_completed_turns(chat_rows), derive_autonomous_events(wake_rows)

    def test_snapshot_membership_is_deterministic_and_unit_level(self):
        turns, events = self._units()
        first = build_source_snapshot(
            turns=turns,
            events=events,
            local_day='2026-09-08',
            source_watermark=3,
            created_at='2026-09-09 00:00:00',
        )
        second = build_source_snapshot(
            turns=turns,
            events=events,
            local_day='2026-09-08',
            source_watermark=3,
            created_at='2026-09-09 00:00:00',
        )
        self.assertEqual(first, second)
        self.assertEqual(
            [(m.seq, m.source_kind, m.source_ref) for m in first.members],
            [(0, 'completed_turn', 'turn:1:2'), (1, 'autonomous_event', 'wake:3')],
        )
        report = validate_exact_coverage(
            first.members,
            expected_source_refs=['turn:1:2', 'wake:3'],
        )
        self.assertTrue(report.valid)
        self.assertEqual(report.source_hash, first.source_hash)

    def test_validator_reports_duplicate_gap_and_revision_mismatch(self):
        members = (
            SourceMember(0, 'completed_turn', 'turn:1:2', 'a', 'conversation', 'a'),
            SourceMember(0, 'completed_turn', 'turn:1:2', 'b', 'conversation', 'not-b'),
        )
        report = validate_exact_coverage(
            members,
            expected_source_refs=['turn:1:2', 'turn:3:4'],
        )
        self.assertFalse(report.valid)
        codes = {issue.code for issue in report.issues}
        self.assertTrue({
            'duplicate_seq',
            'duplicate_source',
            'revision_hash_mismatch',
            'sequence_gap',
            'coverage_gap',
        }.issubset(codes))


if __name__ == '__main__':
    unittest.main()
