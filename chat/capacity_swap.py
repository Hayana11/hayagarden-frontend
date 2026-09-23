"""Capacity Swap Core (P-CONTEXT-WINDOW Step 8-A).

Pure preparation layer: builds an unpublished ``CapacitySwapCandidate`` transcript
preview. Never writes DB, publishes JSONL, resumes Claude, or swaps residents.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from chat.claude_transcript_model import (
    EventRole,
    SidechainPolicy,
    SummaryPolicy,
    ThinkingPolicy,
    TranscriptGraph,
    UnknownEventPolicy,
)
from chat.claude_transcript_transform import (
    SelectionPolicy,
    TransformError,
    TransformRequest,
    emit_anchor_user_event,
    estimate_serialized_token_count,
    merge_prepended_user_and_tail,
    serialize_events,
    sha256_text,
    transform_transcript,
)
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events
from chat.daily_context import _USER_AUTHORS

# Frozen Capacity Swap trigger reasons (must match ``cc_resident._decide_respawn_reason``).
CAPACITY_SWAP_REASONS = frozenset({
    'soft_context',
    'hard_context',
    'turn_limit',
})

REJECTED_NON_CAPACITY_REASONS = frozenset({
    'process_dead',
    'idle',
    'system_changed',
    'tool_profile_changed',
    'model_changed',
    'runtime_changed',
    'history_rewrite',
})


class AnchorStatus(str, Enum):
    ANCHOR_RETAINED = 'ANCHOR_RETAINED'
    ANCHOR_UNAVAILABLE = 'ANCHOR_UNAVAILABLE'
    ANCHOR_TOO_LARGE = 'ANCHOR_TOO_LARGE'
    ANCHOR_IMAGE_DEGRADED = 'ANCHOR_IMAGE_DEGRADED'


class CapacitySwapStatus(str, Enum):
    READY = 'READY'
    TAIL_BUDGET_EXCEEDED = 'TAIL_BUDGET_EXCEEDED'
    TRIGGER_NOT_CAPACITY = 'TRIGGER_NOT_CAPACITY'
    TRANSFORM_FAILED = 'TRANSFORM_FAILED'
    VALIDATOR_REJECTED = 'VALIDATOR_REJECTED'
    ANCHOR_MAPPING_MISSING = 'ANCHOR_MAPPING_MISSING'


@dataclass(frozen=True)
class CapacitySwapCandidate:
    source_context_id: int
    source_context_epoch: int
    source_resident_generation: int

    source_claude_session_id: str
    source_transcript_path: str
    source_scan_offset: int
    source_sha256: str

    target_resident_generation: int
    candidate_session_id: str

    trigger_reason: str

    anchor_status: AnchorStatus
    anchor_message_id: int
    anchor_event_uuid: Optional[str]

    selected_round_count: int
    selected_message_ids: tuple[int, ...]

    estimated_tokens: int
    serialized_bytes: int
    event_count: int
    output_sha256: str

    serialized_jsonl: str

    boundary_required: bool = True

    warnings: tuple[str, ...] = ()


@dataclass
class CapacitySwapPrepareResult:
    status: CapacitySwapStatus
    candidate: Optional[CapacitySwapCandidate] = None
    warnings: list[str] = field(default_factory=list)


def _is_user_author(author: str) -> bool:
    return str(author or '').strip().lower() in _USER_AUTHORS


def _first_formal_user_message(
    formal_messages: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    for row in formal_messages:
        author = str(row.get('author') or '')
        if _is_user_author(author):
            return row
    return None


def _estimate_canonical_token_count(canonical: Any) -> int:
    if isinstance(canonical, str):
        text = canonical
    elif isinstance(canonical, list):
        parts: list[str] = []
        for block in canonical:
            if isinstance(block, dict):
                if block.get('type') == 'text':
                    parts.append(str(block.get('text') or ''))
                else:
                    parts.append(str(block.get('type') or ''))
        text = '\n'.join(parts)
    else:
        text = str(canonical)
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
    return int(estimate_tokens_heuristic_cjk1_ascii4_v1(text))


def _candidate_session_id(
    *,
    source_context_id: int,
    source_context_epoch: int,
    source_resident_generation: int,
    source_scan_offset: int,
    trigger_reason: str,
    retained_transcript_token_budget: int,
    thinking_policy: ThinkingPolicy,
) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            'hayagarden-capacity-swap:'
            f'{source_context_id}:'
            f'{source_context_epoch}:'
            f'{source_resident_generation}:'
            f'{source_scan_offset}:'
            f'{trigger_reason}:'
            f'{retained_transcript_token_budget}:'
            f'{thinking_policy.value}'
        ),
    ))


def _eligible_tail_rounds(
    graph: TranscriptGraph,
    *,
    user_canonical_by_event_uuid: Mapping[str, Any],
    exclude_candidate_uuids: frozenset[str],
) -> list[Any]:
    eligible: list[Any] = []
    for rnd in graph.candidate_rounds:
        cand = rnd.candidate_user_event_uuid
        if cand in exclude_candidate_uuids:
            continue
        if cand not in user_canonical_by_event_uuid:
            continue
        if rnd.has_sidechain_impact:
            continue
        eligible.append(rnd)
    return eligible


def _message_ids_for_selected_rounds(
    selected_rounds: Sequence[Any],
    mapping_message_id_by_event_uuid: Mapping[str, int],
) -> list[int]:
    mids: list[int] = []
    seen: set[int] = set()
    for rnd in selected_rounds:
        for uid in rnd.event_uuids:
            mid = mapping_message_id_by_event_uuid.get(str(uid))
            if mid is None:
                continue
            imid = int(mid)
            if imid in seen:
                continue
            seen.add(imid)
            mids.append(imid)
    return mids


def _resolve_anchor(
    *,
    graph: TranscriptGraph,
    formal_messages: Sequence[Mapping[str, Any]],
    mapping_event_uuid_by_message_id: Mapping[int, str],
    user_canonical_by_event_uuid: Mapping[str, Any],
    anchor_token_budget: int,
) -> tuple[AnchorStatus, int, Optional[str], Optional[Any], list[str]]:
    warnings: list[str] = []
    first_user = _first_formal_user_message(formal_messages)
    if first_user is None:
        return AnchorStatus.ANCHOR_UNAVAILABLE, 0, None, None, warnings

    anchor_message_id = int(first_user.get('id') or 0)
    if anchor_message_id <= 0:
        return AnchorStatus.ANCHOR_UNAVAILABLE, 0, None, None, warnings

    anchor_event_uuid = mapping_event_uuid_by_message_id.get(anchor_message_id)
    if not anchor_event_uuid:
        return (
            AnchorStatus.ANCHOR_UNAVAILABLE,
            anchor_message_id,
            None,
            None,
            warnings,
        )

    anchor_event_uuid = str(anchor_event_uuid)
    canonical = user_canonical_by_event_uuid.get(anchor_event_uuid)
    if canonical is None:
        return (
            AnchorStatus.ANCHOR_UNAVAILABLE,
            anchor_message_id,
            anchor_event_uuid,
            None,
            warnings,
        )

    from chat.attachment_contract import provider_current_turn_attachments
    normalized_attachments = provider_current_turn_attachments(
        first_user.get('attachments') or [],
        legacy_image_url=first_user.get('image_url') or '',
        legacy_file_url=first_user.get('file_url') or '',
        legacy_file_name=first_user.get('file_name') or '',
    )
    has_image_attachment = any(
        item['type'] == 'image' for item in normalized_attachments
    )
    anchor_status = AnchorStatus.ANCHOR_RETAINED
    if has_image_attachment and isinstance(canonical, str):
        anchor_status = AnchorStatus.ANCHOR_IMAGE_DEGRADED
        warnings.append('anchor_image_degraded_to_text')

    token_est = _estimate_canonical_token_count(canonical)
    if token_est > int(anchor_token_budget):
        return (
            AnchorStatus.ANCHOR_TOO_LARGE,
            anchor_message_id,
            anchor_event_uuid,
            canonical,
            warnings,
        )

    src_evt = graph.by_uuid.get(anchor_event_uuid)
    if src_evt is None or src_evt.event_role != EventRole.CANDIDATE_USER:
        return (
            AnchorStatus.ANCHOR_UNAVAILABLE,
            anchor_message_id,
            anchor_event_uuid,
            None,
            warnings,
        )

    return anchor_status, anchor_message_id, anchor_event_uuid, canonical, warnings


def prepare_capacity_swap_candidate(
    *,
    graph: TranscriptGraph,
    trigger_reason: str,
    source_context_id: int,
    source_context_epoch: int,
    source_resident_generation: int,
    source_claude_session_id: str,
    source_transcript_path: str,
    source_scan_offset: int,
    source_sha256: str,
    formal_messages: Sequence[Mapping[str, Any]],
    user_canonical_by_event_uuid: Mapping[str, Any],
    mapping_event_uuid_by_message_id: Mapping[int, str],
    mapping_message_id_by_event_uuid: Mapping[str, int],
    retained_transcript_token_budget: int,
    anchor_token_budget: int,
    cwd: str,
    candidate_session_id: Optional[str] = None,
    thinking_policy: ThinkingPolicy = ThinkingPolicy.DROP,
) -> CapacitySwapPrepareResult:
    """Build an unpublished Capacity Swap candidate (no DB / publish / resume)."""
    reason = str(trigger_reason or '').strip()
    warnings: list[str] = []

    if reason in REJECTED_NON_CAPACITY_REASONS:
        return CapacitySwapPrepareResult(
            status=CapacitySwapStatus.TRIGGER_NOT_CAPACITY,
            warnings=[f'rejected_reason:{reason}'],
        )
    if reason not in CAPACITY_SWAP_REASONS:
        return CapacitySwapPrepareResult(
            status=CapacitySwapStatus.TRIGGER_NOT_CAPACITY,
            warnings=[f'unknown_reason:{reason}'],
        )

    cid = str(candidate_session_id or '').strip()
    if not cid:
        cid = _candidate_session_id(
            source_context_id=source_context_id,
            source_context_epoch=source_context_epoch,
            source_resident_generation=source_resident_generation,
            source_scan_offset=source_scan_offset,
            trigger_reason=reason,
            retained_transcript_token_budget=retained_transcript_token_budget,
            thinking_policy=thinking_policy,
        )

    anchor_status, anchor_message_id, anchor_event_uuid, anchor_canonical, anchor_warnings = (
        _resolve_anchor(
            graph=graph,
            formal_messages=formal_messages,
            mapping_event_uuid_by_message_id=mapping_event_uuid_by_message_id,
            user_canonical_by_event_uuid=user_canonical_by_event_uuid,
            anchor_token_budget=anchor_token_budget,
        )
    )
    warnings.extend(anchor_warnings)

    exclude_anchor = frozenset()
    if anchor_event_uuid:
        exclude_anchor = frozenset({anchor_event_uuid})

    tail_request = TransformRequest(
        new_session_id=cid,
        cwd=cwd,
        keep_rounds=0,
        user_canonical_by_event_uuid=user_canonical_by_event_uuid,
        thinking_policy=thinking_policy,
        sidechain_policy=SidechainPolicy.EXCLUDE,
        summary_policy=SummaryPolicy.DROP,
        unknown_event_policy=UnknownEventPolicy.DROP,
        selection_policy=SelectionPolicy.TOKEN_BUDGET_TAIL,
        tail_token_budget=int(retained_transcript_token_budget),
        exclude_round_candidate_uuids=exclude_anchor,
    )

    try:
        tail_result = transform_transcript(graph, tail_request)
    except TransformError as exc:
        return CapacitySwapPrepareResult(
            status=CapacitySwapStatus.TRANSFORM_FAILED,
            warnings=warnings + [str(exc)],
        )

    tail_budget_exceeded = False
    eligible_tail = _eligible_tail_rounds(
        graph,
        user_canonical_by_event_uuid=user_canonical_by_event_uuid,
        exclude_candidate_uuids=exclude_anchor,
    )
    if len(eligible_tail) > tail_result.selected_round_count:
        tail_budget_exceeded = True
        warnings.append('tail_budget_exceeded')

    selected_tail_rounds = (
        eligible_tail[-tail_result.selected_round_count:]
        if tail_result.selected_round_count
        else []
    )

    events: list[dict[str, Any]] = list(tail_result.events)
    if (
        anchor_status == AnchorStatus.ANCHOR_RETAINED
        or anchor_status == AnchorStatus.ANCHOR_IMAGE_DEGRADED
    ):
        assert anchor_event_uuid and anchor_canonical is not None
        src_evt = graph.by_uuid[anchor_event_uuid]
        anchor_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f'{cid}:anchor:{anchor_event_uuid}')
        )
        anchor_evt = emit_anchor_user_event(
            src_evt,
            new_session_id=cid,
            cwd=cwd,
            version=tail_request.version,
            new_event_uuid=anchor_uuid,
            canonical=anchor_canonical,
        )
        events = merge_prepended_user_and_tail(anchor_evt, tail_result.events)

    serialized = serialize_events(events)
    serialized_bytes = len(serialized.encode('utf-8'))
    output_sha256 = sha256_text(serialized)
    estimated_tokens = estimate_serialized_token_count(events)

    validation = validate_transcript_events(
        events,
        ValidatorOptions(
            session_id=cid,
            thinking_policy=thinking_policy,
            forbid_sidechain=True,
            forbid_summary=True,
            expected_round_count=None,
            max_round_count=None,
            old_uuids=set(graph.by_uuid.keys()),
            unknown_event_mode='reject',
        ),
    )
    if not validation.ok:
        return CapacitySwapPrepareResult(
            status=CapacitySwapStatus.VALIDATOR_REJECTED,
            warnings=warnings + list(validation.errors),
        )

    selected_mids: list[int] = []
    if anchor_status in {
        AnchorStatus.ANCHOR_RETAINED,
        AnchorStatus.ANCHOR_IMAGE_DEGRADED,
    } and anchor_message_id > 0:
        selected_mids.append(int(anchor_message_id))
    for mid in _message_ids_for_selected_rounds(
        selected_tail_rounds,
        mapping_message_id_by_event_uuid,
    ):
        if mid not in selected_mids:
            selected_mids.append(mid)

    status = CapacitySwapStatus.READY
    if tail_budget_exceeded and tail_result.selected_round_count == 0:
        status = CapacitySwapStatus.TAIL_BUDGET_EXCEEDED

    candidate = CapacitySwapCandidate(
        source_context_id=int(source_context_id),
        source_context_epoch=int(source_context_epoch),
        source_resident_generation=int(source_resident_generation),
        source_claude_session_id=str(source_claude_session_id),
        source_transcript_path=str(source_transcript_path),
        source_scan_offset=int(source_scan_offset),
        source_sha256=str(source_sha256),
        target_resident_generation=int(source_resident_generation) + 1,
        candidate_session_id=cid,
        trigger_reason=reason,
        anchor_status=anchor_status,
        anchor_message_id=int(anchor_message_id),
        anchor_event_uuid=anchor_event_uuid,
        selected_round_count=int(tail_result.selected_round_count),
        selected_message_ids=tuple(selected_mids),
        estimated_tokens=int(estimated_tokens),
        serialized_bytes=int(serialized_bytes),
        event_count=len(events),
        output_sha256=output_sha256,
        serialized_jsonl=serialized,
        boundary_required=True,
        warnings=tuple(warnings),
    )
    return CapacitySwapPrepareResult(status=status, candidate=candidate, warnings=warnings)
