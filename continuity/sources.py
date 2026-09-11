"""Pure source derivation for Continuity Compression R1.

Accepts durable ``chat_messages``-shaped rows and returns exact continuity
source contracts. This module never opens production databases or mutates
runtime state.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from chat.attachment_contract import persisted_chat_attachments
from chat.daily_context import (
    SOURCE_KIND_WAKE,
    is_formal_chat_message,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
from continuity.contracts import (
    AutonomousEvent,
    CanonicalTurn,
    EvidenceRef,
    SourceMember,
    SourceSnapshot,
)
from continuity.coverage import source_hash

IDENTITY_ID = 'fyodor'
CHAT_ID = 'default'
POLICY_VERSION = 'continuity_source_v1'

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})
_ASSISTANT_AUTHORS = frozenset({'fyodor', 'claude', 'assistant'})


def _value(row: Any, key: str, default: Any = '') -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _json_value(raw: Any, default: Any) -> Any:
    if raw in (None, ''):
        return default
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _source_kind(row: Any) -> str:
    return str(_value(row, 'source_kind', '') or '').strip().lower()


def _author(row: Any) -> str:
    return str(_value(row, 'author', '') or '').strip().lower()


def _attachments(row: Any) -> list[dict[str, str]]:
    return persisted_chat_attachments(
        _value(row, 'attachments', []),
        legacy_file_url=_value(row, 'file_url', ''),
        legacy_file_name=_value(row, 'file_name', ''),
        legacy_image_url=_value(row, 'image_url', ''),
    )


def _branches_semantic(row: Any) -> list[dict[str, Any]]:
    """Keep branch facts while deliberately excluding branch thinking."""
    raw = _json_value(_value(row, 'branches', ''), [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            'content': str(item.get('content') or ''),
            'tool_calls': _json_value(item.get('tool_calls'), []),
        })
    return out


def _active_branch_identity(row: Any) -> str:
    branches = _branches_semantic(row)
    try:
        idx = int(_value(row, 'branch_idx', 0) or 0)
    except (TypeError, ValueError):
        idx = 0
    if branches:
        return f'message:{int(_value(row, "id", 0) or 0)}:branch:{idx}'
    return 'active-transcript'


def _semantic_cache_info(row: Any) -> dict[str, Any]:
    """Only source/finality provenance survives; usage/transport metrics do not."""
    raw = _json_value(_value(row, 'cache_info', ''), {})
    if not isinstance(raw, dict):
        return {}
    keys = (
        'turn_incomplete',
        'partial_rescue',
        'stream_interrupted',
        'wake_mode',
        'canonical_chat_history',
        'unified_chat_resident',
        'b3_authority',
        'source',
        'provider',
    )
    return {key: raw[key] for key in keys if key in raw}


def _row_payload(row: Any) -> dict[str, Any]:
    """Provider-visible durable evidence only; thinking is never included."""
    return {
        'id': int(_value(row, 'id', 0) or 0),
        'author': _author(row),
        'content': str(_value(row, 'content', '') or ''),
        'source_kind': _source_kind(row),
        'tool_calls': _json_value(_value(row, 'tool_calls', ''), []),
        'branches': _branches_semantic(row),
        'branch_idx': int(_value(row, 'branch_idx', 0) or 0),
        'cache_info': _semantic_cache_info(row),
        'attachments': _attachments(row),
        'created_at': str(_value(row, 'created_at', '') or ''),
    }


def _row_payload_json(row: Any) -> str:
    return _canonical_json(_row_payload(row))


def row_content_hash(row: Any) -> str:
    return _sha256_text(_row_payload_json(row))


def row_logical_size(row: Any) -> int:
    """Stable source-token estimate of canonical provider-visible evidence."""
    return int(estimate_tokens_heuristic_cjk1_ascii4_v1(_row_payload_json(row)))


def row_revision(row: Any) -> str:
    return row_content_hash(row)


def evidence_ref(row: Any, *, prefix: str = 'message') -> EvidenceRef:
    mid = int(_value(row, 'id', 0) or 0)
    payload = _row_payload_json(row)
    digest = _sha256_text(payload)
    return EvidenceRef(
        source_ref=f'{prefix}:{mid}',
        source_revision=digest,
        content_hash=digest,
        logical_size=int(estimate_tokens_heuristic_cjk1_ascii4_v1(payload)),
    )


_INCOMPLETE_MARKERS = (
    'turn_incomplete',
    'partial_rescue',
    'stream_interrupted',
)


def _explicitly_incomplete(row: Any) -> bool:
    cache = _semantic_cache_info(row)
    return any(cache.get(key) is True for key in _INCOMPLETE_MARKERS)


def is_incomplete_source_row(row: Any) -> bool:
    """Return whether durable provenance marks a row as non-final."""
    return _explicitly_incomplete(row)


def is_formal_user_source_row(row: Any) -> bool:
    return _author(row) in _USER_AUTHORS and is_formal_chat_message(row)


def _is_formal_assistant(row: Any) -> bool:
    return _author(row) in _ASSISTANT_AUTHORS and is_formal_chat_message(row)


def _tool_outcome_refs(assistant_row: Any) -> tuple[EvidenceRef, ...]:
    raw = _json_value(_value(assistant_row, 'tool_calls', ''), [])
    if not isinstance(raw, list):
        return ()
    refs: list[EvidenceRef] = []
    mid = int(_value(assistant_row, 'id', 0) or 0)
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        outcome = canonical_tool_outcome(item)
        serialized = _canonical_json(outcome)
        digest = _sha256_text(serialized)
        refs.append(EvidenceRef(
            source_ref=f'message:{mid}:tool_outcome:{index}',
            source_revision=digest,
            content_hash=digest,
            logical_size=int(estimate_tokens_heuristic_cjk1_ascii4_v1(serialized)),
        ))
    return tuple(refs)


def canonical_tool_outcome(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return the single durable tool-outcome payload used by R1 and R3."""
    return {
        # Arguments are part of the durable invocation identity. Keeping them
        # in the outcome ref prevents equal results from different calls from
        # collapsing into one evidence object.
        'name': item.get('name'),
        'args': item.get('args'),
        'result': item.get('result'),
        'success': item.get('success'),
        'artifact': item.get('artifact'),
        'diff': item.get('diff'),
    }


def _turn_revision(user_row: Any, assistant_row: Any) -> str:
    return _sha256_text(_canonical_json({
        'user': row_revision(user_row),
        'assistant': row_revision(assistant_row),
        'branch': _active_branch_identity(assistant_row),
        'tools': [ref.source_revision for ref in _tool_outcome_refs(assistant_row)],
    }))


def derive_completed_turns(
    rows: Iterable[Any],
    *,
    identity_id: str = IDENTITY_ID,
    chat_id: str = CHAT_ID,
) -> tuple[CanonicalTurn, ...]:
    """Derive completed user→assistant turns from the active durable transcript.

    V1 fails closed: exactly one formal assistant before the next formal user is
    required, and explicit partial-rescue rows are never completed turns.
    """
    ordered = sorted(rows, key=lambda row: int(_value(row, 'id', 0) or 0))
    completed: list[CanonicalTurn] = []
    current_user: Any | None = None
    assistants: list[Any] = []

    def flush() -> None:
        nonlocal current_user, assistants
        if current_user is not None and len(assistants) == 1 and not _explicitly_incomplete(assistants[0]):
            assistant = assistants[0]
            uid = int(_value(current_user, 'id', 0) or 0)
            aid = int(_value(assistant, 'id', 0) or 0)
            completed.append(CanonicalTurn(
                turn_id=f'turn:{uid}:{aid}',
                identity_id=identity_id,
                chat_id=chat_id,
                branch_id=_active_branch_identity(assistant),
                user_input_ref=evidence_ref(current_user),
                assistant_committed_output_refs=(evidence_ref(assistant),),
                tool_outcome_refs=_tool_outcome_refs(assistant),
                started_at=str(_value(current_user, 'created_at', '') or ''),
                committed_at=str(_value(assistant, 'created_at', '') or ''),
                finality_status='completed',
                source_revision=_turn_revision(current_user, assistant),
            ))
        current_user = None
        assistants = []

    for row in ordered:
        if is_formal_user_source_row(row):
            flush()
            current_user = row
        elif _is_formal_assistant(row) and current_user is not None:
            assistants.append(row)
    flush()
    return tuple(completed)


def _canonical_normal_wake(row: Any) -> bool:
    if _source_kind(row) != SOURCE_KIND_WAKE or _author(row) not in _ASSISTANT_AUTHORS:
        return False
    cache = _semantic_cache_info(row)
    return bool(
        cache.get('wake_mode') == 'normal'
        and cache.get('canonical_chat_history') is True
        and cache.get('unified_chat_resident') is True
        and cache.get('b3_authority') is True
        and cache.get('source') == SOURCE_KIND_WAKE
        and cache.get('provider') == 'claude_code'
        and not _explicitly_incomplete(row)
        and str(_value(row, 'content', '') or '').strip()
    )


def derive_autonomous_events(
    rows: Iterable[Any],
    *,
    identity_id: str = IDENTITY_ID,
    chat_id: str = CHAT_ID,
) -> tuple[AutonomousEvent, ...]:
    events: list[AutonomousEvent] = []
    for row in sorted(rows, key=lambda item: int(_value(item, 'id', 0) or 0)):
        if not _canonical_normal_wake(row):
            continue
        mid = int(_value(row, 'id', 0) or 0)
        revision = row_revision(row)
        events.append(AutonomousEvent(
            event_id=f'wake:{mid}',
            identity_id=identity_id,
            chat_id=chat_id,
            branch_id='active-transcript',
            event_kind='wake',
            committed_content_ref=evidence_ref(row),
            created_at=str(_value(row, 'created_at', '') or ''),
            finality_status='completed',
            source_revision=revision,
        ))
    return tuple(events)


def enumerate_candidate_source_refs(rows: Iterable[Any]) -> tuple[str, ...]:
    """Enumerate raw eligible unit ids independently of member materialization.

    Replay uses this pass as the expected set so a future derivation or member
    builder regression cannot make coverage validate only against its own output.
    """
    ordered = sorted(rows, key=lambda row: int(_value(row, 'id', 0) or 0))
    refs: list[str] = []
    current_user: Any | None = None
    assistants: list[Any] = []

    def flush() -> None:
        nonlocal current_user, assistants
        if (
            current_user is not None
            and len(assistants) == 1
            and not _explicitly_incomplete(assistants[0])
        ):
            uid = int(_value(current_user, 'id', 0) or 0)
            aid = int(_value(assistants[0], 'id', 0) or 0)
            refs.append(f'turn:{uid}:{aid}')
        current_user = None
        assistants = []

    for row in ordered:
        if is_formal_user_source_row(row):
            flush()
            current_user = row
        elif _is_formal_assistant(row) and current_user is not None:
            assistants.append(row)
    flush()

    refs.extend(
        f'wake:{int(_value(row, "id", 0) or 0)}'
        for row in ordered
        if _canonical_normal_wake(row)
    )
    return tuple(refs)


def build_source_members(
    turns: Iterable[CanonicalTurn],
    events: Iterable[AutonomousEvent],
) -> tuple[SourceMember, ...]:
    units: list[tuple[str, str, str, str, str, int, str]] = []
    for turn in turns:
        size = turn.user_input_ref.logical_size
        size += sum(ref.logical_size for ref in turn.assistant_committed_output_refs)
        size += sum(ref.logical_size for ref in turn.tool_outcome_refs)
        units.append((
            turn.started_at,
            'completed_turn',
            turn.turn_id,
            turn.source_revision,
            'conversation',
            size,
            turn.branch_id,
        ))
    for event in events:
        units.append((
            event.created_at,
            'autonomous_event',
            event.event_id,
            event.source_revision,
            'assistant',
            event.committed_content_ref.logical_size,
            event.branch_id,
        ))
    units.sort(key=lambda item: (item[0], item[2]))
    return tuple(
        SourceMember(
            seq=index,
            source_kind=kind,  # type: ignore[arg-type]
            source_ref=ref,
            source_revision=revision,
            role=role,
            content_hash=revision,
            logical_size=max(0, int(logical_size)),
            created_at=created_at,
            branch_id=branch_id or 'active-transcript',
        )
        for index, (created_at, kind, ref, revision, role, logical_size, branch_id)
        in enumerate(units)
    )


def build_source_snapshot(
    *,
    turns: Iterable[CanonicalTurn],
    events: Iterable[AutonomousEvent],
    local_day: str,
    source_watermark: int,
    created_at: str,
    identity_id: str = IDENTITY_ID,
    chat_id: str = CHAT_ID,
    branch_id: str = 'active-transcript',
    policy_version: str = POLICY_VERSION,
) -> SourceSnapshot:
    members = build_source_members(turns, events)
    digest = source_hash(members)
    return SourceSnapshot(
        snapshot_id=f'source:{local_day}:{source_watermark}:{digest[:16]}',
        identity_id=identity_id,
        chat_id=chat_id,
        branch_id=branch_id,
        local_day=local_day,
        source_watermark=int(source_watermark),
        policy_version=policy_version,
        source_hash=digest,
        status='ready',
        created_at=created_at,
        members=members,
    )

