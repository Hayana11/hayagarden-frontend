"""DayBlock → ContextPlan substitution for the current window.

A READY DayBlock replaces every ContinuityChunk of the same natural day in a
ContextPlan, but only when the DayBlock's frozen source snapshot covers that
day's expected window members exactly (same source_ref, revision and content
hash).  Anything less keeps the day's chunks, so a missing, stale or refused
DayBlock can never lose memory.

The DayBlock is presented to ``build_context_plan`` as an ordinary chunk
binding whose snapshot/candidate are built from the window's own expected
members, so every existing chunk validation still runs on it.  Recent days
(``keep_chunks_from_day`` and later) always stay on chunks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from continuity.context_plan import ContextChunkBinding, _stable_member_identity
from continuity.contracts import (
    ContinuityChunk,
    SourceMember,
    SourceSnapshot,
    candidate_source_revision,
)
from continuity.coverage import source_hash
from continuity.sealing import CandidateBlock, MEASUREMENT_SEMANTICS
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

DEFAULT_DAYBLOCK_STORE_PATH = Path('/var/lib/hayagarden/dayblock.db')
DAYBLOCK_STORE_ENV = 'HAYAGARDEN_DAYBLOCK_STORE'
DAYBLOCK_CONTEXT_POLICY_VERSION = 'dayblock_context_r1'
DAYBLOCK_CHUNK_PREFIX = 'dayblock-'

# Kinds that the window's ContextPlan also enumerates.  DayBlock-only
# auxiliary members (attachment spans, incomplete user turns, visible
# partial turns) have no window counterpart and are allowed to be extra.
_WINDOW_MEMBER_KINDS = frozenset({'completed_turn', 'autonomous_event'})

# A real DayBlock is ~1,000+ chars; the 2026-10-13 refusal was 303.
_MIN_BODY_CHARS = 500
_MIN_RAW_TOKENS_FOR_LENGTH_GATE = 5000
_REFUSAL_PATTERNS = tuple(re.compile(p) for p in (
    r'不再继续写',
    r'(?:停在这里|就写到这里).{0,12}不再',
    r'不涉及亲密内容',
    r'(?:我)?(?:无法|不能|没法)(?:继续)?(?:写|完成|生成)(?:这篇|这份|这段)?(?:日记|日块|内容)',
    r"\bI (?:can(?:no|')t|won't|am unable to) (?:write|continue|help)",
))


def dayblock_store_path() -> Path:
    raw = str(os.environ.get(DAYBLOCK_STORE_ENV) or '').strip()
    return Path(raw) if raw else DEFAULT_DAYBLOCK_STORE_PATH


def dayblock_refusal_reason(body: str, raw_token_estimate: int) -> str | None:
    """Why a READY DayBlock body must not enter context, or None."""
    text = str(body or '').strip()
    if not text:
        return 'empty_body'
    for pattern in _REFUSAL_PATTERNS:
        if pattern.search(text):
            return 'refusal_text'
    if int(raw_token_estimate or 0) >= _MIN_RAW_TOKENS_FOR_LENGTH_GATE and len(text) < _MIN_BODY_CHARS:
        return 'body_too_short'
    return None


@dataclass(frozen=True)
class DayBlockArtifact:
    source_day: str
    generation_id: str
    generation_job_id: str
    body: str
    body_hash: str
    raw_token_estimate: int
    output_token_estimate: int
    provider: str
    model_identity: str
    actual_executor: str
    generator_policy_version: str
    prompt_policy_version: str
    prompt_hash: str
    created_at: str
    members: tuple[SourceMember, ...]

    @property
    def chunk_id(self) -> str:
        return f'{DAYBLOCK_CHUNK_PREFIX}{self.source_day}-{self.generation_id[:12]}'


def load_ready_dayblocks(
    path: str | Path | None = None,
) -> tuple[tuple[DayBlockArtifact, ...], tuple[dict[str, str], ...]]:
    """Read READY DayBlocks read-only; returns (usable, rejected)."""
    target = Path(path) if path is not None else dayblock_store_path()
    if not target.is_file():
        return (), ()
    conn = sqlite3.connect(f'file:{target}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    usable: list[DayBlockArtifact] = []
    rejected: list[dict[str, str]] = []
    try:
        receipts = conn.execute(
            "SELECT r.*, j.snapshot_id AS job_snapshot_id FROM dayblock_generation_receipts r "
            "JOIN dayblock_shadow_jobs j ON j.job_id = r.generation_job_id "
            "WHERE r.status = 'ready' ORDER BY r.source_day"
        ).fetchall()
        for row in receipts:
            day = str(row['source_day'])
            body = str(row['summary_body'] or '')
            if hashlib.sha256(body.encode('utf-8')).hexdigest() != str(row['body_hash'] or ''):
                rejected.append({'source_day': day, 'reason': 'body_hash_mismatch'})
                continue
            refusal = dayblock_refusal_reason(body, int(row['raw_token_estimate'] or 0))
            if refusal:
                rejected.append({'source_day': day, 'reason': refusal})
                continue
            members = tuple(
                SourceMember(
                    seq=int(m['seq']),
                    source_kind=str(m['source_kind']),
                    source_ref=str(m['source_ref']),
                    source_revision=str(m['source_revision']),
                    role=str(m['role']),
                    content_hash=str(m['content_hash']),
                    logical_size=int(m['logical_size'] or 0),
                    created_at=str(m['created_at'] or ''),
                    branch_id=str(m['branch_id'] or 'active-transcript'),
                )
                for m in conn.execute(
                    'SELECT * FROM dayblock_source_members WHERE snapshot_id=? ORDER BY seq',
                    (str(row['job_snapshot_id']),),
                )
            )
            if not members:
                rejected.append({'source_day': day, 'reason': 'snapshot_members_missing'})
                continue
            usable.append(DayBlockArtifact(
                source_day=day,
                generation_id=str(row['generation_id']),
                generation_job_id=str(row['generation_job_id']),
                body=body,
                body_hash=str(row['body_hash']),
                raw_token_estimate=int(row['raw_token_estimate'] or 0),
                output_token_estimate=int(row['output_token_estimate'] or 0),
                provider=str(row['provider']),
                model_identity=str(row['model_identity']),
                actual_executor=str(row['actual_executor'] or ''),
                generator_policy_version=str(row['generator_policy_version']),
                prompt_policy_version=str(row['prompt_policy_version']),
                prompt_hash=str(row['prompt_hash']),
                created_at=str(row['finished_at'] or row['created_at']),
                members=members,
            ))
    except sqlite3.Error:
        return (), ({'source_day': '', 'reason': 'store_unreadable'},)
    finally:
        conn.close()
    return tuple(usable), tuple(rejected)


def _member_day(member: SourceMember) -> str:
    # Same natural-day rule as continuity.sealing._member_day.
    return str(member.created_at or '')[:10]


def _dayblock_binding(
    dayblock: DayBlockArtifact,
    day_members: tuple[SourceMember, ...],
) -> ContextChunkBinding:
    branch_id = str(day_members[0].branch_id)
    member_hash = source_hash(day_members)
    snapshot_id = f'dayblock-ctx:{dayblock.source_day}:{member_hash[:16]}'
    candidate_id = f'dayblock-cand:{dayblock.source_day}:{dayblock.generation_id[:12]}'
    seqs = tuple(int(member.seq) for member in day_members)
    snapshot = SourceSnapshot(
        snapshot_id=snapshot_id,
        identity_id='fyodor',
        chat_id='default',
        branch_id=branch_id,
        local_day=dayblock.source_day,
        source_watermark=max(seqs),
        policy_version=DAYBLOCK_CONTEXT_POLICY_VERSION,
        source_hash=member_hash,
        status='ready',
        created_at=dayblock.created_at,
        members=day_members,
    )
    candidate = CandidateBlock(
        candidate_id=candidate_id,
        snapshot_id=snapshot_id,
        policy_version=DAYBLOCK_CONTEXT_POLICY_VERSION,
        block_seq=0,
        local_day=dayblock.source_day,
        branch_id=branch_id,
        source_start_seq=min(seqs),
        source_end_seq=max(seqs),
        source_seqs=seqs,
        source_refs=tuple(member.source_ref for member in day_members),
        source_revisions=tuple(member.source_revision for member in day_members),
        logical_size=sum(int(member.logical_size or 0) for member in day_members),
        completed_turn_count=sum(1 for m in day_members if m.source_kind == 'completed_turn'),
        oversize=False,
        close_reason='dayblock',
        source_revision=candidate_source_revision(day_members),
        measurement_semantics=MEASUREMENT_SEMANTICS,
    )
    chunk = ContinuityChunk(
        chunk_id=dayblock.chunk_id,
        generation_job_id=dayblock.generation_job_id,
        candidate_id=candidate_id,
        snapshot_id=snapshot_id,
        artifact_revision=f'dayblock:{dayblock.body_hash[:16]}',
        body=dayblock.body,
        body_hash=dayblock.body_hash,
        source_token_estimate=dayblock.raw_token_estimate,
        output_token_estimate=int(estimate_tokens_heuristic_cjk1_ascii4_v1(dayblock.body)),
        generator_policy_version=dayblock.generator_policy_version,
        prompt_policy_version=dayblock.prompt_policy_version,
        provider=dayblock.provider,
        model_identity=dayblock.model_identity,
        actual_executor=dayblock.actual_executor,
        generation_id=dayblock.generation_id,
        status='ready',
        created_at=dayblock.created_at,
        prompt_hash=dayblock.prompt_hash,
    )
    return ContextChunkBinding(chunk=chunk, candidate=candidate, snapshot=snapshot)


def substitute_dayblock_bindings(
    expected_members: Sequence[SourceMember],
    bindings: Sequence[ContextChunkBinding],
    dayblocks: Sequence[DayBlockArtifact],
    *,
    keep_chunks_from_day: str,
) -> tuple[tuple[ContextChunkBinding, ...], tuple[dict[str, Any], ...]]:
    """Replace each fully covered older day's chunks with its DayBlock.

    Returns (bindings, per-day report).  A day is substituted only when:
    it is older than ``keep_chunks_from_day``; the window has members on it;
    every window member of that day matches a DayBlock member exactly; every
    DayBlock window-kind member is still in the window; and no chunk binding
    straddles that day and another.
    """
    expected = tuple(expected_members)
    by_day: dict[str, list[SourceMember]] = {}
    for member in expected:
        by_day.setdefault(_member_day(member), []).append(member)
    chunk_days: dict[int, set[str]] = {}
    for index, binding in enumerate(bindings):
        seqs = {int(seq) for seq in binding.candidate.source_seqs}
        days = {_member_day(member) for member in binding.snapshot.members if int(member.seq) in seqs}
        days.add(str(binding.candidate.local_day))
        chunk_days[index] = days

    report: list[dict[str, Any]] = []
    replace_days: dict[str, ContextChunkBinding] = {}
    for dayblock in sorted(dayblocks, key=lambda item: item.source_day):
        day = dayblock.source_day
        entry: dict[str, Any] = {'source_day': day, 'chunk_id': dayblock.chunk_id}
        day_members = tuple(sorted(by_day.get(day, ()), key=lambda m: int(m.seq)))
        if day >= str(keep_chunks_from_day):
            entry['status'] = 'recent_day_keeps_chunks'
        elif not day_members:
            entry['status'] = 'not_in_window'
        else:
            window_ids = {_stable_member_identity(m) for m in day_members}
            dayblock_ids = {_stable_member_identity(m) for m in dayblock.members}
            dayblock_window_ids = {
                _stable_member_identity(m) for m in dayblock.members
                if str(m.source_kind) in _WINDOW_MEMBER_KINDS
            }
            straddling = [
                index for index, days in chunk_days.items()
                if day in days and len(days) > 1
            ]
            if not window_ids <= dayblock_ids:
                entry['status'] = 'stale_window_member_not_covered'
                entry['uncovered'] = len(window_ids - dayblock_ids)
            elif not dayblock_window_ids <= window_ids:
                entry['status'] = 'stale_dayblock_member_not_in_window'
                entry['extra'] = len(dayblock_window_ids - window_ids)
            elif straddling:
                entry['status'] = 'chunk_straddles_day'
            elif len({str(m.branch_id) for m in day_members}) != 1:
                entry['status'] = 'branch_ambiguous'
            else:
                replace_days[day] = _dayblock_binding(dayblock, day_members)
                entry['status'] = 'substituted'
        report.append(entry)

    kept: list[ContextChunkBinding] = []
    replaced_tokens: dict[str, int] = {}
    replaced_counts: dict[str, int] = {}
    for index, binding in enumerate(bindings):
        days = chunk_days[index]
        day = next(iter(days)) if len(days) == 1 else None
        if day is not None and day in replace_days:
            replaced_counts[day] = replaced_counts.get(day, 0) + 1
            replaced_tokens[day] = replaced_tokens.get(day, 0) + int(
                estimate_tokens_heuristic_cjk1_ascii4_v1(binding.chunk.body)
            )
            continue
        kept.append(binding)
    for entry in report:
        day = entry['source_day']
        if entry.get('status') == 'substituted':
            entry['chunks_replaced'] = replaced_counts.get(day, 0)
            entry['chunk_tokens'] = replaced_tokens.get(day, 0)
            entry['dayblock_tokens'] = int(estimate_tokens_heuristic_cjk1_ascii4_v1(
                replace_days[day].chunk.body
            ))
    ordered = kept + [replace_days[day] for day in sorted(replace_days)]
    return tuple(ordered), tuple(report)


_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _day_entry(conn: sqlite3.Connection, day: str, *, with_body: bool) -> dict[str, Any] | None:
    """One page-facing DayBlock state for ``day``.

    A day may hold several generation attempts. The page shows its single ready
    attempt; otherwise the latest attempt; with no attempt, the latest job.
    """
    receipt = conn.execute(
        'SELECT r.*, j.snapshot_id AS job_snapshot_id FROM dayblock_generation_receipts r '
        'JOIN dayblock_shadow_jobs j ON j.job_id = r.generation_job_id WHERE r.source_day=? '
        # rowid follows insertion order and exists before and after the attempt
        # migration, so a read-only page works against an unmigrated store too.
        "ORDER BY r.status = 'ready' DESC, r.created_at DESC, r.rowid DESC LIMIT 1",
        (day,),
    ).fetchone()
    job = conn.execute(
        'SELECT * FROM dayblock_shadow_jobs WHERE source_day=? ORDER BY created_at DESC, job_id DESC LIMIT 1',
        (day,),
    ).fetchone()
    if receipt is None and job is None:
        return None
    snapshot_id = str(receipt['job_snapshot_id'] if receipt is not None else job['snapshot_id'])
    snapshot = conn.execute(
        'SELECT completed_turn_count FROM dayblock_source_snapshots WHERE snapshot_id=?',
        (snapshot_id,),
    ).fetchone()
    body = ''
    reason: str | None = None
    if receipt is not None:
        status = str(receipt['status'])
        body = str(receipt['summary_body'] or '')
        if status == 'ready':
            if hashlib.sha256(body.encode('utf-8')).hexdigest() != str(receipt['body_hash'] or ''):
                status, reason = 'rejected', 'body_hash_mismatch'
            else:
                reason = dayblock_refusal_reason(body, int(receipt['raw_token_estimate'] or 0))
                if reason:
                    status = 'rejected'
        elif status == 'failed':
            reason = str(receipt['error_code'] or '') or None
        created_at = str(receipt['finished_at'] or receipt['created_at'] or '')
        raw_tokens = int(receipt['raw_token_estimate'] or 0)
        output_tokens = int(receipt['output_token_estimate'] or 0) if body else 0
        model_identity = str(receipt['model_identity'] or '')
    else:
        status = 'blocked' if str(job['status']) in ('blocked', 'planned') else str(job['status'])
        try:
            reasons = json.loads(str(job['blocking_reasons_json'] or '[]'))
        except ValueError:
            reasons = []
        reason = ','.join(str(item) for item in reasons)[:200] or None
        created_at = str(job['created_at'] or '')
        raw_tokens = 0
        output_tokens = 0
        model_identity = str(job['model_identity'] or '')
    entry: dict[str, Any] = {
        'source_day': day,
        'status': status,
        'reason': reason,
        'completed_turn_count': int(snapshot['completed_turn_count']) if snapshot is not None else None,
        'output_token_estimate': output_tokens if body else None,
        'body_char_count': len(body) if body else None,
        'raw_token_estimate': raw_tokens or None,
        'created_at': created_at or None,
        'model_identity': model_identity or None,
    }
    if with_body:
        entry['body'] = body or None
    return entry


def _open_dayblock_store(path: str | Path | None) -> sqlite3.Connection | None:
    target = Path(path) if path is not None else dayblock_store_path()
    if not target.is_file():
        return None
    conn = sqlite3.connect(f'file:{target}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def list_dayblock_days(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read-only per-day DayBlock states for the context-compression page."""
    conn = _open_dayblock_store(path)
    if conn is None:
        return []
    try:
        days = sorted({
            str(row[0]) for row in conn.execute(
                'SELECT source_day FROM dayblock_shadow_jobs UNION SELECT source_day FROM dayblock_generation_receipts'
            )
        })
        return [entry for day in days if (entry := _day_entry(conn, day, with_body=False))]
    finally:
        conn.close()


def get_dayblock_day(path: str | Path | None, day: str) -> dict[str, Any] | None:
    """Read-only DayBlock detail (with body) for one source day, or None."""
    if not _DAY_RE.match(str(day or '')):
        return None
    conn = _open_dayblock_store(path)
    if conn is None:
        return None
    try:
        return _day_entry(conn, str(day), with_body=True)
    finally:
        conn.close()


__all__ = [
    'DAYBLOCK_CHUNK_PREFIX',
    'DAYBLOCK_CONTEXT_POLICY_VERSION',
    'DEFAULT_DAYBLOCK_STORE_PATH',
    'DayBlockArtifact',
    'dayblock_refusal_reason',
    'dayblock_store_path',
    'get_dayblock_day',
    'list_dayblock_days',
    'load_ready_dayblocks',
    'substitute_dayblock_bindings',
]
