"""Pure deterministic Transcript Transform (v0.2 Transcript Core).

Inputs are explicit parameters only. This module never:
- opens a database
- accesses the filesystem
- calls a model
- reads production environment variables
- publishes JSONL / executes resume
- mutates the input TranscriptGraph or its events
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence

from chat.claude_transcript_model import (
    EventRole,
    SidechainPolicy,
    SummaryPolicy,
    ThinkingPolicy,
    TranscriptEvent,
    TranscriptGraph,
    UnknownEventPolicy,
)

STRIP_TOP_LEVEL_KEYS = frozenset({
    'requestId', 'request_id', 'promptId', 'prompt_id',
})


class TransformErrorCode(str, Enum):
    MAPPING_MISSING = 'TRANSFORM_MAPPING_MISSING'
    MAPPING_EMPTY = 'TRANSFORM_MAPPING_EMPTY'
    ROUND_BUDGET = 'TRANSFORM_ROUND_BUDGET'
    TOOL_ORPHAN = 'TRANSFORM_TOOL_ORPHAN'
    INVALID_POLICY = 'TRANSFORM_INVALID_POLICY'
    EMPTY_SELECTION = 'TRANSFORM_EMPTY_SELECTION'
    BYTE_BUDGET = 'TRANSFORM_BYTE_BUDGET'
    THINKING_INVALID = 'TRANSFORM_THINKING_INVALID'
    UNCONFIRMED_USER = 'TRANSFORM_UNCONFIRMED_USER'


class TransformError(ValueError):
    def __init__(self, code: TransformErrorCode, detail: str = ''):
        self.code = code
        msg = code.value if not detail else f'{code.value}:{detail}'
        super().__init__(msg)
        self.detail = detail


@dataclass(frozen=True)
class ToolPrimerCandidate:
    """Interface-only primer candidate. This PR does not search ancestors."""

    label: str
    events: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class TransformRequest:
    new_session_id: str
    cwd: str
    keep_rounds: int
    # authoritative app-message mapping: source event uuid -> canonical user
    # payload (plain str or multimodal content block list from DB).
    user_canonical_by_event_uuid: Mapping[str, Any]
    thinking_policy: ThinkingPolicy
    sidechain_policy: SidechainPolicy = SidechainPolicy.EXCLUDE
    summary_policy: SummaryPolicy = SummaryPolicy.DROP
    unknown_event_policy: UnknownEventPolicy = UnknownEventPolicy.DROP
    version: str = '2.1.220'
    max_output_bytes: Optional[int] = None
    max_output_tokens_estimate: Optional[int] = None
    # optional validated primer candidate (not searched here; not 0-round default)
    tool_primer_candidate: Optional[ToolPrimerCandidate] = None


@dataclass
class TransformResult:
    events: list[dict[str, Any]]
    uuid_map: dict[str, str] = field(default_factory=dict)
    tool_id_map: dict[str, str] = field(default_factory=dict)
    selected_round_count: int = 0
    dropped_sidechain_uuids: list[str] = field(default_factory=list)
    dropped_sidechain_round_user_uuids: list[str] = field(default_factory=list)
    dropped_summary_uuids: list[str] = field(default_factory=list)
    dropped_system_uuids: list[str] = field(default_factory=list)
    dropped_unconfirmed_user_uuids: list[str] = field(default_factory=list)
    dropped_noise_uuids: list[str] = field(default_factory=list)
    output_sha256: str = ''
    notes: list[str] = field(default_factory=list)


def new_uuid() -> str:
    return str(uuid.uuid4())


def serialize_events(events: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        json.dumps(evt, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        for evt in events
    ]
    return '\n'.join(lines) + ('\n' if lines else '')


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _filter_thinking(blocks: list[dict[str, Any]], policy: ThinkingPolicy) -> list[dict[str, Any]]:
    if policy == ThinkingPolicy.KEEP:
        for b in blocks:
            if b.get('type') == 'thinking':
                if not str(b.get('thinking') or '').strip():
                    raise TransformError(
                        TransformErrorCode.THINKING_INVALID,
                        'empty_thinking',
                    )
                if not str(b.get('signature') or '').strip():
                    raise TransformError(
                        TransformErrorCode.THINKING_INVALID,
                        'missing_signature',
                    )
            if b.get('type') == 'redacted_thinking' and not b.get('data'):
                raise TransformError(
                    TransformErrorCode.THINKING_INVALID,
                    'empty_redacted_thinking',
                )
        return blocks
    if policy == ThinkingPolicy.DROP:
        return [
            b for b in blocks
            if b.get('type') not in {'thinking', 'redacted_thinking'}
        ]
    raise TransformError(TransformErrorCode.INVALID_POLICY, str(policy))


def _remap_tool_blocks(
    blocks: list[dict[str, Any]],
    tool_id_map: dict[str, str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in blocks:
        b = copy.deepcopy(block)
        btype = b.get('type')
        if btype == 'tool_use':
            old_id = str(b.get('id') or '')
            if not old_id:
                raise TransformError(TransformErrorCode.TOOL_ORPHAN, 'empty_tool_use_id')
            new_id = tool_id_map.get(old_id)
            if new_id is None:
                new_id = 'toolu_' + hashlib.sha256(old_id.encode('utf-8')).hexdigest()[:20]
                tool_id_map[old_id] = new_id
            b['id'] = new_id
        elif btype == 'tool_result':
            old_id = str(b.get('tool_use_id') or '')
            if not old_id:
                raise TransformError(TransformErrorCode.TOOL_ORPHAN, 'empty_tool_result_id')
            if old_id not in tool_id_map:
                tool_id_map[old_id] = (
                    'toolu_' + hashlib.sha256(old_id.encode('utf-8')).hexdigest()[:20]
                )
            b['tool_use_id'] = tool_id_map[old_id]
        out.append(b)
    return out


def _strip_assistant_metadata(evt: dict[str, Any]) -> None:
    for key in list(evt.keys()):
        if key in STRIP_TOP_LEVEL_KEYS:
            evt.pop(key, None)
    message = evt.get('message')
    if isinstance(message, dict):
        message.pop('id', None)
        message.pop('model', None)
        message.pop('usage', None)


def _is_auto_noise(evt: TranscriptEvent) -> bool:
    return evt.event_role in {EventRole.META, EventRole.UNKNOWN}


def _require_sidechain_exclude(policy: object) -> None:
    """v0.2 accepts only SidechainPolicy.EXCLUDE — no silent fallback."""
    if policy is SidechainPolicy.EXCLUDE:
        return
    if isinstance(policy, SidechainPolicy) and policy == SidechainPolicy.EXCLUDE:
        return
    raise TransformError(TransformErrorCode.INVALID_POLICY, 'sidechain')


def _require_summary_drop(policy: object) -> None:
    """v0.2 accepts only SummaryPolicy.DROP — no silent KEEP."""
    if policy is SummaryPolicy.DROP:
        return
    if isinstance(policy, SummaryPolicy) and policy == SummaryPolicy.DROP:
        return
    raise TransformError(TransformErrorCode.INVALID_POLICY, 'summary')


def _select_confirmed_rounds(
    graph: TranscriptGraph,
    request: TransformRequest,
) -> tuple[list, list[str], list[str]]:
    """Return (eligible_tail, dropped_sidechain_round_users, dropped_unconfirmed)."""
    if request.keep_rounds < 0:
        raise TransformError(TransformErrorCode.ROUND_BUDGET, 'negative')

    _require_sidechain_exclude(request.sidechain_policy)

    dropped_side_rounds: list[str] = []
    dropped_unconfirmed: list[str] = []
    eligible = []

    for rnd in graph.candidate_rounds:
        cand = rnd.candidate_user_event_uuid
        if cand not in request.user_canonical_by_event_uuid:
            dropped_unconfirmed.append(cand)
            continue
        # EXCLUDE: whole impacted round is dropped (never prune-and-keep)
        if rnd.has_sidechain_impact:
            dropped_side_rounds.append(cand)
            continue
        eligible.append(rnd)

    if request.keep_rounds == 0:
        return [], dropped_side_rounds, dropped_unconfirmed
    return eligible[-request.keep_rounds:], dropped_side_rounds, dropped_unconfirmed


def _emit_event(
    src: TranscriptEvent,
    *,
    req: TransformRequest,
    uuid_map: dict[str, str],
    tool_id_map: dict[str, str],
) -> dict[str, Any]:
    old_uid = src.event_uuid
    etype = src.event_type.value if src.event_type.value != 'unknown' else src.raw.get('type')
    new_evt: dict[str, Any] = {
        'type': etype,
        'uuid': uuid_map[old_uid],
        'parentUuid': None,
        'timestamp': src.raw.get('timestamp'),
        'sessionId': req.new_session_id,
        'cwd': req.cwd,
        'version': req.version,
    }

    if src.event_role == EventRole.CANDIDATE_USER:
        canonical = req.user_canonical_by_event_uuid.get(old_uid)
        if canonical is None:
            # Should not reach: unconfirmed rounds are filtered earlier
            raise TransformError(TransformErrorCode.UNCONFIRMED_USER, old_uid)
        if isinstance(canonical, str):
            if not canonical.strip():
                raise TransformError(TransformErrorCode.MAPPING_EMPTY, old_uid)
            user_content: Any = canonical
        elif isinstance(canonical, list):
            if not canonical:
                raise TransformError(TransformErrorCode.MAPPING_EMPTY, old_uid)
            user_content = copy.deepcopy(canonical)
        else:
            raise TransformError(
                TransformErrorCode.INVALID_POLICY,
                f'bad_canonical_type:{type(canonical).__name__}',
            )
        # never copy old user payload; rebuild from authoritative mapping only
        new_evt['message'] = {'role': 'user', 'content': user_content}
        return new_evt

    if src.event_role == EventRole.TOOL_RESULT_USER:
        message = copy.deepcopy(src.raw.get('message') or {})
        blocks = _content_blocks(message)
        message['role'] = 'user'
        message['content'] = _remap_tool_blocks(blocks, tool_id_map)
        new_evt['message'] = message
        src_tool = src.raw.get('sourceToolUseID')
        if isinstance(src_tool, str) and src_tool:
            new_evt['sourceToolUseID'] = tool_id_map.get(src_tool, src_tool)
        return new_evt

    if src.event_role == EventRole.ASSISTANT:
        message = copy.deepcopy(src.raw.get('message') or {})
        blocks = _content_blocks(message)
        blocks = _filter_thinking(blocks, req.thinking_policy)
        blocks = _remap_tool_blocks(blocks, tool_id_map)
        message['role'] = 'assistant'
        message['content'] = blocks
        new_evt['message'] = message
        _strip_assistant_metadata(new_evt)
        return new_evt

    # SYSTEM and others must never be migrated
    raise TransformError(TransformErrorCode.INVALID_POLICY, f'emit_forbidden:{src.event_role}')


def _check_budgets(events: Sequence[Mapping[str, Any]], request: TransformRequest) -> None:
    text = serialize_events(events)
    nbytes = len(text.encode('utf-8'))
    if request.max_output_bytes is not None and nbytes > request.max_output_bytes:
        raise TransformError(
            TransformErrorCode.BYTE_BUDGET,
            f'{nbytes}>{request.max_output_bytes}',
        )
    if request.max_output_tokens_estimate is not None:
        est = max(1, nbytes // 4)
        if est > request.max_output_tokens_estimate:
            raise TransformError(
                TransformErrorCode.BYTE_BUDGET,
                f'tokens_est:{est}>{request.max_output_tokens_estimate}',
            )


def transform_transcript(graph: TranscriptGraph, request: TransformRequest) -> TransformResult:
    """Pure transform: same inputs always yield the same serialized output."""
    if request.thinking_policy not in {ThinkingPolicy.KEEP, ThinkingPolicy.DROP}:
        raise TransformError(TransformErrorCode.INVALID_POLICY, 'thinking')

    _require_summary_drop(request.summary_policy)

    dropped_summary: list[str] = list(graph.summary_uuids)
    dropped_noise: list[str] = []
    dropped_system = list(graph.system_uuids)

    for evt in graph.events:
        if _is_auto_noise(evt):
            dropped_noise.append(evt.event_uuid)
        if (
            request.unknown_event_policy == UnknownEventPolicy.REJECT
            and evt.event_role == EventRole.UNKNOWN
        ):
            raise TransformError(TransformErrorCode.INVALID_POLICY, f'unknown:{evt.event_uuid}')

    selected, dropped_side_rounds, dropped_unconfirmed = _select_confirmed_rounds(
        graph, request
    )

    # All sidechain event UUIDs (for audit); whole rounds dropped separately
    sidechain_event_uuids = sorted(set(graph.sidechain_uuids))
    for rnd in graph.candidate_rounds:
        if rnd.candidate_user_event_uuid in dropped_side_rounds:
            sidechain_event_uuids = sorted(
                set(sidechain_event_uuids) | set(rnd.sidechain_impact_uuids)
            )

    result = TransformResult(
        events=[],
        dropped_sidechain_uuids=sidechain_event_uuids,
        dropped_sidechain_round_user_uuids=dropped_side_rounds,
        dropped_summary_uuids=dropped_summary,
        dropped_system_uuids=dropped_system,
        dropped_unconfirmed_user_uuids=dropped_unconfirmed,
        dropped_noise_uuids=dropped_noise,
        selected_round_count=len(selected),
    )

    if not selected:
        # 0-round and empty-after-filter both default to native cold / empty transcript
        if request.keep_rounds == 0:
            result.notes.append('native_cold_empty_transcript')
            result.output_sha256 = sha256_text(serialize_events(result.events))
            return result
        raise TransformError(TransformErrorCode.EMPTY_SELECTION, 'no_eligible_rounds')

    ordered_src: list[TranscriptEvent] = []
    seen: set[str] = set()
    for rnd in selected:
        for uid in rnd.event_uuids:
            if uid in seen:
                continue
            evt = graph.by_uuid.get(uid)
            if evt is None:
                continue
            # Never migrate SYSTEM / summary / meta / sidechain rows
            if evt.event_role == EventRole.SYSTEM:
                continue
            if evt.event_role == EventRole.SUMMARY:
                continue
            if _is_auto_noise(evt):
                continue
            if evt.is_sidechain or evt.event_role == EventRole.SIDECHAIN:
                continue
            if evt.event_role == EventRole.USER_CONTINUATION:
                continue
            ordered_src.append(evt)
            seen.add(uid)

    if not ordered_src or ordered_src[0].event_role != EventRole.CANDIDATE_USER:
        raise TransformError(TransformErrorCode.EMPTY_SELECTION, 'must_start_with_confirmed_user')

    # Confirm first event is mapped (belt and suspenders)
    if ordered_src[0].event_uuid not in request.user_canonical_by_event_uuid:
        raise TransformError(
            TransformErrorCode.UNCONFIRMED_USER,
            ordered_src[0].event_uuid,
        )

    uuid_map: dict[str, str] = {}
    tool_id_map: dict[str, str] = {}
    for evt in ordered_src:
        uuid_map[evt.event_uuid] = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f'{request.new_session_id}:{evt.event_uuid}')
        )

    forged: list[dict[str, Any]] = []
    for evt in ordered_src:
        forged.append(
            _emit_event(evt, req=request, uuid_map=uuid_map, tool_id_map=tool_id_map)
        )

    if request.tool_primer_candidate and request.tool_primer_candidate.events:
        primer_events = [copy.deepcopy(dict(e)) for e in request.tool_primer_candidate.events]
        for pe in primer_events:
            pe['sessionId'] = request.new_session_id
            pe['cwd'] = request.cwd
            pe['version'] = request.version
        forged = primer_events + forged
        result.notes.append(f'primer_candidate:{request.tool_primer_candidate.label}')

    if forged:
        forged[0]['parentUuid'] = None
        for idx in range(1, len(forged)):
            forged[idx]['parentUuid'] = forged[idx - 1]['uuid']

    emitted_uses: set[str] = set()
    emitted_results: set[str] = set()
    for evt in forged:
        message = evt.get('message') if isinstance(evt.get('message'), dict) else {}
        for block in _content_blocks(message):
            if block.get('type') == 'tool_use':
                emitted_uses.add(str(block.get('id') or ''))
            elif block.get('type') == 'tool_result':
                emitted_results.add(str(block.get('tool_use_id') or ''))
    orphans_use = {x for x in (emitted_uses - emitted_results) if x}
    orphans_result = {x for x in (emitted_results - emitted_uses) if x}
    if '' in emitted_uses or '' in emitted_results:
        raise TransformError(TransformErrorCode.TOOL_ORPHAN, 'empty_id')
    if orphans_use:
        raise TransformError(
            TransformErrorCode.TOOL_ORPHAN,
            'orphan_tool_use:' + ','.join(sorted(orphans_use)),
        )
    if orphans_result:
        raise TransformError(
            TransformErrorCode.TOOL_ORPHAN,
            'orphan_tool_result:' + ','.join(sorted(orphans_result)),
        )

    _check_budgets(forged, request)

    result.events = forged
    result.uuid_map = uuid_map
    result.tool_id_map = tool_id_map
    result.output_sha256 = sha256_text(serialize_events(forged))
    return result


def transform_deterministic_hash(
    graph: TranscriptGraph,
    request: TransformRequest,
    *,
    runs: int = 3,
) -> tuple[str, ...]:
    """Run transform `runs` times and return each output SHA-256."""
    hashes: list[str] = []
    for _ in range(runs):
        result = transform_transcript(graph, request)
        hashes.append(result.output_sha256)
    return tuple(hashes)
