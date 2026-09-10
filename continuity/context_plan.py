"""Pure ContextPlan selection for the R4 representation contract.

This module deliberately stops at representation selection.  It does not know
about providers, resident sessions, gateway payloads, budgets, or persistence.
Raw source members remain the authoritative evidence; a ready R3 chunk may
replace the exact members it proves, but it can never add a second copy of
those members to the same plan.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from continuity.contracts import SourceMember, SourceSnapshot, candidate_source_revision
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock, MEASUREMENT_SEMANTICS
from continuity.contracts import ContinuityChunk
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1


RepresentationKind = Literal['raw', 'chunk']


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _member_key(member: SourceMember) -> tuple[Any, ...]:
    """Return the exact R1 membership identity, including attachment spans."""
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


def _sorted_members(members: Iterable[SourceMember]) -> tuple[SourceMember, ...]:
    return tuple(sorted(tuple(members), key=lambda item: int(item.seq)))


def _as_items(value: Iterable[Any] | Any | None) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)):
        return (value,)
    if isinstance(value, (ContinuityChunk, CandidateBlock, SourceSnapshot, SourceMember)):
        return (value,)
    if hasattr(value, 'chunk') and hasattr(value, 'candidate'):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


@dataclass(frozen=True)
class ContextChunkBinding:
    """R3 chunk plus the immutable candidate and snapshot that prove it."""

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
    """One exact representation candidate or selected representation."""

    representation_id: str
    kind: RepresentationKind
    source_members: tuple[SourceMember, ...]
    source_refs: tuple[str, ...]
    source_revisions: tuple[str, ...]
    source_hash: str
    estimated_tokens: int
    selected: bool = True
    excluded_reason: str | None = None
    chunk_id: str | None = None
    candidate_id: str | None = None
    snapshot_id: str | None = None
    provenance: tuple[tuple[str, str], ...] = ()

    @property
    def source_seqs(self) -> tuple[int, ...]:
        return tuple(int(member.seq) for member in self.source_members)

    @property
    def source_start_seq(self) -> int | None:
        return min(self.source_seqs) if self.source_seqs else None

    @property
    def source_end_seq(self) -> int | None:
        return max(self.source_seqs) + 1 if self.source_seqs else None

    @property
    def source_range(self) -> tuple[int | None, int | None]:
        return self.source_start_seq, self.source_end_seq

    @property
    def exact_members(self) -> tuple[SourceMember, ...]:
        return self.source_members

    @property
    def provenance_map(self) -> dict[str, str]:
        return dict(self.provenance)


@dataclass(frozen=True)
class ContextPlan:
    """Deterministic representation plan; no provider payload is produced."""

    plan_id: str
    plan_hash: str
    source_hash: str
    source_members: tuple[SourceMember, ...]
    representations: tuple[ContextRepresentation, ...]
    exclusions: tuple[ContextPlanExclusion, ...]
    gaps: tuple[ContextPlanExclusion, ...]
    measurement_semantics: str = MEASUREMENT_SEMANTICS

    @property
    def selected_representations(self) -> tuple[ContextRepresentation, ...]:
        return self.representations

    @property
    def ordered_representations(self) -> tuple[ContextRepresentation, ...]:
        return self.representations

    @property
    def rejections(self) -> tuple[ContextPlanExclusion, ...]:
        return self.exclusions

    @property
    def coverage_gaps(self) -> tuple[ContextPlanExclusion, ...]:
        return self.gaps

    @property
    def valid(self) -> bool:
        return not self.gaps

    @property
    def covered_source_members(self) -> tuple[SourceMember, ...]:
        members: list[SourceMember] = []
        for representation in self.representations:
            members.extend(representation.source_members)
        return tuple(sorted(members, key=lambda item: int(item.seq)))

    @property
    def covered_source_refs(self) -> tuple[str, ...]:
        return tuple(member.source_ref for member in self.covered_source_members)


@dataclass(frozen=True)
class _ChunkCandidate:
    binding: ContextChunkBinding | None
    chunk: Any
    candidate: Any
    snapshot: Any


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _normalize_chunk(item: Any, plan_snapshot: SourceSnapshot | None) -> _ChunkCandidate:
    if isinstance(item, ContextChunkBinding):
        return _ChunkCandidate(item, item.chunk, item.candidate, item.snapshot)
    if isinstance(item, (tuple, list)):
        if len(item) == 3:
            return _ChunkCandidate(None, item[0], item[1], item[2])
        if len(item) == 2 and plan_snapshot is not None:
            return _ChunkCandidate(None, item[0], item[1], plan_snapshot)
    chunk = _get(item, 'chunk')
    candidate = _get(item, 'candidate')
    snapshot = _get(item, 'snapshot')
    if chunk is not None and candidate is not None:
        return _ChunkCandidate(None, chunk, candidate, snapshot or plan_snapshot)
    return _ChunkCandidate(None, item, _get(item, 'candidate'), snapshot or plan_snapshot)


def _source_members_for_candidate(
    candidate: Any,
    snapshot: SourceSnapshot | None,
    by_seq: Mapping[int, SourceMember],
) -> tuple[SourceMember, ...]:
    if candidate is None:
        raise ValueError('chunk candidate membership is missing')
    seqs = tuple(int(item) for item in (_get(candidate, 'source_seqs') or ()))
    refs = tuple(str(item) for item in (_get(candidate, 'source_refs') or ()))
    revisions = tuple(str(item) for item in (_get(candidate, 'source_revisions') or ()))
    if not (len(seqs) == len(refs) == len(revisions)) or not seqs:
        raise ValueError('chunk candidate membership is incomplete')
    members: list[SourceMember] = []
    snapshot_by_seq = {
        int(member.seq): member for member in tuple(_get(snapshot, 'members') or ())
    }
    lookup = snapshot_by_seq or dict(by_seq)
    for seq, ref, revision in zip(seqs, refs, revisions):
        member = lookup.get(seq)
        if member is None:
            raise ValueError(f'chunk source member seq {seq} is missing')
        if member.source_ref != ref or member.source_revision != revision:
            raise ValueError(f'chunk source membership mismatch for {ref}')
        members.append(member)
    return tuple(members)


def _source_member_overlap(left: SourceMember, right: SourceMember) -> bool:
    if left.source_ref != right.source_ref:
        return False
    if left.source_revision != right.source_revision:
        return True
    if left.span_start is not None and right.span_start is not None:
        return int(left.span_start) < int(right.span_end) and int(right.span_start) < int(left.span_end)
    return True


def _raw_representation(members: Sequence[SourceMember]) -> ContextRepresentation:
    ordered = tuple(members)
    digest = _sha256({'kind': 'raw', 'members': [_member_identity(item) for item in ordered]})
    member_hash = source_hash(ordered)
    return ContextRepresentation(
        representation_id=f'raw:{digest[:32]}',
        kind='raw',
        source_members=ordered,
        source_refs=tuple(item.source_ref for item in ordered),
        source_revisions=tuple(item.source_revision for item in ordered),
        source_hash=member_hash,
        estimated_tokens=sum(max(0, int(item.logical_size)) for item in ordered),
        provenance=(
            ('measurement_semantics', MEASUREMENT_SEMANTICS),
            ('source_hash', member_hash),
        ),
    )


def _chunk_representation(
    chunk: Any,
    candidate: Any,
    snapshot: Any,
    members: Sequence[SourceMember],
) -> ContextRepresentation:
    ordered = tuple(members)
    chunk_id = str(_get(chunk, 'chunk_id') or '')
    candidate_id = str(_get(candidate, 'candidate_id') or '')
    snapshot_id = str(_get(snapshot, 'snapshot_id') or '')
    body = _get(chunk, 'body')
    body_hash = str(_get(chunk, 'body_hash') or '')
    if isinstance(body, str) and body:
        estimated_tokens = int(estimate_tokens_heuristic_cjk1_ascii4_v1(body))
    else:
        estimated_tokens = int(_get(chunk, 'output_token_estimate') or 0)
    member_hash = source_hash(ordered)
    artifact_revision = str(_get(chunk, 'artifact_revision') or '')
    provenance = tuple(sorted({
        'measurement_semantics': MEASUREMENT_SEMANTICS,
        'source_hash': member_hash,
        'chunk_id': chunk_id,
        'candidate_id': candidate_id,
        'snapshot_id': snapshot_id,
        'artifact_revision': artifact_revision,
        'body_hash': body_hash,
    }.items()))
    return ContextRepresentation(
        representation_id=f'chunk:{chunk_id}',
        kind='chunk',
        source_members=ordered,
        source_refs=tuple(item.source_ref for item in ordered),
        source_revisions=tuple(item.source_revision for item in ordered),
        source_hash=member_hash,
        estimated_tokens=max(0, estimated_tokens),
        chunk_id=chunk_id,
        candidate_id=candidate_id,
        snapshot_id=snapshot_id,
        provenance=provenance,
    )


def _exclusion(code: str, detail: str, *, source_ref: str = '', representation_id: str | None = None) -> ContextPlanExclusion:
    return ContextPlanExclusion(
        code=code,
        source_ref=source_ref,
        detail=detail,
        representation_id=representation_id,
    )


def _identity_payload(
    expected: Sequence[SourceMember],
    selected: Sequence[ContextRepresentation],
    exclusions: Sequence[ContextPlanExclusion],
    gaps: Sequence[ContextPlanExclusion],
) -> dict[str, Any]:
    return {
        'measurement_semantics': MEASUREMENT_SEMANTICS,
        'source_members': [_member_identity(item) for item in expected],
        'representations': [
            {
                'kind': representation.kind,
                'source_members': [_member_identity(item) for item in representation.source_members],
                'source_hash': representation.source_hash,
                'estimated_tokens': int(representation.estimated_tokens),
                'chunk_id': representation.chunk_id,
                'candidate_id': representation.candidate_id,
                'snapshot_id': representation.snapshot_id,
                'provenance': list(representation.provenance),
            }
            for representation in selected
        ],
        'exclusions': sorted((item.code, item.source_ref, item.detail, item.representation_id) for item in exclusions),
        'gaps': sorted((item.code, item.source_ref, item.detail) for item in gaps),
    }


def build_context_plan(
    source_members: Iterable[SourceMember] | SourceSnapshot,
    *,
    chunks: Iterable[Any] | Any | None = None,
    ready_chunks: Iterable[Any] | Any | None = None,
    raw_members: Iterable[SourceMember] | None = None,
    expected_source_members: Iterable[SourceMember] | None = None,
    snapshot: SourceSnapshot | None = None,
) -> ContextPlan:
    """Build a deterministic raw-or-chunk representation plan.

    ``source_members`` is the required exact source membership.  ``raw_members``
    can intentionally be a subset for replay/tests that need to expose a
    coverage gap; in normal use it defaults to the complete source set.
    Ready chunks must be supplied with their R3 ``CandidateBlock`` and
    ``SourceSnapshot`` (a ``ContextChunkBinding`` or a three-tuple).  Without
    that provenance the chunk is rejected rather than guessed into the plan.
    """
    if isinstance(source_members, SourceSnapshot):
        if snapshot is not None and snapshot.snapshot_id != source_members.snapshot_id:
            raise ValueError('conflicting source snapshots')
        snapshot = source_members
        expected = _sorted_members(source_members.members)
    else:
        expected_input = expected_source_members if expected_source_members is not None else source_members
        expected = _sorted_members(expected_input)
    if not expected:
        expected = ()
    if snapshot is not None and tuple(snapshot.members):
        snapshot_members = _sorted_members(snapshot.members)
        if tuple(_member_key(item) for item in snapshot_members) != tuple(_member_key(item) for item in expected):
            raise ValueError('snapshot membership does not match expected source membership')
    raw = _sorted_members(raw_members if raw_members is not None else expected)
    by_key = {_member_key(member): member for member in expected}
    by_seq = {int(member.seq): member for member in expected}
    exclusions: list[ContextPlanExclusion] = []
    raw_keys: set[tuple[Any, ...]] = set()
    for member in raw:
        key = _member_key(member)
        if key not in by_key:
            exclusions.append(_exclusion('unexpected_raw', 'raw member is outside expected source coverage', source_ref=member.source_ref))
            continue
        if key in raw_keys:
            exclusions.append(_exclusion('duplicate_raw', 'raw member appears more than once', source_ref=member.source_ref))
            continue
        raw_keys.add(key)

    normalized_chunks = _as_items(chunks) + _as_items(ready_chunks)
    valid_chunks: list[tuple[ContextRepresentation, set[tuple[Any, ...]]]] = []
    for item in normalized_chunks:
        candidate_item = _normalize_chunk(item, snapshot)
        chunk = candidate_item.chunk
        candidate = candidate_item.candidate
        chunk_id = str(_get(chunk, 'chunk_id') or '')
        representation_id = f'chunk:{chunk_id}'
        try:
            if str(_get(chunk, 'status') or '') != 'ready':
                raise ValueError('chunk is not ready')
            bound_snapshot = candidate_item.snapshot
            if bound_snapshot is None:
                raise ValueError('chunk snapshot provenance is missing')
            if snapshot is not None and str(_get(bound_snapshot, 'snapshot_id') or '') != snapshot.snapshot_id:
                raise ValueError('chunk snapshot identity mismatch')
            if str(_get(chunk, 'snapshot_id') or '') != str(_get(bound_snapshot, 'snapshot_id') or ''):
                raise ValueError('chunk snapshot identity mismatch')
            if str(_get(candidate, 'snapshot_id') or '') != str(_get(bound_snapshot, 'snapshot_id') or ''):
                raise ValueError('candidate snapshot identity mismatch')
            if str(_get(chunk, 'candidate_id') or '') != str(_get(candidate, 'candidate_id') or ''):
                raise ValueError('chunk candidate identity mismatch')
            snapshot_members = tuple(_get(bound_snapshot, 'members') or ())
            if source_hash(snapshot_members) != str(_get(bound_snapshot, 'source_hash') or ''):
                raise ValueError('chunk snapshot source hash mismatch')
            members = _source_members_for_candidate(candidate, bound_snapshot, by_seq)
            member_keys = {_member_key(member) for member in members}
            if not member_keys.issubset(set(by_key)):
                raise ValueError('chunk membership is outside expected source coverage')
            candidate_revision = str(_get(candidate, 'source_revision') or '')
            if candidate_revision != candidate_source_revision(members):
                raise ValueError('chunk candidate source revision is stale')
            candidate_branch = str(_get(candidate, 'branch_id') or '')
            if any(str(member.branch_id or '') != candidate_branch for member in members):
                raise ValueError('chunk branch identity mismatch')
            body = _get(chunk, 'body')
            body_hash = str(_get(chunk, 'body_hash') or '')
            if not isinstance(body, str) or not body.strip():
                raise ValueError('chunk body is missing')
            if not body_hash:
                raise ValueError('chunk body hash is missing')
            actual_body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
            if actual_body_hash != body_hash:
                raise ValueError('chunk body hash mismatch')
            representation = _chunk_representation(chunk, candidate, bound_snapshot, members)
            if not representation.chunk_id:
                raise ValueError('chunk identity is missing')
            valid_chunks.append((representation, member_keys))
        except (TypeError, ValueError, KeyError) as exc:
            exclusions.append(_exclusion('chunk_rejected', str(exc), representation_id=representation_id))

    conflicting: set[int] = set()
    for left_index, (left, left_keys) in enumerate(valid_chunks):
        for right_index in range(left_index + 1, len(valid_chunks)):
            right, right_keys = valid_chunks[right_index]
            overlap = bool(left_keys & right_keys)
            if not overlap:
                for left_member in left.source_members:
                    if any(_source_member_overlap(left_member, right_member) for right_member in right.source_members):
                        overlap = True
                        break
            if overlap:
                conflicting.update((left_index, right_index))
                exclusions.append(_exclusion('chunk_overlap', 'overlapping chunk provenance is ambiguous', representation_id=left.representation_id))
                exclusions.append(_exclusion('chunk_overlap', 'overlapping chunk provenance is ambiguous', representation_id=right.representation_id))

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
                exclusions.append(_exclusion('covered_by_chunk', 'raw member excluded because a validated chunk covers the exact membership', source_ref=member.source_ref))
            continue
        current.append(member)
    if current:
        raw_selected.append(_raw_representation(current))

    selected = sorted(
        selected_chunks + raw_selected,
        key=lambda representation: (
            representation.source_start_seq if representation.source_start_seq is not None else 0,
            representation.source_end_seq if representation.source_end_seq is not None else 0,
            representation.kind,
            representation.representation_id,
        ),
    )

    selected_keys: set[tuple[Any, ...]] = set()
    for representation in selected:
        selected_keys.update(_member_key(member) for member in representation.source_members)
    gaps: list[ContextPlanExclusion] = []
    for member in expected:
        if _member_key(member) not in selected_keys:
            gaps.append(_exclusion('coverage_gap', 'required source membership is not represented', source_ref=member.source_ref))

    identity = _identity_payload(expected, selected, exclusions, gaps)
    digest = _sha256(identity)
    return ContextPlan(
        plan_id=f'plan:{digest[:32]}',
        plan_hash=digest,
        source_hash=source_hash(expected),
        source_members=expected,
        representations=tuple(selected),
        exclusions=tuple(sorted(exclusions, key=lambda item: (item.code, item.source_ref, item.representation_id or '', item.detail))),
        gaps=tuple(sorted(gaps, key=lambda item: (item.source_ref, item.detail))),
    )


# Explicit aliases keep the pure core easy to discover without creating a
# second assembler or compatibility service.
assemble_context_plan = build_context_plan
build_plan = build_context_plan
select_representations = build_context_plan


__all__ = [
    'ContextChunkBinding',
    'ContextPlan',
    'ContextPlanExclusion',
    'ContextRepresentation',
    'MEASUREMENT_SEMANTICS',
    'assemble_context_plan',
    'build_context_plan',
    'build_plan',
    'select_representations',
]

