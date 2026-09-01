"""Context Window v0.2 — Preview / dry-run (read-only candidate preview).

Flag-off. Never writes intents, Registry, Mapping, JSONL, cursor, or resident.
Uses SQLite ``mode=ro`` + ``PRAGMA query_only=ON`` only.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from chat.claude_transcript_model import (
    SidechainPolicy,
    SummaryPolicy,
    ThinkingPolicy,
    UnknownEventPolicy,
)
from chat.claude_transcript_reader import TranscriptReaderError, read_transcript_range
from chat.claude_transcript_transform import (
    TransformError,
    TransformRequest,
    serialize_events,
    sha256_text,
    transform_transcript,
)
from chat.claude_transcript_validator import ValidatorOptions, validate_transcript_events
from chat.context_window import (
    ACTIVE_INTENT_STATUSES,
    NoOpenContextWindowError,
    StaleSourceContextError,
    SwitchInProgressError,
    WindowBusyError,
    _collect_context_formal_messages,
    _find_latest_legacy_bootstrap_conn,
    _find_open_manual_window_conn,
    _is_resident_turn_active_conn,
    _last_formal_message_id,
    _select_rounds_locked,
    _shanghai_now,
)
from chat.daily_context import DEFAULT_CHAT_ID
from chat.session_registry import SCAN_STATUS_READY

logger = logging.getLogger(__name__)

PREVIEW_STATUS_READY = 'READY'
PREVIEW_STATUS_NATIVE_COLD = 'NATIVE_COLD'
PREVIEW_STATUS_BLOCKED = 'BLOCKED'

_MAPPING_ASSISTANT_ROLES = frozenset({'assistant', 'tool_use', 'tool_result_user'})
_PREFIX_READ_CHUNK = 65536


class PreviewError(Exception):
    """Transport / gate error for Preview HTTP mapping."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        http_status: int = 400,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = int(http_status)
        self.retryable = bool(retryable)


class PreviewDbUnavailableError(PreviewError):
    def __init__(self, message: str = 'preview database unavailable'):
        super().__init__(
            message, error_code='preview_db_unavailable', http_status=503,
        )


class _PrefixSnapshotError(Exception):
    """Internal filesystem/prefix failure; never expose path text."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TranscriptPrefixSnapshot:
    end_offset: int
    sha256: str


@dataclass(frozen=True)
class PreparedPreviewCandidate:
    preview_id: str
    candidate_session_id: str

    source_context_id: int
    source_context_epoch: int
    source_version: int
    source_resident_generation: int
    source_boundary_message_id: int

    source_transcript_path: Optional[str]
    source_scan_offset: int
    source_prefix_sha256: Optional[str]

    requested_round_count: int
    selected_round_count: int
    selected_message_ids: tuple[int, ...]

    thinking_policy: ThinkingPolicy

    serialized_jsonl: bytes
    output_sha256: str
    serialized_bytes: int
    event_count: int
    proof_kind: str


@dataclass(frozen=True)
class CandidatePreparation:
    response: dict[str, Any]
    artifact: Optional[PreparedPreviewCandidate]


def _open_preview_db_readonly(db_path: str) -> sqlite3.Connection:
    """Open SQLite in true read-only mode for Preview dry-run."""
    path = os.path.abspath(str(db_path or ''))
    if not path or not os.path.isfile(path):
        raise PreviewDbUnavailableError('preview database file missing')
    uri = Path(path).as_uri() + '?mode=ro'
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise PreviewDbUnavailableError(str(exc)) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
    except sqlite3.Error as exc:
        conn.close()
        raise PreviewDbUnavailableError(str(exc)) from exc
    return conn


def _close_preview_readonly(conn: Optional[sqlite3.Connection]) -> None:
    """Best-effort ROLLBACK + close; idempotent for None/closed connections."""
    if conn is None:
        return
    try:
        conn.execute('ROLLBACK')
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _snapshot_transcript_prefix(
    path: str,
    end_offset: int,
) -> TranscriptPrefixSnapshot:
    """Hash exact ``[0, end_offset)`` without following symlinks or reading past offset."""
    if not isinstance(end_offset, int) or isinstance(end_offset, bool) or end_offset <= 0:
        raise _PrefixSnapshotError('PREVIEW_REGISTRY_SCAN_OFFSET_INVALID')
    flags = os.O_RDONLY
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        logger.info('preview prefix open failed: %s', getattr(exc, 'errno', type(exc).__name__))
        raise _PrefixSnapshotError('PREVIEW_TRANSCRIPT_UNREADABLE') from None
    try:
        st = os.fstat(fd)
        if int(st.st_size) < end_offset:
            raise _PrefixSnapshotError('PREVIEW_TRANSCRIPT_TRUNCATED')
        digest = hashlib.sha256()
        remaining = end_offset
        while remaining > 0:
            chunk = os.read(fd, min(_PREFIX_READ_CHUNK, remaining))
            if not chunk:
                raise _PrefixSnapshotError('PREVIEW_TRANSCRIPT_UNREADABLE')
            digest.update(chunk)
            remaining -= len(chunk)
        return TranscriptPrefixSnapshot(end_offset=end_offset, sha256=digest.hexdigest())
    except _PrefixSnapshotError:
        raise
    except OSError as exc:
        logger.info('preview prefix read failed: %s', getattr(exc, 'errno', type(exc).__name__))
        raise _PrefixSnapshotError('PREVIEW_TRANSCRIPT_UNREADABLE') from None
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _parse_registry_scan_offset(registry: dict[str, Any]) -> Optional[int]:
    """Safe scan_offset parse. Invalid / non-positive → None (never raises)."""
    try:
        parsed = int(registry.get('scan_offset'))
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _resolve_canonical_context_readonly_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str = DEFAULT_CHAT_ID,
) -> dict[str, Any]:
    """Resolve the single open canonical window without bootstrap writes."""
    manual = _find_open_manual_window_conn(conn, chat_id)
    if manual is not None:
        return manual
    legacy = _find_latest_legacy_bootstrap_conn(conn, chat_id)
    if legacy is not None:
        return legacy
    any_count = int(conn.execute(
        'SELECT COUNT(*) FROM daily_contexts WHERE chat_id=?', (chat_id,),
    ).fetchone()[0])
    if any_count > 0:
        raise NoOpenContextWindowError('no_open_context_window')
    raise NoOpenContextWindowError('no_open_context_window')


def _has_active_switch_intent_conn(
    conn: sqlite3.Connection,
    chat_id: str,
    *,
    allowed_switch_request_id: Optional[str] = None,
) -> bool:
    """True if an active switch intent blocks Preview.

    When ``allowed_switch_request_id`` is set, that exact request_id is ignored;
    any *other* active intent still blocks.
    """
    tables = {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if 'context_switch_intents' not in tables:
        return False
    statuses = tuple(ACTIVE_INTENT_STATUSES)
    placeholders = ','.join('?' for _ in statuses)
    allowed = str(allowed_switch_request_id or '').strip()
    if allowed:
        row = conn.execute(
            'SELECT 1 FROM context_switch_intents '
            f'WHERE chat_id=? AND status IN ({placeholders}) AND request_id!=? '
            'LIMIT 1',
            (chat_id, *statuses, allowed),
        ).fetchone()
    else:
        row = conn.execute(
            'SELECT 1 FROM context_switch_intents '
            f'WHERE chat_id=? AND status IN ({placeholders}) LIMIT 1',
            (chat_id, *statuses),
        ).fetchone()
    return row is not None


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(row) if row is not None else None


def _get_registry_conn(
    conn: sqlite3.Connection,
    context_id: int,
    resident_generation: int,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        'SELECT * FROM context_claude_sessions '
        'WHERE context_id=? AND resident_generation=?',
        (int(context_id), int(resident_generation)),
    ).fetchone())


def _canonical_user_payload_from_db(
    *,
    content: str,
    image_url: str,
    attachments: object = None,
    file_url: str = '',
    file_name: str = '',
) -> Any:
    """DB-authoritative user body from ordered canonical attachments."""
    from chat.attachment_contract import provider_current_turn_attachment_parts
    from chat.cc_vision_bridge import VisionBridgeError, build_claude_user_content

    raw_text = str(content or '').strip()
    attachment_parts = provider_current_turn_attachment_parts(
        attachments,
        legacy_file_url=file_url,
        legacy_file_name=file_name,
        legacy_image_url=image_url,
    )
    if not attachment_parts:
        return raw_text
    try:
        return build_claude_user_content(
            text=raw_text,
            attachment_parts=attachment_parts,
        )
    except VisionBridgeError:
        if raw_text:
            return raw_text
        raise


def _get_user_canonical_readonly_conn(
    conn: sqlite3.Connection,
    event_uuids: Sequence[str],
) -> dict[str, Any]:
    """Load mapped canonical user payloads from DB (text or multimodal)."""
    uids = [str(u).strip() for u in event_uuids if str(u or '').strip()]
    if not uids:
        return {}
    placeholders = ','.join('?' for _ in uids)
    cols = {
        str(r[1])
        for r in conn.execute('PRAGMA table_info(chat_messages)').fetchall()
    }
    image_col = 'm.image_url' if 'image_url' in cols else "'' AS image_url"
    file_url_col = 'm.file_url' if 'file_url' in cols else "'' AS file_url"
    file_name_col = 'm.file_name' if 'file_name' in cols else "'' AS file_name"
    attachments_col = 'm.attachments' if 'attachments' in cols else "'' AS attachments"
    rows = conn.execute(
        f'''SELECT e.event_uuid AS event_uuid, m.content AS content, {image_col},
                   {file_url_col}, {file_name_col}, {attachments_col}
            FROM chat_message_claude_events e
            JOIN chat_messages m ON m.id = e.message_id
            WHERE e.role = 'user' AND e.event_uuid IN ({placeholders})''',
        tuple(uids),
    ).fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        out[str(row['event_uuid'])] = _canonical_user_payload_from_db(
            content=str(row['content'] or ''),
            image_url=str(row['image_url'] or ''),
            file_url=str(row['file_url'] or ''),
            file_name=str(row['file_name'] or ''),
            attachments=row['attachments'],
        )
    return out


def _mapping_rows_for_messages_conn(
    conn: sqlite3.Connection,
    message_ids: Sequence[int],
) -> list[dict[str, Any]]:
    mids = [int(m) for m in message_ids]
    if not mids:
        return []
    placeholders = ','.join('?' for _ in mids)
    rows = conn.execute(
        f'''SELECT event_uuid, message_id, role, claude_session_id,
                   context_id, context_epoch, resident_generation, jsonl_byte_offset
            FROM chat_message_claude_events
            WHERE message_id IN ({placeholders})
            ORDER BY jsonl_byte_offset ASC''',
        tuple(mids),
    ).fetchall()
    return [dict(r) for r in rows]


def _message_content_map(messages: list[Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in messages:
        mid = int(row['id'])
        author = str(row['author'] or '').lower()
        role = 'user' if author in {'hayana', 'haya', 'user'} else 'assistant'
        content = str(row['content'] or '').strip()
        if not content and hasattr(row, 'keys') and 'image_url' in row.keys():
            if str(row['image_url'] or '').strip():
                content = '[image]'
        out[mid] = {'message_id': mid, 'role': role, 'content': content}
    return out


def _format_selected_rounds(
    selected_rounds: list[dict[str, Any]],
    content_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for rnd in selected_rounds:
        msgs = []
        for mid in rnd.get('message_ids') or []:
            info = content_by_id.get(int(mid))
            if info is None:
                continue
            msgs.append({
                'message_id': info['message_id'],
                'role': info['role'],
                'content': info['content'],
            })
        formatted.append({
            'round_id': int(rnd.get('round_id') or 0),
            'message_ids': [int(x) for x in (rnd.get('message_ids') or [])],
            'messages': msgs,
        })
    return formatted


def _candidate_session_id(
    *,
    preview_id: str,
    source_context_id: int,
    source_context_epoch: int,
    resident_generation: int,
    scan_offset: int,
    selected_round_count: int,
    thinking_policy: ThinkingPolicy,
) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        (
            'hayagarden-context-window-preview:'
            f'{preview_id}:'
            f'{source_context_id}:'
            f'{source_context_epoch}:'
            f'{resident_generation}:'
            f'{scan_offset}:'
            f'{selected_round_count}:'
            f'{thinking_policy.value}'
        ),
    ))


def _blocked(
    *,
    preview_id: str,
    error_code: str,
    source: dict[str, Any],
    selection: dict[str, Any],
    candidate_session_id: str,
    warnings: Optional[list[str]] = None,
    detail: str = '',
) -> dict[str, Any]:
    if detail:
        logger.info('preview blocked code=%s detail=%s', error_code, detail)
    return {
        'ok': True,
        'preview_status': PREVIEW_STATUS_BLOCKED,
        'preview_id': preview_id,
        'candidate_session_id': candidate_session_id,
        'candidate_session_reserved': False,
        'error_code': error_code,
        'source': source,
        'selection': selection,
        'candidate': {
            'event_count': 0,
            'round_count': 0,
            'serialized_bytes': 0,
            'output_sha256': sha256_text(''),
            'proof_kind': None,
        },
        'dropped': {
            'sidechain_event_count': 0,
            'sidechain_round_count': 0,
            'summary_event_count': 0,
            'system_event_count': 0,
            'unconfirmed_user_count': 0,
            'noise_event_count': 0,
        },
        'validation': {
            'ok': False,
            'errors': [error_code],
            'warnings': list(warnings or []),
            'stats': {},
            'proof_kind': None,
        },
        'warnings': list(warnings or []),
    }


def _source_payload(
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    claude_session_id: Optional[str],
    scan_status: Optional[str],
    scan_offset: Optional[int],
) -> dict[str, Any]:
    return {
        'context_id': int(context_id),
        'context_epoch': int(context_epoch),
        'resident_generation': int(resident_generation),
        'claude_session_id': claude_session_id,
        'scan_status': scan_status,
        'scan_offset': scan_offset,
    }


def _selection_payload(
    *,
    requested_round_count: int,
    selected_round_count: int,
    selected_message_ids: list[int],
    selected_rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        'requested_round_count': int(requested_round_count),
        'selected_round_count': int(selected_round_count),
        'selected_message_count': len(selected_message_ids),
        'selected_message_ids': list(selected_message_ids),
        'selected_rounds': selected_rounds,
    }


def _snapshot_source_identity(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    source_context_id: int,
    source_context_epoch: int,
    now_dt: datetime.datetime,
    allowed_switch_request_id: Optional[str] = None,
) -> dict[str, Any]:
    current = _resolve_canonical_context_readonly_conn(conn, chat_id=chat_id)
    if (
        int(current['id']) != int(source_context_id)
        or int(current['context_epoch']) != int(source_context_epoch)
        or str(current.get('chat_id') or '') != str(chat_id)
    ):
        raise StaleSourceContextError('stale_source_context')
    if current.get('closed_at'):
        raise StaleSourceContextError('stale_source_context')
    gen = int(current.get('resident_generation') or 1)
    if _is_resident_turn_active_conn(conn, int(source_context_id), gen, now_dt):
        raise WindowBusyError('window_busy')
    if _has_active_switch_intent_conn(
        conn, chat_id, allowed_switch_request_id=allowed_switch_request_id,
    ):
        raise SwitchInProgressError('switch_in_progress')
    messages = _collect_context_formal_messages(
        conn,
        context_id=int(source_context_id),
        context_epoch=int(source_context_epoch),
    )
    return {
        'context': dict(current),
        'resident_generation': gen,
        'messages': messages,
    }


def _recheck_after_read(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    source_context_id: int,
    source_context_epoch: int,
    resident_generation: int,
    requested_count: int,
    selected_message_ids: list[int],
    registry_snapshot: Optional[dict[str, Any]],
    now_dt: datetime.datetime,
    allowed_switch_request_id: Optional[str] = None,
) -> Optional[str]:
    try:
        snap = _snapshot_source_identity(
            conn,
            chat_id=chat_id,
            source_context_id=source_context_id,
            source_context_epoch=source_context_epoch,
            now_dt=now_dt,
            allowed_switch_request_id=allowed_switch_request_id,
        )
    except (StaleSourceContextError, WindowBusyError, SwitchInProgressError, NoOpenContextWindowError):
        return 'PREVIEW_SOURCE_CHANGED'
    if int(snap['resident_generation']) != int(resident_generation):
        return 'PREVIEW_SOURCE_CHANGED'
    _rounds, fresh_ids, _sel_count = _select_rounds_locked(snap['messages'], int(requested_count))
    if list(fresh_ids) != list(selected_message_ids):
        return 'PREVIEW_SOURCE_CHANGED'
    if registry_snapshot is not None:
        live = _get_registry_conn(conn, source_context_id, resident_generation)
        if live is None:
            return 'PREVIEW_SOURCE_CHANGED'
        for key in (
            'claude_session_id', 'scan_status', 'scan_offset',
            'context_epoch', 'chat_id', 'transcript_path',
        ):
            if str(live.get(key)) != str(registry_snapshot.get(key)):
                return 'PREVIEW_SOURCE_CHANGED'
    return None


def preview_context_window(
    *,
    source_context_id: int,
    source_context_epoch: int,
    count: int,
    preview_id: str,
    thinking_policy: ThinkingPolicy,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: str,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Read-only Preview / dry-run for a candidate context-window switch."""
    prep = prepare_context_window_candidate(
        source_context_id=source_context_id,
        source_context_epoch=source_context_epoch,
        count=count,
        preview_id=preview_id,
        thinking_policy=thinking_policy,
        chat_id=chat_id,
        db_path=db_path,
        now=now,
        allowed_switch_request_id=None,
    )
    return prep.response


def prepare_context_window_candidate(
    *,
    source_context_id: int,
    source_context_epoch: int,
    count: int,
    preview_id: str,
    thinking_policy: ThinkingPolicy,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: str,
    now: Optional[datetime.datetime] = None,
    allowed_switch_request_id: Optional[str] = None,
) -> CandidatePreparation:
    """Prepare Preview response + optional validated candidate artifact."""
    if thinking_policy not in (ThinkingPolicy.KEEP, ThinkingPolicy.DROP):
        raise ValueError('thinking_policy must be keep or drop')
    try:
        preview_uuid = str(uuid.UUID(str(preview_id)))
    except (TypeError, ValueError) as exc:
        raise ValueError('preview_id must be a UUID') from exc

    now_dt = _shanghai_now(now)
    source_id = int(source_context_id)
    source_epoch = int(source_context_epoch)
    requested = int(count)
    allowed_rid = (
        str(uuid.UUID(str(allowed_switch_request_id)))
        if allowed_switch_request_id not in (None, '')
        else None
    )

    # Populated inside first readonly txn; used after close for fresh recheck.
    gen: int
    source_version: int
    boundary_id: int
    selection: dict[str, Any]
    selected_ids: list[int]
    content_by_id: dict[int, dict[str, Any]]
    native_cold = False
    early_blocked: Optional[dict[str, Any]] = None
    ready_bundle: Optional[dict[str, Any]] = None

    conn = _open_preview_db_readonly(db_path)
    try:
        snap = _snapshot_source_identity(
            conn,
            chat_id=chat_id,
            source_context_id=source_id,
            source_context_epoch=source_epoch,
            now_dt=now_dt,
            allowed_switch_request_id=allowed_rid,
        )
        gen = int(snap['resident_generation'])
        source_version = int(snap['context'].get('version') or 1)
        messages = snap['messages']
        boundary_id = int(_last_formal_message_id(messages))
        selected_rounds, selected_ids, selected_round_count = _select_rounds_locked(
            messages, requested,
        )
        content_by_id = _message_content_map(messages)
        formatted_rounds = _format_selected_rounds(selected_rounds, content_by_id)
        selection = _selection_payload(
            requested_round_count=requested,
            selected_round_count=selected_round_count,
            selected_message_ids=selected_ids,
            selected_rounds=formatted_rounds,
        )

        # ----- count == 0 → prepare NATIVE_COLD (fresh recheck after close) -----
        if requested == 0:
            native_cold = True
        else:
            # ----- count > 0: Registry gate -----
            registry = _get_registry_conn(conn, source_id, gen)
            parsed_scan_offset: Optional[int] = None
            if registry is not None:
                parsed_scan_offset = _parse_registry_scan_offset(registry)

            seed_offset = int(parsed_scan_offset) if parsed_scan_offset is not None else 0
            sid_probe = (
                str(registry.get('claude_session_id') or '').strip() or None
                if registry is not None else None
            )
            status_probe = (
                str(registry.get('scan_status') or '') or None
                if registry is not None else None
            )
            source_base = _source_payload(
                context_id=source_id,
                context_epoch=source_epoch,
                resident_generation=gen,
                claude_session_id=sid_probe,
                scan_status=status_probe,
                scan_offset=parsed_scan_offset,
            )
            candidate_sid_probe = _candidate_session_id(
                preview_id=preview_uuid,
                source_context_id=source_id,
                source_context_epoch=source_epoch,
                resident_generation=gen,
                scan_offset=seed_offset,
                selected_round_count=selected_round_count,
                thinking_policy=thinking_policy,
            )

            def _blk(
                code: str,
                detail: str = '',
                *,
                source: Optional[dict[str, Any]] = None,
                candidate_session_id: Optional[str] = None,
                warnings: Optional[list[str]] = None,
            ) -> dict[str, Any]:
                return _blocked(
                    preview_id=preview_uuid,
                    error_code=code,
                    source=source if source is not None else source_base,
                    selection=selection,
                    candidate_session_id=(
                        candidate_session_id
                        if candidate_session_id is not None
                        else candidate_sid_probe
                    ),
                    detail=detail,
                    warnings=warnings,
                )

            if registry is None:
                early_blocked = _blk(
                    'PREVIEW_REGISTRY_MISSING',
                    source=_source_payload(
                        context_id=source_id,
                        context_epoch=source_epoch,
                        resident_generation=gen,
                        claude_session_id=None,
                        scan_status=None,
                        scan_offset=None,
                    ),
                    candidate_session_id=_candidate_session_id(
                        preview_id=preview_uuid,
                        source_context_id=source_id,
                        source_context_epoch=source_epoch,
                        resident_generation=gen,
                        scan_offset=0,
                        selected_round_count=selected_round_count,
                        thinking_policy=thinking_policy,
                    ),
                )
            elif int(registry.get('context_epoch') or -1) != source_epoch:
                early_blocked = _blk('PREVIEW_REGISTRY_EPOCH_MISMATCH')
            elif str(registry.get('chat_id') or '') != str(chat_id):
                early_blocked = _blk('PREVIEW_REGISTRY_CHAT_MISMATCH')
            elif str(registry.get('scan_status') or '') != SCAN_STATUS_READY:
                early_blocked = _blk('PREVIEW_REGISTRY_NOT_READY')
            else:
                sid = str(registry.get('claude_session_id') or '').strip()
                path = str(registry.get('transcript_path') or '').strip()
                if not sid:
                    early_blocked = _blk('PREVIEW_REGISTRY_SESSION_MISSING')
                elif not path:
                    early_blocked = _blk('PREVIEW_REGISTRY_PATH_MISSING')
                elif parsed_scan_offset is None:
                    early_blocked = _blk(
                        'PREVIEW_REGISTRY_SCAN_OFFSET_INVALID',
                        source=_source_payload(
                            context_id=source_id,
                            context_epoch=source_epoch,
                            resident_generation=gen,
                            claude_session_id=sid or None,
                            scan_status=SCAN_STATUS_READY,
                            scan_offset=None,
                        ),
                        candidate_session_id=_candidate_session_id(
                            preview_id=preview_uuid,
                            source_context_id=source_id,
                            source_context_epoch=source_epoch,
                            resident_generation=gen,
                            scan_offset=0,
                            selected_round_count=selected_round_count,
                            thinking_policy=thinking_policy,
                        ),
                    )
                else:
                    scan_offset = parsed_scan_offset
                    registry_snapshot = dict(registry)
                    candidate_sid = _candidate_session_id(
                        preview_id=preview_uuid,
                        source_context_id=source_id,
                        source_context_epoch=source_epoch,
                        resident_generation=gen,
                        scan_offset=scan_offset,
                        selected_round_count=selected_round_count,
                        thinking_policy=thinking_policy,
                    )
                    source_base = _source_payload(
                        context_id=source_id,
                        context_epoch=source_epoch,
                        resident_generation=gen,
                        claude_session_id=sid,
                        scan_status=SCAN_STATUS_READY,
                        scan_offset=scan_offset,
                    )

                    try:
                        prefix_before = _snapshot_transcript_prefix(path, scan_offset)
                    except _PrefixSnapshotError as exc:
                        early_blocked = _blk(exc.code)
                    else:
                        try:
                            graph = read_transcript_range(path, 0, scan_offset)
                        except TranscriptReaderError as exc:
                            logger.info(
                                'preview transcript reader failed code=%s',
                                getattr(exc, 'code', type(exc).__name__),
                            )
                            early_blocked = _blk('PREVIEW_TRANSCRIPT_READ_FAILED')
                        except Exception as exc:
                            logger.info(
                                'preview transcript read unexpected: %s',
                                type(exc).__name__,
                            )
                            early_blocked = _blk('PREVIEW_TRANSCRIPT_READ_FAILED')
                        else:
                            candidate_user_uuids = [
                                rnd.candidate_user_event_uuid
                                for rnd in graph.candidate_rounds
                            ]
                            try:
                                canonical_map = _get_user_canonical_readonly_conn(
                                    conn, candidate_user_uuids,
                                )
                            except Exception as exc:
                                from chat.cc_vision_bridge import VisionBridgeError
                                if isinstance(exc, VisionBridgeError):
                                    logger.info(
                                        'preview canonical vision build failed code=%s',
                                        getattr(exc, 'code', type(exc).__name__),
                                    )
                                    early_blocked = _blk('PREVIEW_TRANSFORM_FAILED')
                                else:
                                    raise
                            if early_blocked is None:
                                mapping_rows = _mapping_rows_for_messages_conn(
                                    conn, selected_ids,
                                )
                                if not mapping_rows and selected_ids:
                                    early_blocked = _blk('PREVIEW_MAPPING_MISSING')
                                else:
                                    sessions = {
                                        str(r['claude_session_id']) for r in mapping_rows
                                    }
                                    gens = {
                                        int(r['resident_generation']) for r in mapping_rows
                                    }
                                    if len(sessions) > 1 or (gens and gens != {gen}):
                                        early_blocked = _blk(
                                            'PREVIEW_SELECTED_ROUNDS_SPAN_SESSIONS',
                                        )
                                    elif sessions and sessions != {sid}:
                                        early_blocked = _blk(
                                            'PREVIEW_SELECTED_ROUNDS_SPAN_SESSIONS',
                                        )
                                    else:
                                        by_message: dict[int, list[dict[str, Any]]] = {}
                                        for row in mapping_rows:
                                            by_message.setdefault(
                                                int(row['message_id']), [],
                                            ).append(row)

                                        mapping_ok = True
                                        for mid in selected_ids:
                                            info = content_by_id.get(int(mid))
                                            rows = by_message.get(int(mid)) or []
                                            if not rows:
                                                early_blocked = _blk(
                                                    'PREVIEW_MAPPING_MISSING',
                                                    f'message_id={mid}',
                                                )
                                                mapping_ok = False
                                                break
                                            identity_bad = False
                                            for row in rows:
                                                if (
                                                    int(row['context_id']) != source_id
                                                    or int(row['context_epoch']) != source_epoch
                                                    or int(row['resident_generation']) != gen
                                                    or str(row['claude_session_id']) != sid
                                                ):
                                                    early_blocked = _blk(
                                                        'PREVIEW_MAPPING_IDENTITY_MISMATCH',
                                                        f'message_id={mid}',
                                                    )
                                                    identity_bad = True
                                                    break
                                            if identity_bad:
                                                mapping_ok = False
                                                break
                                            if info and info['role'] == 'user':
                                                user_rows = [
                                                    r for r in rows if str(r['role']) == 'user'
                                                ]
                                                if len(user_rows) != 1:
                                                    early_blocked = _blk(
                                                        'PREVIEW_MAPPING_USER_CARDINALITY',
                                                        f'message_id={mid}',
                                                    )
                                                    mapping_ok = False
                                                    break
                                                if str(user_rows[0]['event_uuid']) not in canonical_map:
                                                    early_blocked = _blk(
                                                        'PREVIEW_MAPPING_CANONICAL_MISSING',
                                                        f'message_id={mid}',
                                                    )
                                                    mapping_ok = False
                                                    break
                                            elif info and info['role'] == 'assistant':
                                                asst_rows = [
                                                    r for r in rows
                                                    if str(r['role']) in _MAPPING_ASSISTANT_ROLES
                                                ]
                                                if not asst_rows:
                                                    early_blocked = _blk(
                                                        'PREVIEW_MAPPING_ASSISTANT_MISSING',
                                                        f'message_id={mid}',
                                                    )
                                                    mapping_ok = False
                                                    break

                                        if mapping_ok:
                                            eligible = []
                                            for rnd in graph.candidate_rounds:
                                                cand = rnd.candidate_user_event_uuid
                                                if cand not in canonical_map:
                                                    continue
                                                if rnd.has_sidechain_impact:
                                                    continue
                                                eligible.append(rnd)
                                            transcript_tail = (
                                                eligible[-selected_round_count:]
                                                if selected_round_count else []
                                            )
                                            app_user_ids = [
                                                int(mid) for mid in selected_ids
                                                if content_by_id.get(int(mid), {}).get('role')
                                                == 'user'
                                            ]
                                            app_user_event_uuids: list[str] = []
                                            sel_mismatch = False
                                            for mid in app_user_ids:
                                                user_rows = [
                                                    r for r in by_message.get(int(mid), [])
                                                    if str(r['role']) == 'user'
                                                ]
                                                if len(user_rows) != 1:
                                                    early_blocked = _blk(
                                                        'PREVIEW_SELECTION_MAPPING_MISMATCH',
                                                        f'message_id={mid}',
                                                    )
                                                    sel_mismatch = True
                                                    break
                                                app_user_event_uuids.append(
                                                    str(user_rows[0]['event_uuid']),
                                                )
                                            if not sel_mismatch:
                                                transcript_user_uuids = [
                                                    r.candidate_user_event_uuid
                                                    for r in transcript_tail
                                                ]
                                                if app_user_event_uuids != transcript_user_uuids:
                                                    early_blocked = _blk(
                                                        'PREVIEW_SELECTION_MAPPING_MISMATCH',
                                                    )
                                                else:
                                                    allowed_event_uuids: set[str] = set()
                                                    for rnd in transcript_tail:
                                                        allowed_event_uuids.update(rnd.event_uuids)
                                                    outside = False
                                                    for mid in selected_ids:
                                                        for row in by_message.get(int(mid), []):
                                                            if str(row['event_uuid']) not in allowed_event_uuids:
                                                                early_blocked = _blk(
                                                                    'PREVIEW_MAPPING_EVENT_OUTSIDE_SELECTION',
                                                                    f"event_uuid={row['event_uuid']}",
                                                                )
                                                                outside = True
                                                                break
                                                        if outside:
                                                            break
                                                    if not outside:
                                                        cwds: set[str] = set()
                                                        for rnd in transcript_tail:
                                                            for uid in rnd.event_uuids:
                                                                evt = graph.by_uuid.get(uid)
                                                                if evt is None:
                                                                    continue
                                                                cwd_val = str(
                                                                    evt.raw.get('cwd') or '',
                                                                ).strip()
                                                                if cwd_val:
                                                                    cwds.add(cwd_val)
                                                        if len(cwds) != 1:
                                                            early_blocked = _blk(
                                                                'PREVIEW_CWD_AMBIGUOUS',
                                                            )
                                                        else:
                                                            cwd = next(iter(cwds))
                                                            try:
                                                                transform_result = transform_transcript(
                                                                    graph,
                                                                    TransformRequest(
                                                                        new_session_id=candidate_sid,
                                                                        cwd=cwd,
                                                                        keep_rounds=selected_round_count,
                                                                        user_canonical_by_event_uuid=canonical_map,
                                                                        thinking_policy=thinking_policy,
                                                                        sidechain_policy=SidechainPolicy.EXCLUDE,
                                                                        summary_policy=SummaryPolicy.DROP,
                                                                        unknown_event_policy=UnknownEventPolicy.DROP,
                                                                        tool_primer_candidate=None,
                                                                        version='2.1.220',
                                                                    ),
                                                                )
                                                            except TransformError as exc:
                                                                logger.info(
                                                                    'preview transform failed code=%s',
                                                                    getattr(exc, 'code', type(exc).__name__),
                                                                )
                                                                early_blocked = _blk(
                                                                    'PREVIEW_TRANSFORM_FAILED',
                                                                )
                                                            else:
                                                                validation = validate_transcript_events(
                                                                    transform_result.events,
                                                                    ValidatorOptions(
                                                                        session_id=candidate_sid,
                                                                        thinking_policy=thinking_policy,
                                                                        forbid_sidechain=True,
                                                                        forbid_summary=True,
                                                                        expected_round_count=selected_round_count,
                                                                        max_round_count=selected_round_count,
                                                                        old_uuids=set(graph.by_uuid.keys()),
                                                                        unknown_event_mode='reject',
                                                                    ),
                                                                )
                                                                if not validation.ok:
                                                                    logger.info(
                                                                        'preview validator rejected errors=%s',
                                                                        list(validation.errors)[:5],
                                                                    )
                                                                    early_blocked = _blk(
                                                                        'PREVIEW_VALIDATOR_REJECTED',
                                                                        warnings=list(validation.warnings),
                                                                    )
                                                                else:
                                                                    try:
                                                                        prefix_after = _snapshot_transcript_prefix(
                                                                            path, scan_offset,
                                                                        )
                                                                    except _PrefixSnapshotError as exc:
                                                                        early_blocked = _blk(exc.code)
                                                                    else:
                                                                        if (
                                                                            prefix_after.end_offset
                                                                            != prefix_before.end_offset
                                                                            or prefix_after.sha256
                                                                            != prefix_before.sha256
                                                                        ):
                                                                            early_blocked = _blk(
                                                                                'PREVIEW_SOURCE_CHANGED',
                                                                            )
                                                                        else:
                                                                            serialized = serialize_events(
                                                                                transform_result.events,
                                                                            )
                                                                            serialized_bytes_blob = (
                                                                                serialized.encode('utf-8')
                                                                            )
                                                                            ready_bundle = {
                                                                                'candidate_sid': candidate_sid,
                                                                                'source_base': source_base,
                                                                                'registry_snapshot': registry_snapshot,
                                                                                'transform_result': transform_result,
                                                                                'validation': validation,
                                                                                'serialized_jsonl': serialized_bytes_blob,
                                                                                'serialized_bytes': len(
                                                                                    serialized_bytes_blob,
                                                                                ),
                                                                                'output_sha256': (
                                                                                    transform_result.output_sha256
                                                                                ),
                                                                                'source_transcript_path': path,
                                                                                'source_scan_offset': scan_offset,
                                                                                'source_prefix_sha256': (
                                                                                    prefix_before.sha256
                                                                                ),
                                                                            }
    finally:
        _close_preview_readonly(conn)

    if early_blocked is not None:
        return CandidatePreparation(response=early_blocked, artifact=None)

    # Fresh readonly connection for final identity recheck (NATIVE_COLD and READY).
    conn2 = _open_preview_db_readonly(db_path)
    try:
        if native_cold:
            candidate_sid = _candidate_session_id(
                preview_id=preview_uuid,
                source_context_id=source_id,
                source_context_epoch=source_epoch,
                resident_generation=gen,
                scan_offset=0,
                selected_round_count=0,
                thinking_policy=thinking_policy,
            )
            source_cold = _source_payload(
                context_id=source_id,
                context_epoch=source_epoch,
                resident_generation=gen,
                claude_session_id=None,
                scan_status=None,
                scan_offset=None,
            )
            err = _recheck_after_read(
                conn2,
                chat_id=chat_id,
                source_context_id=source_id,
                source_context_epoch=source_epoch,
                resident_generation=gen,
                requested_count=0,
                selected_message_ids=[],
                registry_snapshot=None,
                now_dt=_shanghai_now(now),
                allowed_switch_request_id=allowed_rid,
            )
            if err:
                return CandidatePreparation(
                    response=_blocked(
                        preview_id=preview_uuid,
                        error_code=err,
                        source=source_cold,
                        selection=selection,
                        candidate_session_id=candidate_sid,
                    ),
                    artifact=None,
                )
            empty_sha = sha256_text('')
            response = {
                'ok': True,
                'preview_status': PREVIEW_STATUS_NATIVE_COLD,
                'preview_id': preview_uuid,
                'candidate_session_id': candidate_sid,
                'candidate_session_reserved': False,
                'source': source_cold,
                'selection': selection,
                'candidate': {
                    'event_count': 0,
                    'round_count': 0,
                    'serialized_bytes': 0,
                    'output_sha256': empty_sha,
                    'proof_kind': 'native_cold_contract',
                },
                'dropped': {
                    'sidechain_event_count': 0,
                    'sidechain_round_count': 0,
                    'summary_event_count': 0,
                    'system_event_count': 0,
                    'unconfirmed_user_count': 0,
                    'noise_event_count': 0,
                },
                'validation': {
                    'ok': True,
                    'errors': [],
                    'warnings': [],
                    'stats': {'event_count': 0, 'round_count': 0},
                    'proof_kind': 'native_cold_contract',
                },
                'warnings': [],
            }
            artifact = PreparedPreviewCandidate(
                preview_id=preview_uuid,
                candidate_session_id=candidate_sid,
                source_context_id=source_id,
                source_context_epoch=source_epoch,
                source_version=source_version,
                source_resident_generation=gen,
                source_boundary_message_id=boundary_id,
                source_transcript_path=None,
                source_scan_offset=0,
                source_prefix_sha256=None,
                requested_round_count=requested,
                selected_round_count=0,
                selected_message_ids=tuple(selected_ids),
                thinking_policy=thinking_policy,
                serialized_jsonl=b'',
                output_sha256=empty_sha,
                serialized_bytes=0,
                event_count=0,
                proof_kind='native_cold_contract',
            )
            return CandidatePreparation(response=response, artifact=artifact)

        assert ready_bundle is not None
        changed = _recheck_after_read(
            conn2,
            chat_id=chat_id,
            source_context_id=source_id,
            source_context_epoch=source_epoch,
            resident_generation=gen,
            requested_count=requested,
            selected_message_ids=selected_ids,
            registry_snapshot=ready_bundle['registry_snapshot'],
            now_dt=_shanghai_now(now),
            allowed_switch_request_id=allowed_rid,
        )
        if changed:
            return CandidatePreparation(
                response=_blocked(
                    preview_id=preview_uuid,
                    error_code=changed,
                    source=ready_bundle['source_base'],
                    selection=selection,
                    candidate_session_id=ready_bundle['candidate_sid'],
                ),
                artifact=None,
            )
    finally:
        _close_preview_readonly(conn2)

    transform_result = ready_bundle['transform_result']
    validation = ready_bundle['validation']
    response = {
        'ok': True,
        'preview_status': PREVIEW_STATUS_READY,
        'preview_id': preview_uuid,
        'candidate_session_id': ready_bundle['candidate_sid'],
        'candidate_session_reserved': False,
        'source': ready_bundle['source_base'],
        'selection': selection,
        'candidate': {
            'event_count': len(transform_result.events),
            'round_count': int(transform_result.selected_round_count),
            'serialized_bytes': ready_bundle['serialized_bytes'],
            'output_sha256': ready_bundle['output_sha256'],
            'proof_kind': validation.proof_kind,
        },
        'dropped': {
            'sidechain_event_count': len(transform_result.dropped_sidechain_uuids),
            'sidechain_round_count': len(
                transform_result.dropped_sidechain_round_user_uuids
            ),
            'summary_event_count': len(transform_result.dropped_summary_uuids),
            'system_event_count': len(transform_result.dropped_system_uuids),
            'unconfirmed_user_count': len(
                transform_result.dropped_unconfirmed_user_uuids
            ),
            'noise_event_count': len(transform_result.dropped_noise_uuids),
        },
        'validation': {
            'ok': True,
            'errors': list(validation.errors),
            'warnings': list(validation.warnings),
            'stats': dict(validation.stats),
            'proof_kind': validation.proof_kind,
        },
        'warnings': list(validation.warnings) + list(transform_result.notes),
    }
    artifact = PreparedPreviewCandidate(
        preview_id=preview_uuid,
        candidate_session_id=str(ready_bundle['candidate_sid']),
        source_context_id=source_id,
        source_context_epoch=source_epoch,
        source_version=source_version,
        source_resident_generation=gen,
        source_boundary_message_id=boundary_id,
        source_transcript_path=str(ready_bundle['source_transcript_path']),
        source_scan_offset=int(ready_bundle['source_scan_offset']),
        source_prefix_sha256=str(ready_bundle['source_prefix_sha256']),
        requested_round_count=requested,
        selected_round_count=int(transform_result.selected_round_count),
        selected_message_ids=tuple(int(x) for x in selected_ids),
        thinking_policy=thinking_policy,
        serialized_jsonl=bytes(ready_bundle['serialized_jsonl']),
        output_sha256=str(ready_bundle['output_sha256']),
        serialized_bytes=int(ready_bundle['serialized_bytes']),
        event_count=len(transform_result.events),
        proof_kind=str(validation.proof_kind),
    )
    return CandidatePreparation(response=response, artifact=artifact)


def parse_thinking_policy(value: Any) -> ThinkingPolicy:
    if value is None or value == '':
        return ThinkingPolicy.DROP
    if not isinstance(value, str):
        raise ValueError('thinking_policy must be keep or drop')
    raw = value.strip().lower()
    if raw == ThinkingPolicy.KEEP.value:
        return ThinkingPolicy.KEEP
    if raw == ThinkingPolicy.DROP.value:
        return ThinkingPolicy.DROP
    raise ValueError('thinking_policy must be keep or drop')
