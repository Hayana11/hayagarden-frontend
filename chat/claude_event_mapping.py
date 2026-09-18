"""Async message ↔ Claude JSONL event Mapping (offline single-pass entry).

Flag-off data layer. No background threads. Runtime may call
``run_mapping_pass`` later; this module never starts processes or models.

Write-lock scope: file I/O and Transcript Reader run *before* BEGIN IMMEDIATE.

Also owns synchronous backlog catch-up when Registry ``scan_offset`` lags
behind a later successful turn's transcript start (Step 10 Defect A).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from chat import daily_context as dc
from chat.claude_transcript_model import (
    CandidateConversationRound,
    EventRole,
    TranscriptEvent,
    TranscriptGraph,
)
from chat.claude_transcript_reader import (
    ReaderErrorCode,
    TranscriptReaderError,
    read_transcript_range,
)
from chat.session_registry import (
    SCAN_STATUS_READY,
    SessionRegistryConflict,
    SessionRegistryError,
    SessionRegistryNotFound,
    cas_advance_scan_offset,
    get_context_claude_session,
    mark_scan_blocked,
)

ROLE_USER = 'user'
ROLE_ASSISTANT = 'assistant'
ROLE_TOOL_USE = 'tool_use'
ROLE_TOOL_RESULT_USER = 'tool_result_user'

ERROR_MAPPING_LAG = 'mapping_lag'
ERROR_CATCHUP_REQUIRED = 'catchup_required'
ERROR_CATCHUP_INCOMPLETE_UNALIGNED = 'catchup_incomplete_unaligned'
ERROR_CATCHUP_AUTH_MISMATCH = 'catchup_auth_mismatch'
ERROR_CATCHUP_FAILED = 'catchup_failed'


class MappingError(Exception):
    def __init__(self, message: str, *, error_code: str = 'mapping_error'):
        super().__init__(message)
        self.error_code = error_code


class MappingConflict(MappingError):
    def __init__(self, message: str, *, error_code: str = 'mapping_conflict'):
        super().__init__(message, error_code=error_code)


class MappingRejected(MappingError):
    """Fail-closed rejection (missing/ambiguous candidates, identity, CAS)."""


@dataclass(frozen=True)
class MappingPassRequest:
    context_id: int
    context_epoch: int
    resident_generation: int
    chat_id: str
    user_message_id: int
    assistant_message_id: int
    expected_start_offset: int
    observed_end_offset: int
    terminal_receipt: Optional[dict[str, Any]] = None


@dataclass
class MappingPassResult:
    ok: bool
    error_code: Optional[str] = None
    mapped_event_uuids: list[str] = field(default_factory=list)
    inserted_event_uuids: list[str] = field(default_factory=list)
    confirmed_existing_event_uuids: list[str] = field(default_factory=list)
    scan_offset: Optional[int] = None
    registry: Optional[dict[str, Any]] = None
    terminal_receipt_id: Optional[int] = None


@dataclass(frozen=True)
class _AuthRound:
    user_message_id: int
    assistant_message_id: int
    incomplete: bool = False


def _assistant_row_incomplete(cache_info: Any) -> bool:
    raw = str(cache_info or '').strip()
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(info, dict):
        return False
    return bool(
        info.get('partial_rescue')
        or info.get('turn_incomplete')
        or info.get('stream_interrupted')
    )


def _load_auth_rounds_after(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    after_message_id: Optional[int],
    through_assistant_id: int,
) -> list[_AuthRound]:
    """Authoritative complete/incomplete formal rounds after Registry watermark.

    Order is message_id ASC within the same context/epoch/generation.
    Incomplete (partial_rescue) assistants are included so catch-up can skip
    them in lockstep with incomplete transcript segments — never mapped as
    complete rounds.
    """
    after = int(after_message_id or 0)
    through = int(through_assistant_id)
    cols = {
        str(r[1]) for r in conn.execute('PRAGMA table_info(chat_messages)').fetchall()
    }
    cache_sel = 'm.cache_info' if 'cache_info' in cols else "'' AS cache_info"
    rows = conn.execute(
        f'''SELECT m.id AS id, dmc.role AS role, {cache_sel}
            FROM daily_message_contexts dmc
            JOIN chat_messages m ON m.id = dmc.message_id
            WHERE dmc.context_id=? AND dmc.context_epoch=?
              AND dmc.resident_generation=?
              AND m.id > ? AND m.id <= ?
            ORDER BY m.id ASC''',
        (
            int(context_id), int(context_epoch), int(resident_generation),
            after, through,
        ),
    ).fetchall()

    rounds: list[_AuthRound] = []
    pending_user: Optional[int] = None
    for row in rows:
        mid = int(row['id'])
        role = str(row['role'] or '')
        if role == ROLE_USER:
            if pending_user is not None:
                raise MappingRejected(
                    f'auth user {pending_user} missing assistant before {mid}',
                    error_code=ERROR_CATCHUP_AUTH_MISMATCH,
                )
            pending_user = mid
            continue
        if role == ROLE_ASSISTANT:
            if pending_user is None:
                raise MappingRejected(
                    f'auth assistant {mid} without preceding user',
                    error_code=ERROR_CATCHUP_AUTH_MISMATCH,
                )
            rounds.append(_AuthRound(
                user_message_id=int(pending_user),
                assistant_message_id=mid,
                incomplete=_assistant_row_incomplete(row['cache_info']),
            ))
            pending_user = None
            continue
        raise MappingRejected(
            f'auth unexpected role={role!r} message_id={mid}',
            error_code=ERROR_CATCHUP_AUTH_MISMATCH,
        )
    if pending_user is not None:
        raise MappingRejected(
            f'auth trailing user {pending_user} without assistant',
            error_code=ERROR_CATCHUP_AUTH_MISMATCH,
        )
    return rounds


def _round_byte_end(
    graph: TranscriptGraph,
    rounds: Sequence[CandidateConversationRound],
    index: int,
    range_end: int,
) -> int:
    """Exclusive end offset for rounds[index] within [.., range_end]."""
    if index + 1 < len(rounds):
        next_uid = rounds[index + 1].candidate_user_event_uuid
        next_evt = graph.by_uuid.get(next_uid)
        if next_evt is None or next_evt.byte_offset is None:
            raise MappingRejected(
                'next candidate missing byte_offset',
                error_code=ERROR_CATCHUP_INCOMPLETE_UNALIGNED,
            )
        return int(next_evt.byte_offset)
    return int(range_end)


def _is_complete_terminal_round(graph: TranscriptGraph, rnd: CandidateConversationRound) -> bool:
    try:
        _assert_complete_terminal_round(graph, rnd)
        return True
    except MappingRejected:
        return False


def _content_has_tool_use(event: TranscriptEvent) -> bool:
    message = event.raw.get('message') if isinstance(event.raw.get('message'), dict) else {}
    content = message.get('content')
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get('type') == 'tool_use' for b in content)


def _content_has_text(event: TranscriptEvent) -> bool:
    message = event.raw.get('message') if isinstance(event.raw.get('message'), dict) else {}
    content = message.get('content')
    if isinstance(content, str) and content.strip():
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'text' and str(block.get('text') or '').strip():
            return True
    return False


def mapping_role_for_event(event: TranscriptEvent, *, is_canonical_user: bool) -> Optional[str]:
    """Map Reader roles to persisted mapping roles. Never upgrades candidates alone."""
    if is_canonical_user:
        if event.event_role != EventRole.CANDIDATE_USER or event.is_sidechain:
            return None
        return ROLE_USER
    if event.is_sidechain or event.event_role == EventRole.SIDECHAIN:
        return None
    if event.event_role == EventRole.ASSISTANT:
        if _content_has_tool_use(event) and not _content_has_text(event):
            return ROLE_TOOL_USE
        return ROLE_ASSISTANT
    if event.event_role == EventRole.TOOL_RESULT_USER:
        return ROLE_TOOL_RESULT_USER
    return None


def _assert_message_window(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    expected_role: str,
) -> None:
    mid = int(message_id)
    want_role = str(expected_role)
    row = conn.execute(
        'SELECT id FROM chat_messages WHERE id=?', (mid,),
    ).fetchone()
    if row is None:
        raise MappingRejected(
            f'message_id {mid} not in chat_messages',
            error_code='message_missing',
        )
    dmc = conn.execute(
        'SELECT context_id, context_epoch, resident_generation, role '
        'FROM daily_message_contexts WHERE message_id=?',
        (mid,),
    ).fetchone()
    if dmc is None:
        raise MappingRejected(
            f'message_id {mid} missing daily_message_contexts',
            error_code='message_context_missing',
        )
    if (
        int(dmc['context_id']) != int(context_id)
        or int(dmc['context_epoch']) != int(context_epoch)
        or int(dmc['resident_generation']) != int(resident_generation)
    ):
        raise MappingRejected(
            f'message_id {mid} window identity mismatch',
            error_code='message_window_mismatch',
        )
    if str(dmc['role']) != want_role:
        raise MappingRejected(
            f'message_id {mid} role={dmc["role"]!r} expected {want_role!r}',
            error_code='message_role_mismatch',
        )


def _event_fields_equal(existing: Mapping[str, Any], planned: Mapping[str, Any]) -> bool:
    keys = (
        'message_id', 'role', 'claude_session_id',
        'context_id', 'context_epoch', 'resident_generation',
    )
    for key in keys:
        if key in ('message_id', 'context_id', 'context_epoch', 'resident_generation'):
            if int(existing[key]) != int(planned[key]):
                return False
        else:
            if str(existing[key]) != str(planned[key]):
                return False
    left_off = existing.get('jsonl_byte_offset')
    right_off = planned.get('jsonl_byte_offset')
    if left_off is None and right_off is None:
        return True
    if left_off is None or right_off is None:
        return False
    return int(left_off) == int(right_off)


def _insert_mapping_row_with_receipt(
    conn: sqlite3.Connection,
    planned: dict[str, Any],
) -> tuple[str, bool]:
    """Insert or confirm one mapping row, returning ``(uuid, inserted)``."""
    uid = str(planned['event_uuid'])
    existing = conn.execute(
        'SELECT * FROM chat_message_claude_events WHERE event_uuid=?',
        (uid,),
    ).fetchone()
    if existing is not None:
        if not _event_fields_equal(dict(existing), planned):
            raise MappingConflict(
                f'event_uuid {uid} bound to conflicting mapping',
                error_code='event_uuid_conflict',
            )
        return uid, False

    if str(planned['role']) == ROLE_USER:
        other = conn.execute(
            "SELECT event_uuid FROM chat_message_claude_events "
            "WHERE message_id=? AND role='user'",
            (int(planned['message_id']),),
        ).fetchone()
        if other is not None and str(other['event_uuid']) != uid:
            raise MappingConflict(
                f'message_id {planned["message_id"]} already has canonical user event',
                error_code='canonical_user_conflict',
            )

    try:
        conn.execute(
            '''INSERT INTO chat_message_claude_events (
                event_uuid, message_id, role, claude_session_id,
                context_id, context_epoch, resident_generation,
                jsonl_byte_offset, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)''',
            (
                uid,
                int(planned['message_id']),
                str(planned['role']),
                str(planned['claude_session_id']),
                int(planned['context_id']),
                int(planned['context_epoch']),
                int(planned['resident_generation']),
                planned.get('jsonl_byte_offset'),
                dc._now_local_str(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise MappingConflict(
            f'mapping integrity: {exc}', error_code='mapping_integrity',
        ) from exc
    return uid, True


def _insert_mapping_row(conn: sqlite3.Connection, planned: dict[str, Any]) -> str:
    """Insert or idempotently confirm one mapping row. Returns event_uuid."""
    uid, _inserted = _insert_mapping_row_with_receipt(conn, planned)
    return uid


def _assert_planned_event_sessions(
    events: Sequence[TranscriptEvent],
    *,
    registry_session_id: str,
) -> None:
    """Require every mapped event's JSONL sessionId to match Registry exactly."""
    expected = str(registry_session_id or '').strip()
    seen: set[str] = set()
    for event in events:
        sid = str(event.session_id or '').strip()
        if not sid:
            raise MappingRejected(
                f'event {event.event_uuid} missing sessionId',
                error_code='event_session_missing',
            )
        seen.add(sid)
    if len(seen) > 1:
        raise MappingRejected(
            f'multiple event sessions in range: {sorted(seen)}',
            error_code='multiple_event_sessions',
        )
    actual = next(iter(seen))
    if actual != expected:
        raise MappingRejected(
            f'event sessionId {actual!r} != registry {expected!r}',
            error_code='event_session_mismatch',
        )


def _assert_complete_terminal_round(graph: TranscriptGraph, rnd) -> None:
    """Refuse incomplete JSONL deltas so scan_offset cannot swallow late rows."""
    if not rnd.has_assistant:
        raise MappingRejected(
            'candidate round has no assistant',
            error_code='incomplete_round_no_assistant',
        )
    if not rnd.event_uuids:
        raise MappingRejected(
            'candidate round empty',
            error_code='incomplete_round_empty',
        )
    last_uid = rnd.event_uuids[-1]
    last_evt = graph.by_uuid[last_uid]
    if last_evt.event_role != EventRole.ASSISTANT or last_evt.is_sidechain:
        raise MappingRejected(
            'candidate round does not end on main-chain assistant',
            error_code='incomplete_round_not_terminal_assistant',
        )
    if not _content_has_text(last_evt):
        raise MappingRejected(
            'terminal assistant has no non-empty text body',
            error_code='incomplete_round_no_terminal_text',
        )


def _plan_mappings_for_graph(
    graph: TranscriptGraph,
    *,
    user_message_id: int,
    assistant_message_id: int,
    claude_session_id: str,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> list[dict[str, Any]]:
    candidates = [
        e for e in graph.events
        if e.event_role == EventRole.CANDIDATE_USER and not e.is_sidechain
    ]
    if len(candidates) == 0:
        raise MappingRejected(
            'no candidate_user in range', error_code='candidate_user_missing',
        )
    if len(candidates) > 1:
        raise MappingRejected(
            f'ambiguous candidate_user count={len(candidates)}',
            error_code='candidate_user_ambiguous',
        )
    if len(graph.candidate_rounds) != 1:
        raise MappingRejected(
            f'expected one candidate round, got {len(graph.candidate_rounds)}',
            error_code='candidate_round_ambiguous',
        )
    rnd = graph.candidate_rounds[0]
    user_uid = rnd.candidate_user_event_uuid
    if user_uid != candidates[0].event_uuid:
        raise MappingRejected(
            'candidate round user mismatch', error_code='candidate_round_mismatch',
        )

    _assert_complete_terminal_round(graph, rnd)

    planned: list[dict[str, Any]] = []
    planned_events: list[TranscriptEvent] = []
    for uid in rnd.event_uuids:
        event = graph.by_uuid[uid]
        is_user = uid == user_uid
        role = mapping_role_for_event(event, is_canonical_user=is_user)
        if role is None:
            continue
        message_id = int(user_message_id) if role == ROLE_USER else int(assistant_message_id)
        # Persist the event's own JSONL sessionId (never overwrite mismatch).
        event_sid = str(event.session_id or '').strip()
        planned.append({
            'event_uuid': uid,
            'message_id': message_id,
            'role': role,
            'claude_session_id': event_sid,
            'context_id': int(context_id),
            'context_epoch': int(context_epoch),
            'resident_generation': int(resident_generation),
            'jsonl_byte_offset': event.byte_offset,
        })
        planned_events.append(event)

    if not any(p['role'] == ROLE_USER for p in planned):
        raise MappingRejected(
            'canonical user mapping not planned', error_code='canonical_user_unplanned',
        )

    _assert_planned_event_sessions(
        planned_events, registry_session_id=claude_session_id,
    )
    return planned


def _verify_registry_identity(
    registry: dict[str, Any],
    req: MappingPassRequest,
    *,
    expected_start: int,
) -> None:
    if int(registry['context_epoch']) != int(req.context_epoch):
        raise MappingRejected('context_epoch mismatch', error_code='epoch_mismatch')
    if int(registry['resident_generation']) != int(req.resident_generation):
        raise MappingRejected(
            'resident_generation mismatch', error_code='generation_mismatch',
        )
    if str(registry['chat_id']) != str(req.chat_id).strip():
        raise MappingRejected('chat_id mismatch', error_code='chat_id_mismatch')
    reg_off = int(registry['scan_offset'])
    want = int(expected_start)
    if reg_off < want:
        raise MappingRejected(
            'scan_offset behind expected_start_offset',
            error_code=ERROR_MAPPING_LAG,
        )
    if reg_off > want:
        raise MappingRejected(
            'scan_offset ahead of expected_start_offset',
            error_code='scan_offset_cas_conflict',
        )


def _persist_blocked(
    *,
    context_id: int,
    resident_generation: int,
    error_code: str,
    expected_offset: int,
    db_path: Optional[str],
) -> Optional[dict[str, Any]]:
    """CAS-blocked write; on stale race return current snapshot without overwrite.

    When the failure is a true backlog (registry.scan_offset behind the turn
    start), mark BLOCKED at the *actual* registry offset so callers never see
    READY + stale prefix after a confirmed lag.
    """
    code = str(error_code)
    cas_offset = int(expected_offset)
    if code in {
        ERROR_MAPPING_LAG,
        ERROR_CATCHUP_REQUIRED,
        ERROR_CATCHUP_FAILED,
        ERROR_CATCHUP_INCOMPLETE_UNALIGNED,
        ERROR_CATCHUP_AUTH_MISMATCH,
        'scan_offset_cas_conflict',
    }:
        current = get_context_claude_session(
            int(context_id), int(resident_generation), db_path=db_path,
        )
        if current is not None:
            reg_off = int(current['scan_offset'])
            if reg_off < int(expected_offset):
                cas_offset = reg_off
                if code == 'scan_offset_cas_conflict':
                    code = ERROR_MAPPING_LAG
    try:
        return mark_scan_blocked(
            context_id=int(context_id),
            resident_generation=int(resident_generation),
            error_code=code,
            expected_offset=int(cas_offset),
            db_path=db_path,
        )
    except SessionRegistryConflict:
        return get_context_claude_session(
            int(context_id), int(resident_generation), db_path=db_path,
        )
    except SessionRegistryError:
        return get_context_claude_session(
            int(context_id), int(resident_generation), db_path=db_path,
        )


def _plan_one_round_from_graph(
    graph: TranscriptGraph,
    rnd: CandidateConversationRound,
    *,
    user_message_id: int,
    assistant_message_id: int,
    claude_session_id: str,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> list[dict[str, Any]]:
    """Plan mappings for one already-selected candidate round."""
    _assert_complete_terminal_round(graph, rnd)
    user_uid = rnd.candidate_user_event_uuid
    planned: list[dict[str, Any]] = []
    planned_events: list[TranscriptEvent] = []
    for uid in rnd.event_uuids:
        event = graph.by_uuid[uid]
        is_user = uid == user_uid
        role = mapping_role_for_event(event, is_canonical_user=is_user)
        if role is None:
            continue
        message_id = int(user_message_id) if role == ROLE_USER else int(assistant_message_id)
        event_sid = str(event.session_id or '').strip()
        planned.append({
            'event_uuid': uid,
            'message_id': message_id,
            'role': role,
            'claude_session_id': event_sid,
            'context_id': int(context_id),
            'context_epoch': int(context_epoch),
            'resident_generation': int(resident_generation),
            'jsonl_byte_offset': event.byte_offset,
        })
        planned_events.append(event)

    if not any(p['role'] == ROLE_USER for p in planned):
        raise MappingRejected(
            'canonical user mapping not planned', error_code='canonical_user_unplanned',
        )
    _assert_planned_event_sessions(
        planned_events, registry_session_id=claude_session_id,
    )
    return planned


def _phase1_plan(
    req: MappingPassRequest,
    *,
    db_path: Optional[str],
    start: int,
    end: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read Registry + JSONL and plan mappings. No SQLite write transaction."""
    registry = get_context_claude_session(
        int(req.context_id), int(req.resident_generation), db_path=db_path,
    )
    if registry is None:
        raise MappingRejected('registry missing', error_code='registry_missing')
    _verify_registry_identity(registry, req, expected_start=start)

    transcript_path = str(registry['transcript_path'])
    sid = str(registry['claude_session_id'])
    try:
        size = os.path.getsize(transcript_path)
    except OSError as exc:
        raise MappingRejected(
            f'transcript unreadable: {exc}', error_code='transcript_unreadable',
        ) from exc
    if size < int(registry['scan_offset']) or size < start:
        raise MappingRejected(
            'transcript truncated below registered offset',
            error_code='transcript_truncated',
        )
    if size < end:
        raise MappingRejected(
            'transcript shorter than observed_end_offset',
            error_code='transcript_short',
        )

    try:
        graph = read_transcript_range(transcript_path, start, end)
    except TranscriptReaderError as exc:
        code = exc.code.value if isinstance(exc.code, ReaderErrorCode) else 'reader_error'
        raise MappingRejected(str(exc), error_code=code) from exc

    planned = _plan_mappings_for_graph(
        graph,
        user_message_id=int(req.user_message_id),
        assistant_message_id=int(req.assistant_message_id),
        claude_session_id=sid,
        context_id=int(req.context_id),
        context_epoch=int(req.context_epoch),
        resident_generation=int(req.resident_generation),
    )
    return dict(registry), planned


def _phase1_plan_catchup(
    req: MappingPassRequest,
    *,
    db_path: Optional[str],
    registry: dict[str, Any],
    require_reach_end: bool = True,
) -> tuple[dict[str, Any], list[tuple[int, int, list[dict[str, Any]]]]]:
    """Plan multi-round catch-up from registry.scan_offset → observed_end.

    Returns (registry, work_items) where each work item is
    ``(cas_expected_offset, new_offset, planned_rows)``.
    Incomplete transcript/auth pairs are skipped only when their exclusive
    end boundary is proven by a following candidate_user (or range end with
    a matching incomplete auth round).

    When ``require_reach_end`` is False (Forge/Swap watermark catch-up), stop
    after authoritative rounds are satisfied even if the file has newer bytes.
    """
    start = int(registry['scan_offset'])
    end = int(req.observed_end_offset)
    if end < start:
        raise MappingRejected(
            'catch-up end before registry scan_offset',
            error_code='offset_invalid',
        )

    transcript_path = str(registry['transcript_path'])
    sid = str(registry['claude_session_id'])
    try:
        size = os.path.getsize(transcript_path)
    except OSError as exc:
        raise MappingRejected(
            f'transcript unreadable: {exc}', error_code='transcript_unreadable',
        ) from exc
    if size < start or size < end:
        raise MappingRejected(
            'transcript truncated/short for catch-up',
            error_code='transcript_short',
        )

    try:
        graph = read_transcript_range(transcript_path, start, end)
    except TranscriptReaderError as exc:
        code = exc.code.value if isinstance(exc.code, ReaderErrorCode) else 'reader_error'
        raise MappingRejected(str(exc), error_code=code) from exc

    conn = dc._connect(db_path)
    try:
        auth_rounds = _load_auth_rounds_after(
            conn,
            context_id=int(req.context_id),
            context_epoch=int(req.context_epoch),
            resident_generation=int(req.resident_generation),
            after_message_id=(
                int(registry['last_mapped_message_id'])
                if registry.get('last_mapped_message_id') is not None
                else None
            ),
            through_assistant_id=int(req.assistant_message_id),
        )
    finally:
        conn.close()

    if not auth_rounds:
        raise MappingRejected(
            'catch-up has no authoritative rounds',
            error_code=ERROR_CATCHUP_AUTH_MISMATCH,
        )
    last = auth_rounds[-1]
    if (
        int(last.user_message_id) != int(req.user_message_id)
        or int(last.assistant_message_id) != int(req.assistant_message_id)
        or last.incomplete
    ):
        raise MappingRejected(
            'catch-up final auth round != current complete turn',
            error_code=ERROR_CATCHUP_AUTH_MISMATCH,
        )

    rounds = list(graph.candidate_rounds)
    work: list[tuple[int, int, list[dict[str, Any]]]] = []
    auth_i = 0
    cursor = start

    for idx, rnd in enumerate(rounds):
        if auth_i >= len(auth_rounds):
            if require_reach_end:
                raise MappingRejected(
                    'extra complete transcript round without auth',
                    error_code=ERROR_CATCHUP_AUTH_MISMATCH,
                )
            break

        round_end = _round_byte_end(graph, rounds, idx, end)
        if round_end < cursor:
            raise MappingRejected(
                'catch-up round end before cursor',
                error_code=ERROR_CATCHUP_FAILED,
            )
        complete = _is_complete_terminal_round(graph, rnd)
        if not complete:
            boundary_clear = (idx + 1 < len(rounds)) or (round_end == end)
            if not boundary_clear:
                raise MappingRejected(
                    'incomplete backlog segment boundary unknown',
                    error_code=ERROR_CATCHUP_INCOMPLETE_UNALIGNED,
                )
            auth = auth_rounds[auth_i]
            if not auth.incomplete:
                raise MappingRejected(
                    'incomplete transcript vs complete auth round',
                    error_code=ERROR_CATCHUP_INCOMPLETE_UNALIGNED,
                )
            if round_end > cursor:
                work.append((cursor, round_end, []))
            cursor = round_end
            auth_i += 1
            continue

        auth = auth_rounds[auth_i]
        if auth.incomplete:
            raise MappingRejected(
                'complete transcript vs incomplete auth round',
                error_code=ERROR_CATCHUP_INCOMPLETE_UNALIGNED,
            )
        planned = _plan_one_round_from_graph(
            graph,
            rnd,
            user_message_id=int(auth.user_message_id),
            assistant_message_id=int(auth.assistant_message_id),
            claude_session_id=sid,
            context_id=int(req.context_id),
            context_epoch=int(req.context_epoch),
            resident_generation=int(req.resident_generation),
        )
        work.append((cursor, round_end, planned))
        cursor = round_end
        auth_i += 1

    if auth_i != len(auth_rounds):
        raise MappingRejected(
            'authoritative rounds remain after transcript catch-up',
            error_code=ERROR_CATCHUP_AUTH_MISMATCH,
        )
    if require_reach_end and cursor != end:
        if cursor < end and not any(
            e.byte_offset is not None and int(e.byte_offset) >= cursor
            and e.event_role == EventRole.CANDIDATE_USER and not e.is_sidechain
            for e in graph.events
        ):
            work.append((cursor, end, []))
            cursor = end
        else:
            raise MappingRejected(
                'catch-up did not reach observed_end_offset',
                error_code=ERROR_CATCHUP_FAILED,
            )
    return dict(registry), work


def _commit_mapping_work(
    req: MappingPassRequest,
    *,
    db_path: Optional[str],
    phase1_registry: dict[str, Any],
    work: Sequence[tuple[int, int, list[dict[str, Any]]]],
) -> MappingPassResult:
    """Phase-2 write: apply planned rows + CAS advances in order."""
    conn = dc._connect(db_path)
    registry_snapshot: Optional[dict[str, Any]] = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        registry = get_context_claude_session(
            int(req.context_id), int(req.resident_generation), conn=conn,
        )
        if registry is None:
            raise MappingRejected('registry missing', error_code='registry_missing')
        registry_snapshot = dict(registry)

        if str(registry['transcript_path']) != str(phase1_registry['transcript_path']):
            raise MappingRejected('transcript_path changed', error_code='registry_changed')
        if str(registry['claude_session_id']) != str(phase1_registry['claude_session_id']):
            raise MappingRejected(
                'registry session changed between phases',
                error_code='registry_changed',
            )
        if int(registry['context_epoch']) != int(req.context_epoch):
            raise MappingRejected('context_epoch mismatch', error_code='epoch_mismatch')
        if int(registry['resident_generation']) != int(req.resident_generation):
            raise MappingRejected(
                'resident_generation mismatch', error_code='generation_mismatch',
            )
        if str(registry['chat_id']) != str(req.chat_id).strip():
            raise MappingRejected('chat_id mismatch', error_code='chat_id_mismatch')

        mapped: list[str] = []
        inserted: list[str] = []
        confirmed_existing: list[str] = []
        final_offset = int(registry['scan_offset'])
        last_mapped_asst: Optional[int] = (
            int(registry['last_mapped_message_id'])
            if registry.get('last_mapped_message_id') is not None
            else None
        )

        for expected_off, new_off, planned in work:
            if int(registry['scan_offset']) != int(expected_off):
                raise MappingRejected(
                    'scan_offset moved during catch-up',
                    error_code='scan_offset_cas_conflict',
                )
            for row in planned:
                if str(row['claude_session_id']) != str(registry['claude_session_id']):
                    raise MappingRejected(
                        'planned event session != registry',
                        error_code='event_session_mismatch',
                    )
                if str(row['role']) == ROLE_USER:
                    _assert_message_window(
                        conn, int(row['message_id']),
                        context_id=int(req.context_id),
                        context_epoch=int(req.context_epoch),
                        resident_generation=int(req.resident_generation),
                        expected_role=ROLE_USER,
                    )
                elif str(row['role']) == ROLE_ASSISTANT:
                    _assert_message_window(
                        conn, int(row['message_id']),
                        context_id=int(req.context_id),
                        context_epoch=int(req.context_epoch),
                        resident_generation=int(req.resident_generation),
                        expected_role=ROLE_ASSISTANT,
                    )
                uid, was_inserted = _insert_mapping_row_with_receipt(conn, row)
                mapped.append(uid)
                if was_inserted:
                    inserted.append(uid)
                else:
                    confirmed_existing.append(uid)
                if str(row['role']) == ROLE_ASSISTANT:
                    last_mapped_asst = int(row['message_id'])

            cas_advance_scan_offset(
                context_id=int(req.context_id),
                resident_generation=int(req.resident_generation),
                expected_offset=int(expected_off),
                new_offset=int(new_off),
                last_mapped_message_id=last_mapped_asst,
                scan_status=SCAN_STATUS_READY,
                scan_error_code=None,
                conn=conn,
            )
            registry = get_context_claude_session(
                int(req.context_id), int(req.resident_generation), conn=conn,
            )
            if registry is None:
                raise MappingRejected('registry missing', error_code='registry_missing')
            final_offset = int(new_off)

        receipt_id: Optional[int] = None
        if req.terminal_receipt is not None:
            metadata = dict(req.terminal_receipt)
            receipt_id = dc.insert_terminal_mapping_receipt(
                conn=conn,
                assistant_message_id=int(req.assistant_message_id),
                user_message_id=int(req.user_message_id),
                context_id=int(req.context_id),
                context_epoch=int(req.context_epoch),
                resident_generation=int(req.resident_generation),
                expected_cursor=metadata.get('expected_cursor'),
                transcript_path=str(metadata.get('transcript_path') or ''),
                claude_session_id=str(metadata.get('claude_session_id') or ''),
                transcript_start_offset=metadata.get('transcript_start_offset'),
                transcript_end_offset=metadata.get('transcript_end_offset'),
                pre_registry_snapshot=metadata.get('pre_registry_snapshot'),
                post_registry_snapshot=registry,
                inserted_event_uuids=inserted,
                confirmed_existing_event_uuids=confirmed_existing,
                mapped_event_uuids=mapped,
            )
        conn.commit()
        final_reg = get_context_claude_session(
            int(req.context_id), int(req.resident_generation), db_path=db_path,
        )
        return MappingPassResult(
            ok=True,
            mapped_event_uuids=mapped,
            inserted_event_uuids=inserted,
            confirmed_existing_event_uuids=confirmed_existing,
            scan_offset=final_offset,
            registry=final_reg,
            terminal_receipt_id=receipt_id,
        )
    except (MappingRejected, MappingConflict, SessionRegistryConflict, SessionRegistryNotFound) as exc:
        conn.rollback()
        code = getattr(exc, 'error_code', 'mapping_rejected')
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code=str(code),
            expected_offset=int(req.expected_start_offset),
            db_path=db_path,
        )
        return MappingPassResult(
            ok=False,
            error_code=str(code),
            registry=reg or registry_snapshot,
        )
    except SessionRegistryError as exc:
        conn.rollback()
        code = exc.error_code
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code=str(code),
            expected_offset=int(req.expected_start_offset),
            db_path=db_path,
        )
        return MappingPassResult(
            ok=False,
            error_code=str(code),
            registry=reg or registry_snapshot,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_mapping_pass(
    request: MappingPassRequest,
    *,
    db_path: Optional[str] = None,
) -> MappingPassResult:
    """Single offline mapping pass over an explicit JSONL byte range.

    Phase 1 (no write lock): Registry snapshot, file size, Reader, plan.
    Phase 2 (BEGIN IMMEDIATE): re-check identity/offset/roles, write, CAS.

    When ``registry.scan_offset < expected_start_offset``, attempts synchronous
    multi-round catch-up from the Registry watermark through
    ``observed_end_offset`` instead of permanently poisoning later turns.
    """
    dc.ensure_schema(db_path)
    req = request
    start = int(req.expected_start_offset)
    end = int(req.observed_end_offset)
    registry_snapshot: Optional[dict[str, Any]] = None

    if start < 0 or end < start:
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code='offset_invalid',
            expected_offset=max(start, 0),
            db_path=db_path,
        )
        return MappingPassResult(ok=False, error_code='offset_invalid', registry=reg)

    # ----- Detect backlog vs fast path -----
    pre_reg = get_context_claude_session(
        int(req.context_id), int(req.resident_generation), db_path=db_path,
    )
    if pre_reg is None:
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code='registry_missing',
            expected_offset=start,
            db_path=db_path,
        )
        return MappingPassResult(ok=False, error_code='registry_missing', registry=reg)

    registry_snapshot = dict(pre_reg)
    reg_off = int(pre_reg['scan_offset'])

    if reg_off < start:
        # DEFECT A: backlog catch-up (authoritative start = registry watermark)
        try:
            phase1_registry, work = _phase1_plan_catchup(
                req,
                db_path=db_path,
                registry=dict(pre_reg),
                require_reach_end=True,
            )
        except (MappingRejected, MappingConflict) as exc:
            code = getattr(exc, 'error_code', ERROR_CATCHUP_FAILED)
            if str(code) == ERROR_MAPPING_LAG:
                code = ERROR_CATCHUP_FAILED
            reg = _persist_blocked(
                context_id=req.context_id,
                resident_generation=req.resident_generation,
                error_code=str(code),
                expected_offset=start,
                db_path=db_path,
            )
            return MappingPassResult(
                ok=False,
                error_code=str(code),
                registry=reg or registry_snapshot,
            )
        return _commit_mapping_work(
            req,
            db_path=db_path,
            phase1_registry=phase1_registry,
            work=work,
        )

    if reg_off > start:
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code='scan_offset_cas_conflict',
            expected_offset=start,
            db_path=db_path,
        )
        return MappingPassResult(
            ok=False,
            error_code='scan_offset_cas_conflict',
            registry=reg or registry_snapshot,
        )

    # ----- Fast path: registry.scan_offset == turn start -----
    phase1_registry: Optional[dict[str, Any]] = None
    try:
        phase1_registry, planned = _phase1_plan(
            req, db_path=db_path, start=start, end=end,
        )
        registry_snapshot = dict(phase1_registry)
    except (MappingRejected, MappingConflict) as exc:
        code = getattr(exc, 'error_code', 'mapping_rejected')
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code=str(code),
            expected_offset=start,
            db_path=db_path,
        )
        return MappingPassResult(
            ok=False,
            error_code=str(code),
            registry=reg or registry_snapshot,
        )

    return _commit_mapping_work(
        req,
        db_path=db_path,
        phase1_registry=phase1_registry,
        work=[(start, end, planned)],
    )


def latest_complete_assistant_watermark(
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    before_message_id: Optional[int] = None,
    db_path: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[int]:
    """Latest complete/formal assistant id in-window (excludes partial_rescue)."""
    own = conn is None
    c = conn or dc._connect(db_path)
    try:
        cols = {
            str(r[1]) for r in c.execute('PRAGMA table_info(chat_messages)').fetchall()
        }
        cache_sel = 'm.cache_info' if 'cache_info' in cols else "'' AS cache_info"
        params: list[Any] = [
            int(context_id), int(context_epoch), int(resident_generation),
        ]
        before_sql = ''
        if before_message_id is not None:
            before_sql = ' AND m.id < ?'
            params.append(int(before_message_id))
        rows = c.execute(
            f'''SELECT m.id AS id, {cache_sel}
                FROM daily_message_contexts dmc
                JOIN chat_messages m ON m.id = dmc.message_id
                WHERE dmc.context_id=? AND dmc.context_epoch=?
                  AND dmc.resident_generation=?
                  AND dmc.role='assistant'{before_sql}
                ORDER BY m.id DESC''',
            tuple(params),
        ).fetchall()
        for row in rows:
            if _assistant_row_incomplete(row['cache_info']):
                continue
            return int(row['id'])
        return None
    finally:
        if own:
            c.close()


def registry_mapping_lags_watermark(
    registry: Optional[Mapping[str, Any]],
    watermark_assistant_id: Optional[int],
) -> bool:
    """True when Registry last_mapped is behind authoritative complete watermark."""
    if watermark_assistant_id is None:
        return False
    if registry is None:
        return True
    mapped = registry.get('last_mapped_message_id')
    if mapped is None:
        return True
    return int(mapped) < int(watermark_assistant_id)


def attempt_mapping_catchup_through(
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    chat_id: str,
    through_assistant_id: int,
    through_user_message_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> MappingPassResult:
    """Best-effort catch-up through an authoritative assistant watermark.

    Used by Capacity Swap / Manual Forge before consuming mapped prefix.
    Does not invent provider results; fail-closed on unalignable backlog.
    """
    dc.ensure_schema(db_path)
    registry = get_context_claude_session(
        int(context_id), int(resident_generation), db_path=db_path,
    )
    if registry is None:
        return MappingPassResult(ok=False, error_code='registry_missing')

    conn = dc._connect(db_path)
    try:
        if through_user_message_id is None:
            row = conn.execute(
                '''SELECT message_id FROM daily_message_contexts
                   WHERE context_id=? AND context_epoch=? AND resident_generation=?
                     AND role='user' AND message_id < ?
                   ORDER BY message_id DESC LIMIT 1''',
                (
                    int(context_id), int(context_epoch), int(resident_generation),
                    int(through_assistant_id),
                ),
            ).fetchone()
            if row is None:
                return MappingPassResult(
                    ok=False,
                    error_code=ERROR_CATCHUP_AUTH_MISMATCH,
                    registry=dict(registry),
                )
            user_mid = int(row['message_id'])
        else:
            user_mid = int(through_user_message_id)
    finally:
        conn.close()

    path = str(registry.get('transcript_path') or '')
    try:
        end = os.path.getsize(path) if path else 0
    except OSError:
        return MappingPassResult(
            ok=False,
            error_code='transcript_unreadable',
            registry=dict(registry),
        )

    if (
        registry.get('last_mapped_message_id') is not None
        and int(registry['last_mapped_message_id']) >= int(through_assistant_id)
    ):
        return MappingPassResult(
            ok=True,
            scan_offset=int(registry['scan_offset']),
            registry=dict(registry),
        )

    # Force lag path when registry is behind file end; catch-up starts at
    # registry.scan_offset and may stop once the watermark auth rounds are done.
    if end > int(registry['scan_offset']):
        req = MappingPassRequest(
            context_id=int(context_id),
            context_epoch=int(context_epoch),
            resident_generation=int(resident_generation),
            chat_id=str(chat_id),
            user_message_id=int(user_mid),
            assistant_message_id=int(through_assistant_id),
            expected_start_offset=int(end),
            observed_end_offset=int(end),
        )
        try:
            phase1_registry, work = _phase1_plan_catchup(
                req,
                db_path=db_path,
                registry=dict(registry),
                require_reach_end=False,
            )
        except (MappingRejected, MappingConflict) as exc:
            code = getattr(exc, 'error_code', ERROR_CATCHUP_FAILED)
            reg = _persist_blocked(
                context_id=int(context_id),
                resident_generation=int(resident_generation),
                error_code=str(code),
                expected_offset=int(end),
                db_path=db_path,
            )
            return MappingPassResult(
                ok=False,
                error_code=str(code),
                registry=reg or dict(registry),
            )
        if not work:
            return MappingPassResult(
                ok=True,
                scan_offset=int(registry['scan_offset']),
                registry=dict(registry),
            )
        return _commit_mapping_work(
            req,
            db_path=db_path,
            phase1_registry=phase1_registry,
            work=work,
        )

    # File has no unmapped bytes but watermark still lags — fail closed.
    reg = _persist_blocked(
        context_id=int(context_id),
        resident_generation=int(resident_generation),
        error_code=ERROR_MAPPING_LAG,
        expected_offset=int(end) + 1,
        db_path=db_path,
    )
    return MappingPassResult(
        ok=False,
        error_code=ERROR_MAPPING_LAG,
        registry=reg or dict(registry),
    )


def get_user_canonical_by_event_uuid(
    event_uuids: Sequence[str],
    *,
    db_path: Optional[str] = None,
) -> dict[str, str]:
    """Return ``{event_uuid: chat_messages.content}`` for mapped canonical users only.

    Unmapped JSONL user rows are never included. Content is read from
    ``chat_messages`` (no duplicated canonical body column).
    """
    uids = [str(u).strip() for u in event_uuids if str(u or '').strip()]
    if not uids:
        return {}
    dc.ensure_schema(db_path)
    conn = dc._connect(db_path)
    try:
        placeholders = ','.join('?' for _ in uids)
        rows = conn.execute(
            f'''SELECT e.event_uuid AS event_uuid, m.content AS content
                FROM chat_message_claude_events e
                JOIN chat_messages m ON m.id = e.message_id
                WHERE e.role = 'user' AND e.event_uuid IN ({placeholders})''',
            tuple(uids),
        ).fetchall()
        return {str(r['event_uuid']): str(r['content'] or '') for r in rows}
    finally:
        conn.close()

