"""Exact-membership validation for Continuity Compression R1."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from continuity.contracts import SourceMember


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class CoverageIssue:
    code: str
    source_ref: str
    detail: str


@dataclass(frozen=True)
class CoverageReport:
    valid: bool
    source_hash: str
    issues: tuple[CoverageIssue, ...]


def source_hash(members: Iterable[SourceMember]) -> str:
    ordered = tuple(members)
    payload = [
        {
            'seq': m.seq,
            'source_kind': m.source_kind,
            'source_ref': m.source_ref,
            'source_revision': m.source_revision,
            'role': m.role,
            'content_hash': m.content_hash,
            'span_start': m.span_start,
            'span_end': m.span_end,
        }
        for m in ordered
    ]
    return _sha256_text(_canonical_json(payload))


def validate_exact_coverage(
    members: Iterable[SourceMember],
    *,
    expected_source_refs: Iterable[str] | None = None,
) -> CoverageReport:
    ordered = tuple(members)
    issues: list[CoverageIssue] = []

    seen_refs: set[str] = set()
    seen_seq: set[int] = set()
    for member in ordered:
        if member.seq in seen_seq:
            issues.append(CoverageIssue(
                code='duplicate_seq', source_ref=member.source_ref,
                detail=f'duplicate member seq {member.seq}',
            ))
        seen_seq.add(member.seq)
        if member.source_ref in seen_refs:
            issues.append(CoverageIssue(
                code='duplicate_source', source_ref=member.source_ref,
                detail='same source_ref appears more than once',
            ))
        seen_refs.add(member.source_ref)
        if member.source_revision != member.content_hash:
            issues.append(CoverageIssue(
                code='revision_hash_mismatch', source_ref=member.source_ref,
                detail='source revision does not match content hash',
            ))
        if (
            member.span_start is not None and member.span_end is not None
            and member.span_end < member.span_start
        ):
            issues.append(CoverageIssue(
                code='invalid_span', source_ref=member.source_ref,
                detail='span_end is before span_start',
            ))

    if ordered:
        expected_seq = set(range(len(ordered)))
        missing_seq = sorted(expected_seq - seen_seq)
        extra_seq = sorted(seen_seq - expected_seq)
        for seq in missing_seq:
            issues.append(CoverageIssue(
                code='sequence_gap', source_ref='', detail=f'missing seq {seq}',
            ))
        for seq in extra_seq:
            issues.append(CoverageIssue(
                code='sequence_out_of_range', source_ref='', detail=f'unexpected seq {seq}',
            ))

    if expected_source_refs is not None:
        expected = set(expected_source_refs)
        actual = {m.source_ref for m in ordered}
        for ref in sorted(expected - actual):
            issues.append(CoverageIssue(
                code='coverage_gap', source_ref=ref,
                detail='expected source is absent from membership',
            ))
        for ref in sorted(actual - expected):
            issues.append(CoverageIssue(
                code='unexpected_source', source_ref=ref,
                detail='membership contains source outside expected coverage',
            ))

    return CoverageReport(
        valid=not issues,
        source_hash=source_hash(ordered),
        issues=tuple(issues),
    )
