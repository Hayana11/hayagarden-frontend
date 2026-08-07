"""DB-authoritative Forge adapter for manual context-window switch.

Builds Claude JSONL solely from locked formal chat_messages. Does not scan
legacy Claude transcripts for carryover content.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from chat.daily_context import (
    _USER_AUTHORS,
    _message_display_content,
    _table_columns,
    is_formal_chat_message,
)
from chat.cc_vision_bridge import VisionBridgeError, build_claude_user_content
from tools.claude_forge_core import (
    build_minimal_text_session,
    new_uuid,
    session_jsonl_path_for_cwd,
    sha256_file,
    sha256_text,
    verify_work_root,
)
from tools.claude_forge_validator import validate_forged_transcript

BOUNDARY_PRIMER_USER = '[context-window-boundary]'
BOUNDARY_PRIMER_ASSISTANT = '[context-window-ready]'

FORGE_VERSION = '2.1.220'


class CarryoverUnforgeableError(ValueError):
    """selected_message_ids cannot form a valid forged transcript."""

    def __init__(self, detail: str = ''):
        code = 'carryover_message_unforgeable'
        super().__init__(code if not detail else '%s:%s' % (code, detail))
        self.error_code = 'carryover_message_unforgeable'
        self.detail = detail


@dataclass
class ForgeWriteResult:
    target_session_id: str
    jsonl_path: Path
    sha256: str
    event_count: int


def _role_for_author(author: str) -> str:
    return 'user' if str(author or '').strip().lower() in _USER_AUTHORS else 'assistant'


def _fetch_messages_by_ids(
    conn: sqlite3.Connection,
    message_ids: Sequence[int],
) -> list[Any]:
    if not message_ids:
        return []
    cols = _table_columns(conn, 'chat_messages')
    if not cols:
        raise CarryoverUnforgeableError('chat_messages_missing')
    select_cols = ['id', 'author', 'content', 'created_at']
    for optional in ('tool_calls', 'source_kind', 'image_url'):
        if optional in cols:
            select_cols.append(optional)
    placeholders = ','.join('?' for _ in message_ids)
    rows = conn.execute(
        'SELECT %s FROM chat_messages WHERE id IN (%s)'
        % (', '.join(select_cols), placeholders),
        tuple(int(x) for x in message_ids),
    ).fetchall()
    by_id = {int(r['id']): r for r in rows}
    ordered: list[Any] = []
    for mid in message_ids:
        row = by_id.get(int(mid))
        if row is None:
            raise CarryoverUnforgeableError('message_missing:%s' % mid)
        ordered.append(row)
    return ordered


def _row_image_url(row: Any) -> str:
    if hasattr(row, 'keys') and 'image_url' in row.keys():
        return str(row['image_url'] or '').strip()
    return ''


def _user_content_for_row(row: Any) -> Any:
    """Build forged user message.content — text str or multimodal list."""
    raw_text = str(row['content'] or '').strip()
    image_url = _row_image_url(row)
    if not image_url:
        text = _message_display_content(row)
        if not text:
            raise CarryoverUnforgeableError('empty_content:%s' % int(row['id']))
        return text
    try:
        return build_claude_user_content(text=raw_text, image_refs=[image_url])
    except VisionBridgeError as exc:
        if raw_text:
            return raw_text
        raise CarryoverUnforgeableError('vision_unresolvable:%s' % exc.code) from exc


def _event_for_role(
    *,
    role: str,
    content: Any,
    event_uuid: str,
    parent: Optional[str],
    session_id: str,
    cwd: str,
    timestamp: str,
) -> dict[str, Any]:
    if role == 'user':
        user_content = content
    else:
        user_content = [{'type': 'text', 'text': str(content)}]
    return {
        'type': role,
        'uuid': event_uuid,
        'parentUuid': parent,
        'timestamp': timestamp,
        'sessionId': session_id,
        'cwd': cwd,
        'version': FORGE_VERSION,
        'message': {'role': role, 'content': user_content},
    }


def build_events_from_selected_messages(
    conn: sqlite3.Connection,
    *,
    selected_message_ids: Sequence[int],
    target_session_id: str,
    cwd: str,
) -> list[dict[str, Any]]:
    """Build user/assistant events from DB rows in locked order."""
    if not selected_message_ids:
        events = build_minimal_text_session(
            session_id=target_session_id,
            cwd=cwd,
            user_texts=[BOUNDARY_PRIMER_USER],
            assistant_texts=[BOUNDARY_PRIMER_ASSISTANT],
        )
        for evt in events:
            evt['version'] = FORGE_VERSION
        return events

    rows = _fetch_messages_by_ids(conn, selected_message_ids)
    events: list[dict[str, Any]] = []
    parent: Optional[str] = None
    saw_user_in_round = False
    saw_assistant_in_round = False
    ts = '2026-07-30T00:00:00.000Z'

    for row in rows:
        if not is_formal_chat_message(row):
            raise CarryoverUnforgeableError('not_formal:%s' % int(row['id']))
        role = _role_for_author(str(row['author'] or ''))
        if role not in ('user', 'assistant'):
            raise CarryoverUnforgeableError('bad_role:%s' % int(row['id']))
        if role == 'user':
            message_content = _user_content_for_row(row)
        else:
            text = _message_display_content(row)
            if not text:
                raise CarryoverUnforgeableError('empty_content:%s' % int(row['id']))
            message_content = text

        if role == 'user':
            if saw_user_in_round and not saw_assistant_in_round:
                raise CarryoverUnforgeableError('round_order:%s' % int(row['id']))
            if events and not saw_assistant_in_round:
                raise CarryoverUnforgeableError('incomplete_round_before:%s' % int(row['id']))
            saw_user_in_round = True
            saw_assistant_in_round = False
        else:
            if not saw_user_in_round:
                raise CarryoverUnforgeableError('round_order:%s' % int(row['id']))
            saw_assistant_in_round = True

        eid = new_uuid()
        events.append(_event_for_role(
            role=role,
            content=message_content,
            event_uuid=eid,
            parent=parent,
            session_id=target_session_id,
            cwd=cwd,
            timestamp=ts,
        ))
        parent = eid

    if not events:
        raise CarryoverUnforgeableError('no_events')
    if events[0]['type'] != 'user':
        raise CarryoverUnforgeableError('first_not_user')
    if not saw_assistant_in_round:
        raise CarryoverUnforgeableError('incomplete_trailing_user')
    return events


def atomic_write_jsonl_fsync(path: Path, events: Sequence[dict[str, Any]]) -> str:
    """tmp → flush → fsync → rename (and fsync directory)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(evt, ensure_ascii=False, separators=(',', ':')) for evt in events]
    text = '\n'.join(lines) + ('\n' if lines else '')
    data = text.encode('utf-8')
    digest = sha256_text(text)
    tmp = path.parent / ('.forge-tmp-%s.jsonl' % new_uuid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return digest


def forge_target_session_from_db(
    conn: sqlite3.Connection,
    *,
    selected_message_ids: Sequence[int],
    cwd: str,
    claude_home: Path,
    target_session_id: Optional[str] = None,
) -> ForgeWriteResult:
    verify_work_root(Path(cwd))
    home = Path(claude_home)
    verify_work_root(home)
    sid = str(target_session_id or new_uuid())
    events = build_events_from_selected_messages(
        conn,
        selected_message_ids=list(selected_message_ids),
        target_session_id=sid,
        cwd=str(cwd),
    )
    out_path = session_jsonl_path_for_cwd(str(cwd), sid, claude_home=home)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    digest = atomic_write_jsonl_fsync(out_path, events)
    validation = validate_forged_transcript(
        events,
        session_id=sid,
        output_path=out_path,
        expected_sha256=digest,
        allowed_output_root=home,
        forbid_legacy_injection=True,
        forbid_sidechain=True,
    )
    if not validation.ok:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CarryoverUnforgeableError('validator:%s' % ';'.join(validation.errors[:5]))
    file_digest = sha256_file(out_path)
    if file_digest != digest:
        raise CarryoverUnforgeableError('sha_mismatch_after_write')
    return ForgeWriteResult(
        target_session_id=sid,
        jsonl_path=out_path,
        sha256=file_digest,
        event_count=len(events),
    )
