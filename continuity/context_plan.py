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
class ContextPlanExclusion:
    code: str
    source_ref: str
    detail: str
    representation_id: str | None = None

    @property
    def reason(self) -> str:
        return self.code


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
    exclusions: tuple[ContextPlanExclusion, ...]
    gaps: tuple[ContextPlanExclusion, ...]
    measurement_semantics: str = MEASUREMENT_SEMANTICS

    @property
    def valid(self) -> bool:
        return not self.gaps

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


def _identity_payload(
    expected: Sequence[SourceMember],
    selected: Sequence[ContextRepresentation],
    exclusions: Sequence[ContextPlanExclusion],
    gaps: Sequence[ContextPlanExclusion],
) -> dict[str, Any]:
    return {
        'measurement_semantics': MEASUREMENT_SEMANTICS,
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


def build_context_plan(
    expected_members: Sequence[SourceMember],
    *,
    raw_members: Sequence[SourceMember] | None = None,
    chunks: Sequence[ContextChunkBinding] = (),
) -> ContextPlan:
    """Select exact raw/chunk representations for one required source set."""
    expected = _ordered(expected_members)
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
        selected_chunks.append(representation)
        covered_by_chunk.update(member_keys)

    selected_raw_keys = raw_keys - covered_by_chunk
    raw_selected: list[ContextRepresentation] = []
    current: list[SourceMember] = []
    for member in expected:
        key = _member_key(member)
        if key not in selected_raw_keys:
            if current:
                raw_selected.append(_raw_representation(current))
                current = []
            if key in covered_by_chunk and key in raw_keys:
                exclusions.append(_exclusion(
                    'covered_by_chunk',
                    'raw member excluded because a validated chunk covers the exact membership',
                    source_ref=member.source_ref,
                ))
            continue
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
    selected_keys = {
        _member_key(member)
        for representation in selected
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

    identity = _identity_payload(expected, selected, exclusions, gaps)
    digest = _sha256(identity)
    return ContextPlan(
        plan_id=f'plan:{digest[:32]}',
        plan_hash=digest,
        source_hash=source_hash(expected),
        source_members=expected,
        representations=tuple(selected),
        exclusions=tuple(sorted(
            exclusions,
            key=lambda item: (item.code, item.source_ref, item.representation_id or '', item.detail),
        )),
        gaps=tuple(sorted(gaps, key=lambda item: (item.source_ref, item.code))),
    )


__all__ = [
    'ContextChunkBinding',
    'ContextPlan',
    'ContextPlanExclusion',
    'ContextRepresentation',
    'build_context_plan',
]

