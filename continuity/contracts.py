"""Frozen data contracts for Continuity Compression R1.

These objects describe source evidence only. They do not generate compression,
select context, mutate runtime state, or write persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FinalityStatus = Literal['completed', 'incomplete']
SourceKind = Literal['completed_turn', 'autonomous_event', 'attachment_span']


@dataclass(frozen=True)
class EvidenceRef:
    """Content-addressed reference to one durable source or tool outcome."""

    source_ref: str
    source_revision: str
    content_hash: str


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
