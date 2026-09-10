"""Pure R4 representation planning for exact Continuity source membership.

The core chooses one representation for each required source member. It has
no provider, runtime, persistence, or payload responsibilities.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from continuity.contracts import (
    ContinuityChunk,
    SourceMember,
    SourceSnapshot,
    candidate_source_revision,
)
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock, MEASUREMENT_SEMANTICS
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


RepresentationKind = Literal['raw', 'chunk']
BudgetStatus = Literal['unbounded', 'fit', 'overflow', 'blocked']
SectionKind = Literal[
    'invariant_system',
    'accepted_state',
    'accepted_open_loops',
    'older_continuity',
    'recent_raw',
    'current_request',
]

_FIXED_SECTION_KINDS = frozenset({
    'invariant_system',
    'accepted_state',
    'accepted_open_loops',
    'current_request',
})
_HISTORY_SECTION_KINDS = frozenset({'older_continuity', 'recent_raw'})
_SECTION_ORDER = (
    'invariant_system',
    'accepted_state',
    'accepted_open_loops',
    'older_continuity',
    'recent_raw',
    'current_request',
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _member_key(member: SourceMember) -> tuple[Any, ...]:
    """Exact R1 identity, including non-contiguous attachment spans."""
    return (
        int(member.seq),
        str(member.source_kind),
        str(member.source_ref),
        str(member.source_revision),
        str(member.content_hash),
        member.span_start,
        member.span_end,
        str(member.branch_id),
    )


def _member_identity(member: SourceMember) -> dict[str, Any]:
    """Stable plan identity fields; wall-clock creation time is excluded."""
    return {
        'seq': int(member.seq),
        'source_kind': str(member.source_kind),
        'source_ref': str(member.source_ref),
        'source_revision': str(member.source_revision),
        'content_hash': str(member.content_hash),
        'span_start': member.span_start,
        'span_end': member.span_end,
        'logical_size': int(member.logical_size),
        'branch_id': str(member.branch_id),
    }


def _ordered(members: Sequence[SourceMember]) -> tuple[SourceMember, ...]:
    return tuple(sorted(tuple(members), key=lambda member: int(member.seq)))


@dataclass(frozen=True)
class ContextChunkBinding:
    """The only accepted chunk input: chunk plus its R3 proof objects."""

    chunk: ContinuityChunk
    candidate: CandidateBlock
    snapshot: SourceSnapshot


@dataclass(frozen=True)
class ContextBudgetPolicy:
    """Explicit, pure budget inputs for one shadow ContextPlan."""

    token_budget: int
    reserve_budget: int = 0
    recent_raw_target: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ('token_budget', self.token_budget),
            ('reserve_budget', self.reserve_budget),
            ('recent_raw_target', self.recent_raw_target),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f'invalid_budget_policy:{name}_must_be_integer')
        if self.token_budget <= 0:
            raise ValueError('invalid_budget_policy:token_budget_must_be_positive')
        if self.reserve_budget < 0:
            raise ValueError('invalid_budget_policy:reserve_budget_must_be_non_negative')
        if self.recent_raw_target < 0:
            raise ValueError('invalid_budget_policy:recent_raw_target_must_be_non_negative')

    @property
    def usable_budget(self) -> int:
        return self.token_budget - self.reserve_budget


@dataclass(frozen=True)
class ContextSection:
    """A position in one plan, without duplicating representation content.

    Fixed sections are accepted projections from an upstream authority and
    carry metadata only. History sections reference an existing
    ``ContextRepresentation`` by ``representation_id``.
    """

    kind: SectionKind
    source_ref: str
    content_hash: str
    estimated_tokens: int
    representation_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SECTION_ORDER:
            raise ValueError(f'invalid_context_section:unknown_kind:{self.kind}')
        if not str(self.source_ref).strip():
            raise ValueError('invalid_context_section:source_ref_required')
        if not str(self.content_hash).strip():
            raise ValueError('invalid_context_section:content_hash_required')
        if isinstance(self.estimated_tokens, bool) or not isinstance(self.estimated_tokens, int):
            raise ValueError('invalid_context_section:estimated_tokens_must_be_integer')
        if self.estimated_tokens < 0:
            raise ValueError('invalid_context_section:estimated_tokens_must_be_non_negative')
        if self.kind in _FIXED_SECTION_KINDS and self.representation_id is not None:
            raise ValueError('invalid_context_section:fixed_section_cannot_reference_representation')
        if self.kind in _HISTORY_SECTION_KINDS and not str(self.representation_id or '').strip():
            raise ValueError('invalid_context_section:history_section_requires_representation')


@dataclass(frozen=True)
class ContextPlanExclusion:
    code: str
    source_ref: str
    detail: str
    representation_id: str | None = None


@dataclass(frozen=True)
class ContextRepresentation:
    representation_id: str
    kind: RepresentationKind
    source_members: tuple[SourceMember, ...]
    source_refs: tuple[str, ...]
    source_revisions: tuple[str, ...]
    source_hash: str
    estimated_tokens: int
    chunk_id: str | None = None
    candidate_id: str | None = None
    snapshot_id: str | None = None
    provenance: tuple[tuple[str, str], ...] = ()

    @property
    def source_seqs(self) -> tuple[int, ...]:
        return tuple(int(member.seq) for member in self.source_members)


@dataclass(frozen=True)
class ContextPlan:
    plan_id: str
    plan_hash: str
    source_hash: str
    source_members: tuple[SourceMember, ...]
    representations: tuple[ContextRepresentation, ...]
    ordered_sections: tuple[ContextSection, ...]
    exclusions: tuple[ContextPlanExclusion, ...]
    gaps: tuple[ContextPlanExclusion, ...]
    measurement_semantics: str = MEASUREMENT_SEMANTICS
    budget_policy: ContextBudgetPolicy | None = None
    budget_status: BudgetStatus = 'unbounded'
    recent_raw_source_seqs: tuple[int, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.gaps and self.budget_status not in ('overflow', 'blocked')

    @property
    def token_budget(self) -> int | None:
        return self.budget_policy.token_budget if self.budget_policy else None

    @property
    def reserve_budget(self) -> int | None:
        return self.budget_policy.reserve_budget if self.budget_policy else None

    @property
    def usable_budget(self) -> int | None:
        return self.budget_policy.usable_budget if self.budget_policy else None

    @property
    def selected_token_estimate(self) -> int:
        return sum(int(item.estimated_tokens) for item in self.representations)

    @property
    def fixed_section_token_estimate(self) -> int:
        return sum(
            int(section.estimated_tokens)
            for section in self.ordered_sections
            if section.kind in _FIXED_SECTION_KINDS
        )

    @property
    def ordered_section_token_estimate(self) -> int:
        return sum(int(section.estimated_tokens) for section in self.ordered_sections)

    @property
    def total_token_estimate(self) -> int:
        """Fixed sections + selected continuity + reserved budget."""
        reserve = self.reserve_budget or 0
        return self.ordered_section_token_estimate + int(reserve)

    @property
    def recent_raw_token_estimate(self) -> int:
        recent = frozenset(int(seq) for seq in self.recent_raw_source_seqs)
        return sum(
            int(item.estimated_tokens)
            for item in self.representations
            if item.kind == 'raw' and frozenset(item.source_seqs).issubset(recent)
        )

    @property
    def older_representation_token_estimate(self) -> int:
        return self.selected_token_estimate - self.recent_raw_token_estimate

    @property
    def remaining_budget(self) -> int | None:
        if self.usable_budget is None:
            return None
        return self.usable_budget - self.selected_token_estimate

    @property
    def budget_overflow(self) -> bool:
        return self.budget_status == 'overflow'

    @property
    def budget_exclusions(self) -> tuple[ContextPlanExclusion, ...]:
        return tuple(item for item in self.exclusions if item.code == 'budget_excluded')

    @property
    def covered_source_members(self) -> tuple[SourceMember, ...]:
        members = [
            member
            for representation in self.representations
            for member in representation.source_members
        ]
        return tuple(sorted(members, key=lambda member: int(member.seq)))


def _raw_representation(members: Sequence[SourceMember]) -> ContextRepresentation:
    ordered = tuple(members)
    digest = _sha256({'kind': 'raw', 'members': [_member_identity(member) for member in ordered]})
    member_hash = source_hash(ordered)
    return ContextRepresentation(
        representation_id=f'raw:{digest[:32]}',
        kind='raw',
        source_members=ordered,
        source_refs=tuple(member.source_ref for member in ordered),
        source_revisions=tuple(member.source_revision for member in ordered),
        source_hash=member_hash,
        estimated_tokens=sum(max(0, int(member.logical_size)) for member in ordered),
        provenance=(
            ('measurement_semantics', MEASUREMENT_SEMANTICS),
            ('source_hash', member_hash),
        ),
    )


def _chunk_representation(
    binding: ContextChunkBinding,
    members: Sequence[SourceMember],
) -> ContextRepresentation:
    chunk = binding.chunk
    candidate = binding.candidate
    snapshot = binding.snapshot
    ordered = tuple(members)
    member_hash = source_hash(ordered)
    provenance = tuple(sorted({
        'measurement_semantics': MEASUREMENT_SEMANTICS,
        'source_hash': member_hash,
        'chunk_id': str(chunk.chunk_id),
        'candidate_id': str(candidate.candidate_id),
        'snapshot_id': str(snapshot.snapshot_id),
        'artifact_revision': str(chunk.artifact_revision),
        'body_hash': str(chunk.body_hash),
    }.items()))
    return ContextRepresentation(
        representation_id=f'chunk:{chunk.chunk_id}',
        kind='chunk',
        source_members=ordered,
        source_refs=tuple(member.source_ref for member in ordered),
        source_revisions=tuple(member.source_revision for member in ordered),
        source_hash=member_hash,
        estimated_tokens=int(estimate_tokens_heuristic_cjk1_ascii4_v1(chunk.body)),
        chunk_id=str(chunk.chunk_id),
        candidate_id=str(candidate.candidate_id),
        snapshot_id=str(snapshot.snapshot_id),
        provenance=provenance,
    )


def _exclusion(
    code: str,
    detail: str,
    *,
    source_ref: str = '',
    representation_id: str | None = None,
) -> ContextPlanExclusion:
    return ContextPlanExclusion(code, source_ref, detail, representation_id)


def _validated_fixed_sections(
    sections: Sequence[ContextSection],
) -> dict[str, ContextSection]:
    by_kind: dict[str, ContextSection] = {}
    for section in tuple(sections):
        if not isinstance(section, ContextSection):
            raise ValueError('invalid_context_section:expected_context_section')
        if section.kind not in _FIXED_SECTION_KINDS:
            raise ValueError('invalid_context_section:history_sections_are_plan_owned')
        if section.kind in by_kind:
            raise ValueError(f'invalid_context_section:duplicate_kind:{section.kind}')
        by_kind[section.kind] = section
    return by_kind


def _history_section(
    representation: ContextRepresentation,
    *,
    kind: Literal['older_continuity', 'recent_raw'],
) -> ContextSection:
    return ContextSection(
        kind=kind,
        source_ref=representation.representation_id,
        content_hash=representation.source_hash,
        estimated_tokens=int(representation.estimated_tokens),
        representation_id=representation.representation_id,
    )


def _build_ordered_sections(
    fixed_sections: dict[str, ContextSection],
    representations: Sequence[ContextRepresentation],
    recent_raw_keys: set[tuple[Any, ...]],
) -> tuple[ContextSection, ...]:
    sections: list[ContextSection] = []
    for kind in ('invariant_system', 'accepted_state', 'accepted_open_loops'):
        section = fixed_sections.get(kind)
        if section is not None:
            sections.append(section)

    for representation in representations:
        member_keys = {_member_key(member) for member in representation.source_members}
        section_kind: Literal['older_continuity', 'recent_raw'] = (
            'recent_raw'
            if representation.kind == 'raw' and member_keys and member_keys.issubset(recent_raw_keys)
            else 'older_continuity'
        )
        sections.append(_history_section(representation, kind=section_kind))

    current_request = fixed_sections.get('current_request')
    if current_request is not None:
        sections.append(current_request)
    return tuple(sections)


def _section_identity(section: ContextSection) -> dict[str, Any]:
    return {
        'kind': section.kind,
        'source_ref': section.source_ref,
        'content_hash': section.content_hash,
        'estimated_tokens': int(section.estimated_tokens),
        'representation_id': section.representation_id,
    }


def _identity_payload(
    expected: Sequence[SourceMember],
    selected: Sequence[ContextRepresentation],
    exclusions: Sequence[ContextPlanExclusion],
    gaps: Sequence[ContextPlanExclusion],
    *,
    ordered_sections: Sequence[ContextSection] = (),
    budget_policy: ContextBudgetPolicy | None = None,
    budget_status: BudgetStatus = 'unbounded',
    recent_raw_source_seqs: Sequence[int] = (),
) -> dict[str, Any]:
    return {
        'measurement_semantics': MEASUREMENT_SEMANTICS,
        'budget_policy': (
            {
                'token_budget': budget_policy.token_budget,
                'reserve_budget': budget_policy.reserve_budget,
                'recent_raw_target': budget_policy.recent_raw_target,
            }
            if budget_policy is not None else None
        ),
        'budget_status': budget_status,
        'recent_raw_source_seqs': [int(seq) for seq in recent_raw_source_seqs],
        'source_members': [_member_identity(member) for member in expected],
        'representations': [
            {
                'kind': representation.kind,
                'source_members': [_member_identity(member) for member in representation.source_members],
                'source_hash': representation.source_hash,
                'estimated_tokens': int(representation.estimated_tokens),
                'chunk_id': representation.chunk_id,
                'candidate_id': representation.candidate_id,
                'snapshot_id': representation.snapshot_id,
                'provenance': list(representation.provenance),
            }
            for representation in selected
        ],
        'ordered_sections': [_section_identity(section) for section in ordered_sections],
        # Details are human diagnostics only and deliberately do not affect
        # deterministic identity.
        'exclusions': sorted(
            (item.code, item.source_ref, item.representation_id)
            for item in exclusions
        ),
        'gaps': sorted((item.code, item.source_ref) for item in gaps),
    }


def _member_overlap(left: SourceMember, right: SourceMember) -> bool:
    if left.source_ref != right.source_ref:
        return False
    if left.source_revision != right.source_revision:
        return True
    if left.span_start is not None and right.span_start is not None:
        return int(left.span_start) < int(right.span_end) and int(right.span_start) < int(left.span_end)
    return True


def _recent_raw_suffix(
    expected: Sequence[SourceMember],
    target: int,
) -> set[tuple[Any, ...]]:
    """Return the expected newest whole-member suffix for the raw priority."""
    if target <= 0:
        return set()
    selected: set[tuple[Any, ...]] = set()
    estimate = 0
    for member in reversed(tuple(expected)):
        selected.add(_member_key(member))
        estimate += max(0, int(member.logical_size))
        if estimate >= target:
            break
    return selected


def _budget_exclusion(representation: ContextRepresentation, detail: str) -> ContextPlanExclusion:
    return _exclusion(
        'budget_excluded',
        detail,
        source_ref=','.join(representation.source_refs),
        representation_id=representation.representation_id,
    )


def _apply_budget(
    representations: Sequence[ContextRepresentation],
    *,
    budget_policy: ContextBudgetPolicy | None,
    recent_raw_keys: set[tuple[Any, ...]],
    exclusions: list[ContextPlanExclusion],
) -> tuple[tuple[ContextRepresentation, ...], BudgetStatus]:
    """Apply reserve, recent-raw priority, then oldest-first trimming."""
    if budget_policy is None:
        return tuple(representations), 'unbounded'

    if budget_policy.usable_budget <= 0:
        exclusions.append(_exclusion(
            'reserve_exceeds_budget',
            'reserve_budget leaves no usable context budget',
        ))
        exclusions.extend(
            _budget_exclusion(item, 'representation excluded because reserve consumes the budget')
            for item in representations
        )
        return (), 'blocked'

    recent = tuple(
        item for item in representations
        if item.kind == 'raw'
        and bool(set(_member_key(member) for member in item.source_members) & recent_raw_keys)
    )
    older = tuple(item for item in representations if item not in recent)
    recent_cost = sum(int(item.estimated_tokens) for item in recent)
    if recent_cost > budget_policy.usable_budget:
        exclusions.append(_exclusion(
            'budget_overflow',
            'recent raw suffix alone exceeds usable budget',
        ))
        exclusions.extend(
            _budget_exclusion(item, 'older representation excluded after recent raw overflow')
            for item in older
        )
        return tuple(recent), 'overflow'

    remaining = budget_policy.usable_budget - recent_cost
    ordered_older = sorted(
        older,
        key=lambda value: (
            value.source_seqs[0] if value.source_seqs else 0,
            value.source_seqs[-1] if value.source_seqs else 0,
            value.representation_id,
        ),
    )
    selected_older = list(ordered_older)
    older_cost = sum(int(item.estimated_tokens) for item in selected_older)
    while selected_older and older_cost > remaining:
        item = selected_older.pop(0)
        older_cost -= int(item.estimated_tokens)
        exclusions.append(_budget_exclusion(
            item,
            'oldest older representation excluded from the remaining budget',
        ))
    return tuple(recent) + tuple(selected_older), 'fit'


def build_context_plan(
    expected_members: Sequence[SourceMember],
    *,
    raw_members: Sequence[SourceMember] | None = None,
    chunks: Sequence[ContextChunkBinding] = (),
    budget_policy: ContextBudgetPolicy | None = None,
    fixed_sections: Sequence[ContextSection] = (),
) -> ContextPlan:
    """Select exact raw/chunk representations for one required source set."""
    expected = _ordered(expected_members)
    accepted_fixed_sections = _validated_fixed_sections(fixed_sections)
    raw = _ordered(expected if raw_members is None else raw_members)
    expected_by_key = {_member_key(member): member for member in expected}
    exclusions: list[ContextPlanExclusion] = []
    raw_keys: set[tuple[Any, ...]] = set()

    for member in raw:
        key = _member_key(member)
        if key not in expected_by_key:
            exclusions.append(_exclusion(
                'unexpected_raw',
                'raw member is outside expected source coverage',
                source_ref=member.source_ref,
            ))
        elif key in raw_keys:
            exclusions.append(_exclusion(
                'duplicate_raw',
                'raw member appears more than once',
                source_ref=member.source_ref,
            ))
        else:
            raw_keys.add(key)

    recent_raw_keys = _recent_raw_suffix(
        expected,
        budget_policy.recent_raw_target if budget_policy else 0,
    )

    valid_chunks: list[tuple[ContextRepresentation, set[tuple[Any, ...]]]] = []
    for binding in tuple(chunks):
        chunk = binding.chunk
        candidate = binding.candidate
        snapshot = binding.snapshot
        representation_id = f'chunk:{chunk.chunk_id}'
        try:
            if chunk.status != 'ready':
                raise ValueError('chunk is not ready')
            if chunk.snapshot_id != snapshot.snapshot_id:
                raise ValueError('chunk snapshot identity mismatch')
            if candidate.snapshot_id != snapshot.snapshot_id:
                raise ValueError('candidate snapshot identity mismatch')
            if chunk.candidate_id != candidate.candidate_id:
                raise ValueError('chunk candidate identity mismatch')
            if source_hash(snapshot.members) != snapshot.source_hash:
                raise ValueError('chunk snapshot source hash mismatch')

            seqs = tuple(int(seq) for seq in candidate.source_seqs)
            refs = tuple(str(ref) for ref in candidate.source_refs)
            revisions = tuple(str(revision) for revision in candidate.source_revisions)
            if not (len(seqs) == len(refs) == len(revisions)) or not seqs:
                raise ValueError('chunk candidate membership is incomplete')
            snapshot_by_seq = {int(member.seq): member for member in snapshot.members}
            members: list[SourceMember] = []
            for seq, ref, revision in zip(seqs, refs, revisions):
                member = snapshot_by_seq.get(seq)
                if member is None:
                    raise ValueError(f'chunk source member seq {seq} is missing')
                if member.source_ref != ref or member.source_revision != revision:
                    raise ValueError(f'chunk source membership mismatch for {ref}')
                members.append(member)
            selected_members = tuple(members)
            member_keys = {_member_key(member) for member in selected_members}
            if not member_keys.issubset(set(expected_by_key)):
                raise ValueError('chunk membership is outside expected source coverage')
            if candidate.source_revision != candidate_source_revision(selected_members):
                raise ValueError('chunk candidate source revision is stale')
            if any(member.branch_id != candidate.branch_id for member in selected_members):
                raise ValueError('chunk branch identity mismatch')
            if not chunk.body.strip():
                raise ValueError('chunk body is missing')
            if hashlib.sha256(chunk.body.encode('utf-8')).hexdigest() != chunk.body_hash:
                raise ValueError('chunk body hash mismatch')
            representation = _chunk_representation(binding, selected_members)
            valid_chunks.append((representation, member_keys))
        except (TypeError, ValueError, AttributeError) as exc:
            exclusions.append(_exclusion(
                'chunk_rejected',
                str(exc),
                representation_id=representation_id,
            ))

    conflicting: set[int] = set()
    for left_index, (left, left_keys) in enumerate(valid_chunks):
        for right_index in range(left_index + 1, len(valid_chunks)):
            right, right_keys = valid_chunks[right_index]
            overlap = bool(left_keys & right_keys)
            if not overlap:
                overlap = any(
                    _member_overlap(left_member, right_member)
                    for left_member in left.source_members
                    for right_member in right.source_members
                )
            if overlap:
                conflicting.update((left_index, right_index))
                exclusions.extend((
                    _exclusion(
                        'chunk_overlap',
                        'overlapping chunk provenance is ambiguous',
                        representation_id=left.representation_id,
                    ),
                    _exclusion(
                        'chunk_overlap',
                        'overlapping chunk provenance is ambiguous',
                        representation_id=right.representation_id,
                    ),
                ))

    covered_by_chunk: set[tuple[Any, ...]] = set()
    selected_chunks: list[ContextRepresentation] = []
    for index, (representation, member_keys) in enumerate(valid_chunks):
        if index in conflicting:
            continue
        if member_keys & recent_raw_keys:
            exclusions.append(_exclusion(
                'recent_raw_priority',
                'chunk excluded so the recent raw suffix remains raw',
                representation_id=representation.representation_id,
            ))
            continue
        selected_chunks.append(representation)
        covered_by_chunk.update(member_keys)

    selected_raw_keys = raw_keys - covered_by_chunk
    raw_selected: list[ContextRepresentation] = []
    current: list[SourceMember] = []
    current_is_recent: bool | None = None
    for member in expected:
        key = _member_key(member)
        if key not in selected_raw_keys:
            if current:
                raw_selected.append(_raw_representation(current))
                current = []
                current_is_recent = None
            if key in covered_by_chunk and key in raw_keys:
                exclusions.append(_exclusion(
                    'covered_by_chunk',
                    'raw member excluded because a validated chunk covers the exact membership',
                    source_ref=member.source_ref,
                ))
            continue
        is_recent = key in recent_raw_keys
        if current and current_is_recent != is_recent:
            raw_selected.append(_raw_representation(current))
            current = []
        current_is_recent = is_recent
        current.append(member)
    if current:
        raw_selected.append(_raw_representation(current))

    selected = sorted(
        selected_chunks + raw_selected,
        key=lambda representation: (
            representation.source_seqs[0] if representation.source_seqs else 0,
            representation.source_seqs[-1] if representation.source_seqs else 0,
            representation.kind,
            representation.representation_id,
        ),
    )
    coverage_selected = tuple(selected)
    selected_keys = {
        _member_key(member)
        for representation in coverage_selected
        for member in representation.source_members
    }
    gaps = [
        _exclusion(
            'coverage_gap',
            'required source membership is not represented',
            source_ref=member.source_ref,
        )
        for member in expected
        if _member_key(member) not in selected_keys
    ]

    selected, budget_status = _apply_budget(
        coverage_selected,
        budget_policy=budget_policy,
        recent_raw_keys=recent_raw_keys,
        exclusions=exclusions,
    )
    selected = tuple(sorted(
        selected,
        key=lambda representation: (
            representation.source_seqs[0] if representation.source_seqs else 0,
            representation.source_seqs[-1] if representation.source_seqs else 0,
            representation.kind,
            representation.representation_id,
        ),
    ))
    ordered_sections = _build_ordered_sections(
        accepted_fixed_sections,
        selected,
        recent_raw_keys,
    )
    identity = _identity_payload(
        expected,
        selected,
        exclusions,
        gaps,
        ordered_sections=ordered_sections,
        budget_policy=budget_policy,
        budget_status=budget_status,
        recent_raw_source_seqs=tuple(
            sorted(int(member.seq) for member in expected if _member_key(member) in recent_raw_keys)
        ),
    )
    digest = _sha256(identity)
    return ContextPlan(
        plan_id=f'plan:{digest[:32]}',
        plan_hash=digest,
        source_hash=source_hash(expected),
        source_members=expected,
        representations=tuple(selected),
        ordered_sections=ordered_sections,
        exclusions=tuple(sorted(
            exclusions,
            key=lambda item: (item.code, item.source_ref, item.representation_id or '', item.detail),
        )),
        gaps=tuple(sorted(gaps, key=lambda item: (item.source_ref, item.code))),
        budget_policy=budget_policy,
        budget_status=budget_status,
        recent_raw_source_seqs=tuple(
            sorted(int(member.seq) for member in expected if _member_key(member) in recent_raw_keys)
        ),
    )


__all__ = [
    'ContextBudgetPolicy',
    'ContextChunkBinding',
    'ContextPlan',
    'ContextPlanExclusion',
    'ContextRepresentation',
    'ContextSection',
    'SectionKind',
    'build_context_plan',
]


