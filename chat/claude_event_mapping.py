"""Async message ↔ Claude JSONL event Mapping (offline single-pass entry).

Flag-off data layer. No background threads. Runtime may call
``run_mapping_pass`` later; this module never starts processes or models.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from chat import daily_context as dc
from chat.claude_transcript_model import EventRole, TranscriptEvent, TranscriptGraph
from chat.claude_transcript_reader import (
    ReaderErrorCode,
    TranscriptReaderError,
    read_transcript_range,
)
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
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


@dataclass
class MappingPassResult:
    ok: bool
    error_code: Optional[str] = None
    mapped_event_uuids: list[str] = field(default_factory=list)
    scan_offset: Optional[int] = None
    registry: Optional[dict[str, Any]] = None


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
    # SYSTEM / SUMMARY / META / UNKNOWN / unconfirmed CANDIDATE_USER — skip
    return None


def _assert_message_window(
    conn: sqlite3.Connection,
    message_id: int,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
) -> None:
    mid = int(message_id)
    row = conn.execute(
        'SELECT id FROM chat_messages WHERE id=?', (mid,),
    ).fetchone()
    if row is None:
        raise MappingRejected(
            f'message_id {mid} not in chat_messages',
            error_code='message_missing',
        )
    dmc = conn.execute(
        'SELECT context_id, context_epoch, resident_generation '
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


def _insert_mapping_row(conn: sqlite3.Connection, planned: dict[str, Any]) -> str:
    """Insert or idempotently confirm one mapping row. Returns event_uuid."""
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
        return uid

    # Canonical user: reject if message already has a different user event.
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
    return uid


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

    planned: list[dict[str, Any]] = []
    for uid in rnd.event_uuids:
        event = graph.by_uuid[uid]
        is_user = uid == user_uid
        role = mapping_role_for_event(event, is_canonical_user=is_user)
        if role is None:
            continue
        message_id = int(user_message_id) if role == ROLE_USER else int(assistant_message_id)
        planned.append({
            'event_uuid': uid,
            'message_id': message_id,
            'role': role,
            'claude_session_id': claude_session_id,
            'context_id': int(context_id),
            'context_epoch': int(context_epoch),
            'resident_generation': int(resident_generation),
            'jsonl_byte_offset': event.byte_offset,
        })
    if not any(p['role'] == ROLE_USER for p in planned):
        raise MappingRejected(
            'canonical user mapping not planned', error_code='canonical_user_unplanned',
        )
    return planned


def _persist_blocked(
    *,
    context_id: int,
    resident_generation: int,
    error_code: str,
    db_path: Optional[str],
) -> Optional[dict[str, Any]]:
    try:
        return mark_scan_blocked(
            context_id=int(context_id),
            resident_generation=int(resident_generation),
            error_code=error_code,
            db_path=db_path,
        )
    except SessionRegistryError:
        return None


def run_mapping_pass(
    request: MappingPassRequest,
    *,
    db_path: Optional[str] = None,
) -> MappingPassResult:
    """Single offline mapping pass over an explicit JSONL byte range.

    All mapping writes + scan_offset CAS happen in one DB transaction.
    On failure: mapping batch rolled back, offset not advanced, BLOCKED persisted.
    """
    dc.ensure_schema(db_path)
    req = request
    start = int(req.expected_start_offset)
    end = int(req.observed_end_offset)
    if start < 0 or end < start:
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code='offset_invalid',
            db_path=db_path,
        )
        return MappingPassResult(ok=False, error_code='offset_invalid', registry=reg)

    conn = dc._connect(db_path)
    planned: list[dict[str, Any]] = []
    registry_snapshot: Optional[dict[str, Any]] = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        registry = get_context_claude_session(
            int(req.context_id), int(req.resident_generation), conn=conn,
        )
        if registry is None:
            raise MappingRejected('registry missing', error_code='registry_missing')
        registry_snapshot = dict(registry)

        if int(registry['context_epoch']) != int(req.context_epoch):
            raise MappingRejected('context_epoch mismatch', error_code='epoch_mismatch')
        if int(registry['resident_generation']) != int(req.resident_generation):
            raise MappingRejected(
                'resident_generation mismatch', error_code='generation_mismatch',
            )
        if str(registry['chat_id']) != str(req.chat_id).strip():
            raise MappingRejected('chat_id mismatch', error_code='chat_id_mismatch')
        if int(registry['scan_offset']) != start:
            raise MappingRejected(
                'scan_offset != expected_start_offset',
                error_code='scan_offset_cas_conflict',
            )

        transcript_path = str(registry['transcript_path'])
        sid = str(registry['claude_session_id'])
        try:
            size = os.path.getsize(transcript_path)
        except OSError as exc:
            raise MappingRejected(
                f'transcript unreadable: {exc}', error_code='transcript_unreadable',
            ) from exc
        # Truncation / rewind refused — never auto-seek to file head.
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

        _assert_message_window(
            conn, int(req.user_message_id),
            context_id=int(req.context_id),
            context_epoch=int(req.context_epoch),
            resident_generation=int(req.resident_generation),
        )
        _assert_message_window(
            conn, int(req.assistant_message_id),
            context_id=int(req.context_id),
            context_epoch=int(req.context_epoch),
            resident_generation=int(req.resident_generation),
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

        mapped: list[str] = []
        for row in planned:
            mapped.append(_insert_mapping_row(conn, row))

        cas_advance_scan_offset(
            context_id=int(req.context_id),
            resident_generation=int(req.resident_generation),
            expected_offset=start,
            new_offset=end,
            last_mapped_message_id=int(req.assistant_message_id),
            scan_status=SCAN_STATUS_READY,
            scan_error_code=None,
            conn=conn,
        )
        conn.commit()
        final_reg = get_context_claude_session(
            int(req.context_id), int(req.resident_generation), db_path=db_path,
        )
        return MappingPassResult(
            ok=True,
            mapped_event_uuids=mapped,
            scan_offset=end,
            registry=final_reg,
        )
    except (MappingRejected, MappingConflict, SessionRegistryConflict, SessionRegistryNotFound) as exc:
        conn.rollback()
        code = getattr(exc, 'error_code', 'mapping_rejected')
        reg = _persist_blocked(
            context_id=req.context_id,
            resident_generation=req.resident_generation,
            error_code=str(code),
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
