"""Deterministic candidate block sealing for Continuity Compression R2.

R2 creates only immutable candidate metadata.  It never generates a body,
calls a provider, or chooses a runtime context representation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from chat.day_handoff import chat_day_for_timestamp
from continuity.contracts import SourceMember, SourceSnapshot
from continuity.coverage import CoverageReport, validate_exact_coverage


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class SealingPolicy:
    """Versioned R2 boundary policy; changing a value requires a new version."""

    version: str = 'continuity_sealing_v1_12k_20turns'
    target_logical_size: int = 12_000
    max_completed_turns: int = 20

    def __post_init__(self) -> None:
        if not str(self.version).strip():
            raise ValueError('policy version must be non-empty')
        if int(self.target_logical_size) <= 0:
            raise ValueError('target logical size must be positive')
        if int(self.max_completed_turns) <= 0:
            raise ValueError('max completed turns must be positive')


DEFAULT_SEALING_POLICY = SealingPolicy()


@dataclass(frozen=True)
class CandidateBlock:
    """Content-addressed candidate range with exact source membership."""

    candidate_id: str
    snapshot_id: str
    policy_version: str
    block_seq: int
    local_day: str
    branch_id: str
    source_start_seq: int
    source_end_seq: int
    source_seqs: tuple[int, ...]
    source_refs: tuple[str, ...]
    source_revisions: tuple[str, ...]
    logical_size: int
    completed_turn_count: int
    oversize: bool
    close_reason: str
    source_revision: str


def _member_day(member: SourceMember) -> str:
    raw = str(member.created_at or '').strip()
    try:
        timestamp = dt.datetime.strptime(raw[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError) as exc:
        raise ValueError('source member created_at must be YYYY-MM-DD HH:MM:SS') from exc
    return chat_day_for_timestamp(timestamp)


def _validate_snapshot_members(snapshot: SourceSnapshot) -> None:
    members = tuple(snapshot.members)
    expected = tuple(member.source_ref for member in members)
    report = validate_exact_coverage(members, expected_source_refs=expected)
    if not report.valid:
        codes = ','.join(issue.code for issue in report.issues)
        raise ValueError(f'source snapshot coverage invalid: {codes}')
    if report.source_hash != snapshot.source_hash:
        raise ValueError('source snapshot hash mismatch')
    for member in members:
        if int(member.logical_size) < 0:
            raise ValueError('source member logical_size must be non-negative')
        _member_day(member)


def _candidate(
    snapshot: SourceSnapshot,
    policy: SealingPolicy,
    block_seq: int,
    members: list[SourceMember],
    *,
    close_reason: str,
) -> CandidateBlock:
    if not members:
        raise ValueError('cannot seal an empty candidate block')
    refs = tuple(member.source_ref for member in members)
    revisions = tuple(member.source_revision for member in members)
    seqs = tuple(int(member.seq) for member in members)
    source_revision = _sha256({
        'seqs': seqs,
        'refs': refs,
        'revisions': revisions,
    })
    identity = {
        'snapshot_id': snapshot.snapshot_id,
        'snapshot_source_hash': snapshot.source_hash,
        'policy_version': policy.version,
        'block_seq': int(block_seq),
        'source_start_seq': seqs[0],
        'source_end_seq': seqs[-1] + 1,
        'source_revision': source_revision,
    }
    candidate_id = f"candidate:{_sha256(identity)[:32]}"
    return CandidateBlock(
        candidate_id=candidate_id,
        snapshot_id=snapshot.snapshot_id,
        policy_version=policy.version,
        block_seq=int(block_seq),
        local_day=_member_day(members[0]),
        branch_id=str(members[0].branch_id or snapshot.branch_id),
        source_start_seq=seqs[0],
        source_end_seq=seqs[-1] + 1,
        source_seqs=seqs,
        source_refs=refs,
        source_revisions=revisions,
        logical_size=sum(int(member.logical_size) for member in members),
        completed_turn_count=sum(
            1 for member in members if member.source_kind == 'completed_turn'
        ),
        oversize=any(int(member.logical_size) > int(policy.target_logical_size) for member in members),
        close_reason=close_reason,
        source_revision=source_revision,
    )


def seal_snapshot(
    snapshot: SourceSnapshot,
    policy: SealingPolicy = DEFAULT_SEALING_POLICY,
) -> tuple[CandidateBlock, ...]:
    """Seal one immutable snapshot into deterministic, flat candidate blocks."""
    _validate_snapshot_members(snapshot)
    members = tuple(snapshot.members)
    if not members:
        return ()

    candidates: list[CandidateBlock] = []
    current: list[SourceMember] = []
    current_size = 0
    current_turns = 0
    block_seq = 0

    def flush(reason: str) -> None:
        nonlocal current, current_size, current_turns, block_seq
        if not current:
            return
        candidates.append(_candidate(snapshot, policy, block_seq, current, close_reason=reason))
        block_seq += 1
        current = []
        current_size = 0
        current_turns = 0

    for member in members:
        member_day = _member_day(member)
        member_branch = str(member.branch_id or snapshot.branch_id)
        if current:
            current_day = _member_day(current[0])
            current_branch = str(current[0].branch_id or snapshot.branch_id)
            if member_day != current_day:
                flush('day_boundary')
            elif member_branch != current_branch:
                flush('branch_boundary')

        member_size = int(member.logical_size)
        member_turns = 1 if member.source_kind == 'completed_turn' else 0
        if current and member_size > int(policy.target_logical_size):
            flush('before_oversize')

        current.append(member)
        current_size += member_size
        current_turns += member_turns

        if member_size > int(policy.target_logical_size):
            flush('oversize_single_turn' if member_turns else 'oversize_unit')
        elif current_turns >= int(policy.max_completed_turns):
            flush('max_completed_turns')
        elif current_size >= int(policy.target_logical_size):
            flush('target_logical_size')

    flush('end_of_snapshot')
    return tuple(candidates)


def validate_candidate_coverage(
    snapshot: SourceSnapshot,
    candidates: Iterable[CandidateBlock],
) -> CoverageReport:
    """Validate that candidate membership is an exact partition of the snapshot."""
    ordered = tuple(candidates)
    by_seq = {int(member.seq): member for member in snapshot.members}
    flattened: list[SourceMember] = []
    seen: set[int] = set()
    for candidate in ordered:
        if candidate.snapshot_id != snapshot.snapshot_id:
            raise ValueError('candidate belongs to another source snapshot')
        if candidate.policy_version == '':
            raise ValueError('candidate policy version is required')
        if (
            len(candidate.source_seqs) != len(candidate.source_refs)
            or len(candidate.source_seqs) != len(candidate.source_revisions)
        ):
            raise ValueError('candidate source membership length mismatch')
        for seq, source_ref, source_revision in zip(
            candidate.source_seqs, candidate.source_refs, candidate.source_revisions,
        ):
            if seq in seen:
                raise ValueError('candidate source membership duplicated')
            member = by_seq.get(int(seq))
            if member is None or member.source_ref != source_ref:
                raise ValueError('candidate source membership is not in snapshot')
            if member.source_revision != source_revision:
                raise ValueError('candidate source revision is stale')
            seen.add(int(seq))
            flattened.append(member)
    flattened.sort(key=lambda member: int(member.seq))
    return validate_exact_coverage(
        flattened,
        expected_source_refs=(member.source_ref for member in snapshot.members),
        expected_source_revisions={
            member.source_ref: member.source_revision for member in snapshot.members
        },
    )

