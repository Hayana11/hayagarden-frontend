"""CONTINUITY-R4-R1 — pure ContextPlan representation and coverage tests."""
from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from continuity.context_plan import (
    ContextBudgetPolicy,
    ContextChunkBinding,
    ContextPlanExclusion,
    ContextSection,
    _identity_payload,
    _sha256,
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
        self.assertEqual(
            tuple(item.source_ref for item in plan.covered_source_members),
            tuple(item.source_ref for item in self.members),
        )

    def test_chunk_replaces_exactly_covered_raw_members(self):
        binding = _binding(self.members, seqs=(0, 1))
        plan = build_context_plan(self.members, chunks=(binding,))
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

    def test_plan_hash_ignores_exclusion_detail_text(self):
        binding = _binding(self.members, seqs=(0,))
        plan = build_context_plan(self.members, chunks=(binding,))
        first = _identity_payload(
            plan.source_members,
            plan.representations,
            (ContextPlanExclusion('chunk_rejected', '', 'detail-a', 'chunk:x'),),
            plan.gaps,
        )
        second = _identity_payload(
            plan.source_members,
            plan.representations,
            (ContextPlanExclusion('chunk_rejected', '', 'detail-b', 'chunk:x'),),
            plan.gaps,
        )
        self.assertEqual(_sha256(first), _sha256(second))

    def test_recent_raw_target_selects_newest_whole_member_suffix(self):
        members = tuple(_member(index, logical_size=10) for index in range(3))
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(
                token_budget=50,
                reserve_budget=0,
                recent_raw_target=15,
            ),
        )
        self.assertEqual(plan.recent_raw_source_seqs, (1, 2))
        self.assertEqual(
            [item.source_seqs for item in plan.representations],
            [(0,), (1, 2)],
        )
        self.assertEqual(plan.recent_raw_token_estimate, 20)

    def test_recent_raw_wins_over_chunk_covering_newest_range(self):
        members = tuple(_member(index, logical_size=10) for index in range(3))
        binding = _binding(members, seqs=(1, 2), body='x' * 8)
        plan = build_context_plan(
            members,
            chunks=(binding,),
            budget_policy=ContextBudgetPolicy(token_budget=50, recent_raw_target=15),
        )
        self.assertEqual([item.kind for item in plan.representations], ['raw', 'raw'])
        self.assertEqual([item.source_seqs for item in plan.representations], [(0,), (1, 2)])
        self.assertTrue(any(item.code == 'recent_raw_priority' for item in plan.exclusions))

    def test_recent_raw_range_is_expected_driven_when_newest_raw_is_missing(self):
        members = tuple(_member(index, logical_size=10) for index in range(3))
        binding = _binding(members, seqs=(2,), body='x' * 8)
        plan = build_context_plan(
            members,
            raw_members=members[:2],
            chunks=(binding,),
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=10),
        )
        self.assertEqual(plan.recent_raw_source_seqs, (2,))
        selected_seqs = tuple(
            seq for representation in plan.representations for seq in representation.source_seqs
        )
        self.assertNotIn(2, selected_seqs)
        self.assertFalse(any(
            item.kind == 'chunk' and 2 in item.source_seqs
            for item in plan.representations
        ))
        self.assertTrue(any(
            item.code == 'coverage_gap' and item.source_ref == members[2].source_ref
            for item in plan.gaps
        ))
        self.assertFalse(plan.valid)

    def test_older_valid_chunk_remains_after_recent_raw_priority(self):
        members = tuple(_member(index, logical_size=4) for index in range(3))
        binding = _binding(members, seqs=(0,), body='x' * 8)
        plan = build_context_plan(
            members,
            chunks=(binding,),
            budget_policy=ContextBudgetPolicy(token_budget=10, recent_raw_target=4),
        )
        self.assertTrue(plan.valid)
        self.assertEqual(
            [(item.kind, item.source_seqs) for item in plan.representations],
            [('chunk', (0,)), ('raw', (1,)), ('raw', (2,))],
        )
        self.assertEqual(plan.selected_token_estimate, 10)
        self.assertEqual(plan.remaining_budget, 0)

    def test_oldest_older_representation_drops_first(self):
        members = tuple(_member(index, logical_size=5) for index in range(4))
        bindings = tuple(
            _binding(members, seqs=(index,), chunk_id=f'chunk:{index}', body='x' * 8)
            for index in range(3)
        )
        plan = build_context_plan(
            members,
            chunks=bindings,
            budget_policy=ContextBudgetPolicy(token_budget=10, recent_raw_target=5),
        )
        self.assertEqual([item.source_seqs for item in plan.representations], [(1,), (2,), (3,)])
        self.assertTrue(any(
            item.code == 'budget_excluded' and item.representation_id == 'chunk:chunk:0'
            for item in plan.exclusions
        ))
        self.assertFalse(any(item.code == 'coverage_gap' for item in plan.gaps))

    def test_older_budget_drops_oldest_until_newest_suffix_fits(self):
        members = tuple(_member(index, logical_size=1) for index in range(2))
        older = (
            _binding(members, seqs=(0,), chunk_id='a', body='a' * 16),
            _binding(members, seqs=(1,), chunk_id='b', body='b' * 32),
        )
        over_budget = build_context_plan(
            members,
            chunks=older,
            budget_policy=ContextBudgetPolicy(token_budget=7),
        )
        self.assertEqual(over_budget.representations, ())
        self.assertEqual(
            {
                item.representation_id
                for item in over_budget.budget_exclusions
            },
            {'chunk:a', 'chunk:b'},
        )
        fits_newest = build_context_plan(
            members,
            chunks=older,
            budget_policy=ContextBudgetPolicy(token_budget=9),
        )
        self.assertEqual(
            [item.source_seqs for item in fits_newest.representations],
            [(1,)],
        )
        self.assertTrue(any(
            item.code == 'budget_excluded' and item.representation_id == 'chunk:a'
            for item in fits_newest.exclusions
        ))

    def test_budget_exclusion_is_not_coverage_gap(self):
        members = tuple(_member(index, logical_size=5) for index in range(3))
        bindings = tuple(
            _binding(members, seqs=(index,), chunk_id=f'chunk:{index}', body='x' * 8)
            for index in range(2)
        )
        plan = build_context_plan(
            members,
            chunks=bindings,
            budget_policy=ContextBudgetPolicy(token_budget=5, recent_raw_target=0),
        )
        self.assertTrue(plan.valid)
        self.assertTrue(plan.budget_exclusions)
        self.assertEqual(plan.gaps, ())

    def test_true_missing_representation_remains_coverage_gap(self):
        members = tuple(_member(index, logical_size=5) for index in range(3))
        plan = build_context_plan(
            members,
            raw_members=(members[0],),
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=0),
        )
        self.assertFalse(plan.valid)
        self.assertEqual(
            [item.source_ref for item in plan.gaps],
            [members[1].source_ref, members[2].source_ref],
        )
        self.assertFalse(plan.budget_exclusions)

    def test_recent_raw_alone_overflow_is_explicit(self):
        members = tuple(_member(index, logical_size=10) for index in range(2))
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=15, recent_raw_target=15),
        )
        self.assertFalse(plan.valid)
        self.assertTrue(plan.budget_overflow)
        self.assertEqual(plan.budget_status, 'overflow')
        self.assertEqual(plan.recent_raw_token_estimate, 20)
        self.assertEqual(plan.remaining_budget, -5)
        self.assertTrue(any(item.code == 'budget_overflow' for item in plan.exclusions))

    def test_reserve_at_total_budget_is_explicitly_blocked(self):
        members = tuple(_member(index, logical_size=5) for index in range(2))
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=10, reserve_budget=10),
        )
        self.assertFalse(plan.valid)
        self.assertEqual(plan.budget_status, 'blocked')
        self.assertEqual(plan.usable_budget, 0)
        self.assertTrue(any(item.code == 'reserve_exceeds_budget' for item in plan.exclusions))

    def test_oversize_newest_member_is_kept_whole(self):
        members = (
            _member(0, logical_size=3),
            _member(1, logical_size=50),
        )
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=10),
        )
        self.assertEqual(plan.recent_raw_source_seqs, (1,))
        self.assertEqual([item.source_seqs for item in plan.representations], [(0,), (1,)])
        self.assertEqual(plan.recent_raw_token_estimate, 50)

    def test_budget_policy_is_part_of_deterministic_identity(self):
        members = tuple(_member(index, logical_size=3) for index in range(3))
        first = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=10, recent_raw_target=3),
        )
        second = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=10, recent_raw_target=3),
        )
        changed = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=11, recent_raw_target=3),
        )
        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertNotEqual(first.plan_hash, changed.plan_hash)

    @staticmethod
    def _section(kind, *, source_ref=None, content_hash=None, estimated_tokens=3):
        return ContextSection(
            kind=kind,
            source_ref=source_ref or f'{kind}:v1',
            content_hash=content_hash or f'hash:{kind}:v1',
            estimated_tokens=estimated_tokens,
        )

    def test_ordered_sections_use_canonical_order(self):
        plan = build_context_plan(
            tuple(_member(index, logical_size=5) for index in range(3)),
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=5),
            fixed_sections=(
                self._section('current_request'),
                self._section('accepted_open_loops'),
                self._section('invariant_system'),
                self._section('accepted_state'),
            ),
        )
        self.assertEqual(
            [section.kind for section in plan.ordered_sections],
            [
                'invariant_system',
                'accepted_state',
                'accepted_open_loops',
                'older_continuity',
                'recent_raw',
                'current_request',
            ],
        )

    def test_history_sections_reference_selected_representations(self):
        plan = build_context_plan(
            self.members,
            fixed_sections=(self._section('accepted_state'),),
        )
        representation_ids = {item.representation_id for item in plan.representations}
        history = [
            section for section in plan.ordered_sections
            if section.kind in {'older_continuity', 'recent_raw'}
        ]
        self.assertEqual(
            {section.representation_id for section in history},
            representation_ids,
        )
        self.assertTrue(all(
            section.source_ref == section.representation_id
            for section in history
        ))

    def test_sections_do_not_reselect_or_duplicate_history_members(self):
        binding = _binding(self.members, seqs=(0, 1))
        plan = build_context_plan(
            self.members,
            chunks=(binding,),
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=10),
        )
        history_sections = [
            section for section in plan.ordered_sections
            if section.kind in {'older_continuity', 'recent_raw'}
        ]
        selected_ids = {
            section.representation_id for section in history_sections
        }
        self.assertEqual(selected_ids, {
            item.representation_id for item in plan.representations
        })
        selected_seqs = [
            seq for item in plan.representations for seq in item.source_seqs
        ]
        self.assertEqual(selected_seqs, sorted(set(selected_seqs)))

    def test_recent_raw_sections_follow_older_continuity(self):
        members = tuple(_member(index, logical_size=5) for index in range(3))
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=100, recent_raw_target=5),
        )
        kinds = [section.kind for section in plan.ordered_sections]
        self.assertLess(kinds.index('older_continuity'), kinds.index('recent_raw'))

    def test_accepted_state_and_open_loops_precede_history(self):
        plan = build_context_plan(
            self.members,
            fixed_sections=(
                self._section('accepted_open_loops'),
                self._section('accepted_state'),
            ),
        )
        kinds = [section.kind for section in plan.ordered_sections]
        self.assertLess(kinds.index('accepted_state'), kinds.index('older_continuity'))
        self.assertLess(kinds.index('accepted_open_loops'), kinds.index('older_continuity'))

    def test_absent_fixed_sections_have_no_empty_placeholders(self):
        plan = build_context_plan(self.members)
        self.assertEqual(
            {section.kind for section in plan.ordered_sections},
            {'older_continuity'},
        )
        self.assertNotIn('accepted_state', [section.kind for section in plan.ordered_sections])
        self.assertNotIn('accepted_open_loops', [section.kind for section in plan.ordered_sections])

    def test_current_request_is_last_semantic_section(self):
        plan = build_context_plan(
            self.members,
            fixed_sections=(
                self._section('invariant_system'),
                self._section('current_request'),
            ),
        )
        self.assertEqual(plan.ordered_sections[-1].kind, 'current_request')

    def test_fixed_section_fingerprint_is_part_of_plan_identity(self):
        first = build_context_plan(
            self.members,
            fixed_sections=(self._section('accepted_state', content_hash='state:a'),),
        )
        same = build_context_plan(
            self.members,
            fixed_sections=(self._section('accepted_state', content_hash='state:a'),),
        )
        changed = build_context_plan(
            self.members,
            fixed_sections=(self._section('accepted_state', content_hash='state:b'),),
        )
        self.assertEqual(first.plan_hash, same.plan_hash)
        self.assertNotEqual(first.plan_hash, changed.plan_hash)
        self.assertNotEqual(first.plan_id, changed.plan_id)

    def test_current_request_fingerprint_is_part_of_plan_identity(self):
        first = build_context_plan(
            self.members,
            fixed_sections=(self._section('current_request', content_hash='request:a'),),
        )
        changed = build_context_plan(
            self.members,
            fixed_sections=(self._section('current_request', content_hash='request:b'),),
        )
        self.assertNotEqual(first.plan_hash, changed.plan_hash)

    def test_fixed_section_input_order_does_not_change_plan_identity(self):
        sections = (
            self._section('invariant_system'),
            self._section('accepted_state'),
            self._section('accepted_open_loops'),
            self._section('current_request'),
        )
        first = build_context_plan(self.members, fixed_sections=sections)
        reordered = build_context_plan(
            self.members,
            fixed_sections=(sections[3], sections[1], sections[0], sections[2]),
        )
        self.assertEqual(first.ordered_sections, reordered.ordered_sections)
        self.assertEqual(first.plan_hash, reordered.plan_hash)

    def test_history_section_metadata_matches_representation(self):
        plan = build_context_plan(self.members)
        by_id = {item.representation_id: item for item in plan.representations}
        for section in plan.ordered_sections:
            if section.representation_id is None:
                continue
            representation = by_id[section.representation_id]
            self.assertEqual(section.content_hash, representation.source_hash)
            self.assertEqual(section.estimated_tokens, representation.estimated_tokens)

    def test_section_text_is_not_stored_or_used_for_identity(self):
        section = self._section('accepted_state', content_hash='state:a')
        plan = build_context_plan(self.members, fixed_sections=(section,))
        self.assertFalse(hasattr(plan.ordered_sections[0], 'text'))
        self.assertEqual(plan.ordered_sections[0].content_hash, 'state:a')

    def test_ordered_section_token_accounting_includes_fixed_and_reserve(self):
        members = tuple(_member(index, logical_size=5) for index in range(2))
        plan = build_context_plan(
            members,
            budget_policy=ContextBudgetPolicy(token_budget=20, reserve_budget=2),
            fixed_sections=(
                self._section('invariant_system', estimated_tokens=3),
                self._section('accepted_state', estimated_tokens=4),
                self._section('current_request', estimated_tokens=2),
            ),
        )
        self.assertEqual(plan.selected_token_estimate, 10)
        self.assertEqual(plan.fixed_section_token_estimate, 9)
        self.assertEqual(plan.ordered_section_token_estimate, 19)
        self.assertEqual(plan.total_token_estimate, 21)
        self.assertEqual(plan.reserve_budget, 2)

    def test_fixed_section_contract_rejects_history_and_duplicates(self):
        with self.assertRaisesRegex(ValueError, 'history_sections_are_plan_owned'):
            build_context_plan(
                self.members,
                fixed_sections=(ContextSection(
                    kind='older_continuity',
                    source_ref='representation:old',
                    content_hash='hash:old',
                    estimated_tokens=1,
                    representation_id='representation:old',
                ),),
            )
        with self.assertRaisesRegex(ValueError, 'duplicate_kind'):
            build_context_plan(
                self.members,
                fixed_sections=(
                    self._section('accepted_state', content_hash='a'),
                    self._section('accepted_state', content_hash='b'),
                ),
            )

    def test_invalid_policy_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'token_budget_must_be_positive'):
            ContextBudgetPolicy(token_budget=0)
        with self.assertRaisesRegex(ValueError, 'reserve_budget_must_be_non_negative'):
            ContextBudgetPolicy(token_budget=10, reserve_budget=-1)


if __name__ == '__main__':
    unittest.main()


