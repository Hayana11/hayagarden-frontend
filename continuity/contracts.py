"""Frozen data contracts for Continuity Compression R1.

These objects describe source evidence only. They do not generate compression,
select context, mutate runtime state, or write persistence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal


FinalityStatus = Literal['completed', 'incomplete']
SourceKind = Literal['completed_turn', 'autonomous_event', 'attachment_span']


def candidate_source_revision(members: tuple['SourceMember', ...] | list['SourceMember']) -> str:
    """Return the canonical R2 candidate membership revision.

    The identity is defined with the source order, revisions, and stable
    sealing measurements.  Keeping it beside the immutable contracts lets
    read-only consumers validate the same identity without importing a module
    that also discovers/materializes chat rows.
    """
    ordered = tuple(members)
    payload = {
        'seqs': tuple(int(member.seq) for member in ordered),
        'refs': tuple(member.source_ref for member in ordered),
        'revisions': tuple(member.source_revision for member in ordered),
        'measurements': [
            {
                'logical_size': int(member.logical_size),
                'created_at': member.created_at,
                'branch_id': member.branch_id,
            }
            for member in ordered
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class EvidenceRef:
    """Content-addressed reference to one durable source or tool outcome."""

    source_ref: str
    source_revision: str
    content_hash: str
    # R2 measure only; defaults keep the R1 positional contract intact.
    logical_size: int = 0


@dataclass(frozen=True)
class CanonicalTurn:
    """Smallest ordinary conversation unit eligible for compression."""

    turn_id: str
    identity_id: str
    chat_id: str
    branch_id: str
    user_input_ref: EvidenceRef
    assistant_committed_output_refs: tuple[EvidenceRef, ...]
    tool_outcome_refs: tuple[EvidenceRef, ...]
    started_at: str
    committed_at: str
    finality_status: FinalityStatus
    source_revision: str


@dataclass(frozen=True)
class AutonomousEvent:
    """Committed assistant-initiated continuity event (for V1: canonical Wake)."""

    event_id: str
    identity_id: str
    chat_id: str
    branch_id: str
    event_kind: str
    committed_content_ref: EvidenceRef
    created_at: str
    finality_status: FinalityStatus
    source_revision: str


@dataclass(frozen=True)
class SourceMember:
    """Exact membership entry carried by an immutable source snapshot."""

    seq: int
    source_kind: SourceKind
    source_ref: str
    source_revision: str
    role: str
    content_hash: str
    span_start: int | None = None
    span_end: int | None = None
    # Additive R2 sealing metadata.  Existing R1 callers may omit it.
    logical_size: int = 0
    created_at: str = ''
    branch_id: str = 'active-transcript'


@dataclass(frozen=True)
class SourceSnapshot:
    """Immutable, exactly enumerable source set for a later derived artifact."""

    snapshot_id: str
    identity_id: str
    chat_id: str
    branch_id: str
    local_day: str
    source_watermark: int
    policy_version: str
    source_hash: str
    status: str
    created_at: str
    members: tuple[SourceMember, ...]


@dataclass(frozen=True)
class ContinuityGenerationJob:
    """Candidate-level R3 generation job with frozen execution provenance."""

    generation_job_id: str
    idempotency_key: str
    candidate_id: str
    snapshot_id: str
    candidate_source_revision: str
    generator_policy_version: str
    prompt_policy_version: str
    measurement_semantics: str
    frozen_provider: str | None
    frozen_model_identity: str | None
    status: str
    attempt: int
    error_code: str | None
    generation_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ContinuityChunk:
    """Immutable generated shadow artifact; lifecycle status is metadata only."""

    chunk_id: str
    generation_job_id: str
    candidate_id: str
    snapshot_id: str
    artifact_revision: str
    body: str
    body_hash: str
    source_token_estimate: int
    output_token_estimate: int
    generator_policy_version: str
    prompt_policy_version: str
    provider: str
    model_identity: str
    actual_executor: str
    generation_id: str
    status: str
    created_at: str

