"""CONTINUITY-R4-R1 — pure ContextPlan representation and coverage tests."""
from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from continuity.context_plan import (
    ContextChunkBinding,
    build_context_plan,
)
from continuity.contracts import SourceMember, SourceSnapshot, candidate_source_revision
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


def _member(
    seq: int,
    *,
    ref: str | None = None,
    revision: str | None = None,
    content_hash: str | None = None,
    branch: str = 'active-transcript',
    logical_size: int = 20,
) -> SourceMember:
    return SourceMember(
        seq=seq,
        source_kind='completed_turn',
        source_ref=ref or f'turn:{seq}:{seq + 1}',
        source_revision=revision or f'revision-{seq}',
        role='conversation',
        content_hash=content_hash or f'content-{seq}',
        logical_size=logical_size,
        created_at=f'2026-09-08 12:0{seq}:00',
        branch_id=branch,
    )


def _snapshot(members: tuple[SourceMember, ...]) -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id='source:r4',
        identity_id='fyodor',
        chat_id='default',
        branch_id='active-transcript',
        local_day='2026-09-08',
        source_watermark=max((m.seq for m in members), default=0),
        policy_version='continuity_source_v1',
        source_hash=source_hash(members),
        status='ready',
        created_at='2026-09-09T00:00:00Z',
        members=members,
    )


def _binding(
    members: tuple[SourceMember, ...],
    *,
    seqs: tuple[int, ...],
    chunk_id: str = 'chunk:r4',
    status: str = 'ready',
    branch: str | None = None,
    body: str = 'compressed evidence',
    body_hash: str | None = None,
    source_revision: str | None = None,
) -> ContextChunkBinding:
    snap = _snapshot(members)
    by_seq = {item.seq: item for item in members}
    selected = tuple(by_seq[index] if index in by_seq else members[index] for index in seqs)
    candidate = CandidateBlock(
        candidate_id=f'candidate:{chunk_id}',
        snapshot_id=snap.snapshot_id,
        policy_version='continuity_sealing_v1_12k_20turns',
        block_seq=0,
        local_day=selected[0].created_at[:10],
        branch_id=branch or selected[0].branch_id,
        source_start_seq=selected[0].seq,
        source_end_seq=selected[-1].seq + 1,
        source_seqs=tuple(item.seq for item in selected),
        source_refs=tuple(item.source_ref for item in selected),
        source_revisions=tuple(item.source_revision for item in selected),
        logical_size=sum(item.logical_size for item in selected),
        completed_turn_count=len(selected),
        oversize=False,
        close_reason='test',
        source_revision=source_revision or candidate_source_revision(selected),
    )
    chunk = SimpleNamespace(
        chunk_id=chunk_id,
        generation_job_id=f'generation:{chunk_id}',
        candidate_id=candidate.candidate_id,
        snapshot_id=snap.snapshot_id,
        artifact_revision=f'artifact:{chunk_id}',
        body=body,
        body_hash=body_hash or hashlib.sha256(body.encode('utf-8')).hexdigest(),
        source_token_estimate=sum(item.logical_size for item in selected),
        output_token_estimate=estimate_tokens_heuristic_cjk1_ascii4_v1(body),
        status=status,
    )
    return ContextChunkBinding(chunk=chunk, candidate=candidate, snapshot=snap)


class ContextPlanTests(unittest.TestCase):
    def setUp(self):
        self.members = tuple(_member(index) for index in range(3))

    def test_raw_only_has_exact_coverage(self):
        plan = build_context_plan(self.members)
        self.assertTrue(plan.valid)
        self.assertEqual([item.kind for item in plan.representations], ['raw'])
        self.assertEqual(plan.representations[0].source_seqs, (0, 1, 2))
        self.assertEqual(plan.covered_source_refs, tuple(item.source_ref for item in self.members))

    def test_chunk_replaces_exactly_covered_raw_members(self):
        binding = _binding(self.members, seqs=(0, 1))
        plan = build_context_plan(self.members, ready_chunks=(binding,))
        self.assertEqual([item.kind for item in plan.representations], ['chunk', 'raw'])
        self.assertEqual(plan.representations[0].source_seqs, (0, 1))
        self.assertEqual(plan.representations[1].source_seqs, (2,))
        self.assertTrue(any(item.code == 'covered_by_chunk' for item in plan.exclusions))

    def test_newer_uncovered_raw_remains_selected(self):
        binding = _binding(self.members, seqs=(0,))
        plan = build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(plan.representations[-1].kind, 'raw')
        self.assertEqual(plan.representations[-1].source_seqs, (1, 2))

    def test_raw_and_chunk_never_double_select_same_member(self):
        binding = _binding(self.members, seqs=(0, 1))
        plan = build_context_plan(self.members, chunks=(binding,))
        selected = [seq for representation in plan.representations for seq in representation.source_seqs]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertNotIn(0, plan.representations[-1].source_seqs)
        self.assertNotIn(1, plan.representations[-1].source_seqs)

    def test_overlapping_chunks_are_excluded_deterministically(self):
        first = _binding(self.members, seqs=(0, 1), chunk_id='chunk:first')
        second = _binding(self.members, seqs=(1, 2), chunk_id='chunk:second')
        plan = build_context_plan(self.members, chunks=(second, first))
        self.assertEqual([item.kind for item in plan.representations], ['raw'])
        self.assertEqual(plan.representations[0].source_seqs, (0, 1, 2))
        rejected = {(item.code, item.representation_id) for item in plan.exclusions}
        self.assertIn(('chunk_overlap', 'chunk:chunk:first'), rejected)
        self.assertIn(('chunk_overlap', 'chunk:chunk:second'), rejected)

    def test_stale_source_revision_is_excluded(self):
        binding = _binding(self.members, seqs=(0,), source_revision='stale-candidate')
        plan = build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(plan.representations[0].kind, 'raw')
        self.assertTrue(any(item.code == 'chunk_rejected' for item in plan.exclusions))

    def test_gap_is_explicit_when_raw_is_unavailable(self):
        plan = build_context_plan(
            self.members,
            raw_members=(self.members[0],),
            expected_source_members=self.members,
        )
        self.assertFalse(plan.valid)
        self.assertEqual(
            [item.source_ref for item in plan.gaps],
            [self.members[1].source_ref, self.members[2].source_ref],
        )
        self.assertTrue(all(item.code == 'coverage_gap' for item in plan.gaps))

    def test_branch_mismatch_is_rejected(self):
        binding = _binding(self.members, seqs=(0,), branch='other-branch')
        plan = build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(plan.representations[0].kind, 'raw')
        self.assertTrue(any(item.code == 'chunk_rejected' for item in plan.exclusions))

    def test_plan_identity_is_deterministic(self):
        binding = _binding(self.members, seqs=(0, 1))
        first = build_context_plan(self.members, chunks=(binding,))
        second = build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.plan_hash, second.plan_hash)

    def test_source_order_is_stable_independent_of_chunk_input_order(self):
        first = _binding(self.members, seqs=(0,), chunk_id='chunk:first')
        second = _binding(self.members, seqs=(2,), chunk_id='chunk:second')
        plan = build_context_plan(self.members, chunks=(second, first))
        self.assertEqual([item.source_seqs for item in plan.representations], [(0,), (1,), (2,)])
        self.assertEqual([item.kind for item in plan.representations], ['chunk', 'raw', 'chunk'])

    def test_chunk_cost_uses_existing_token_estimator(self):
        body = '猫' * 40
        binding = _binding(self.members, seqs=(0,), body=body)
        plan = build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(
            plan.representations[0].estimated_tokens,
            estimate_tokens_heuristic_cjk1_ascii4_v1(body),
        )
        self.assertLess(
            plan.representations[0].estimated_tokens,
            len(body.encode('utf-8')),
        )

    def test_pure_plan_does_not_mutate_source_or_chunk(self):
        binding = _binding(self.members, seqs=(0, 1))
        before_members = tuple(self.members)
        before_body = binding.chunk.body
        build_context_plan(self.members, chunks=(binding,))
        self.assertEqual(self.members, before_members)
        self.assertEqual(binding.chunk.body, before_body)

    def test_non_contiguous_exact_membership_and_non_overlapping_spans_are_preserved(self):
        members = (
            SourceMember(
                seq=10, source_kind='attachment_span', source_ref='attachment:file-1',
                source_revision='revision-1', role='attachment', content_hash='content-1',
                span_start=0, span_end=4, logical_size=4,
                created_at='2026-09-08 12:00:00', branch_id='active-transcript',
            ),
            SourceMember(
                seq=20, source_kind='attachment_span', source_ref='attachment:file-1',
                source_revision='revision-1', role='attachment', content_hash='content-1',
                span_start=4, span_end=9, logical_size=5,
                created_at='2026-09-08 12:00:01', branch_id='active-transcript',
            ),
        )
        plan = build_context_plan(members)
        self.assertTrue(plan.valid)
        self.assertEqual(plan.representations[0].source_seqs, (10, 20))


if __name__ == '__main__':
    unittest.main()

