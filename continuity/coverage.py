"""Exact-membership validation for Continuity Compression R1."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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
    expected_source_revisions: Mapping[str, str] | None = None,
) -> CoverageReport:
    """Validate exact membership, stale revisions, and half-open span coverage."""
    ordered = tuple(members)
    issues: list[CoverageIssue] = []

    seen_identity: set[tuple] = set()
    unspanned_refs: set[str] = set()
    seen_seq: set[int] = set()
    spans: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)

    for member in ordered:
        if member.seq in seen_seq:
            issues.append(CoverageIssue(
                code='duplicate_seq', source_ref=member.source_ref,
                detail=f'duplicate member seq {member.seq}',
            ))
        seen_seq.add(member.seq)

        identity = (
            member.source_ref,
            member.source_revision,
            member.span_start,
            member.span_end,
        )
        if identity in seen_identity:
            issues.append(CoverageIssue(
                code='duplicate_source', source_ref=member.source_ref,
                detail='same source membership identity appears more than once',
            ))
        seen_identity.add(identity)

        has_start = member.span_start is not None
        has_end = member.span_end is not None
        if has_start != has_end:
            issues.append(CoverageIssue(
                code='invalid_span', source_ref=member.source_ref,
                detail='span_start and span_end must be present together',
            ))
        elif has_start and has_end:
            start = int(member.span_start)
            end = int(member.span_end)
            if start < 0 or end <= start:
                issues.append(CoverageIssue(
                    code='invalid_span', source_ref=member.source_ref,
                    detail='span must be a non-empty half-open range',
                ))
            else:
                spans[(member.source_ref, member.source_revision)].append((start, end))
        else:
            if member.source_ref in unspanned_refs:
                issues.append(CoverageIssue(
                    code='duplicate_source', source_ref=member.source_ref,
                    detail='unspanned source_ref appears more than once',
                ))
            unspanned_refs.add(member.source_ref)

        if expected_source_revisions is not None:
            expected_revision = expected_source_revisions.get(member.source_ref)
            if expected_revision is not None and member.source_revision != expected_revision:
                issues.append(CoverageIssue(
                    code='source_revision_changed', source_ref=member.source_ref,
                    detail='source revision no longer matches frozen membership',
                ))

    for (ref, _revision), ranges in spans.items():
        ranges.sort()
        previous_end: int | None = None
        for start, end in ranges:
            if previous_end is not None:
                if start < previous_end:
                    issues.append(CoverageIssue(
                        code='coverage_overlap', source_ref=ref,
                        detail=f'span {start}:{end} overlaps previous coverage ending at {previous_end}',
                    ))
                elif start > previous_end:
                    issues.append(CoverageIssue(
                        code='span_gap', source_ref=ref,
                        detail=f'gap between {previous_end} and {start}',
                    ))
            previous_end = max(previous_end or end, end)

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
        actual = {member.source_ref for member in ordered}
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
