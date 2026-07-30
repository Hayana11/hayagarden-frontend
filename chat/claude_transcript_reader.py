"""Read-only Claude Code JSONL transcript reader (v0.2 Transcript Core).

Responsibilities:
- open source JSONL read-only
- parse line-by-line
- preserve raw event objects
- build UUID / parent / tool indexes
- classify event roles (real user vs tool_result user vs meta)
- report illegal JSON / duplicate UUID with explicit errors

Non-responsibilities (explicit):
- round selection, rewriting, resume, DB mapping, mtime session pick, --continue
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from chat.claude_transcript_model import (
    EventRole,
    EventType,
    RealConversationRound,
    ToolResultRef,
    ToolUseRef,
    TranscriptEvent,
    TranscriptGraph,
)


class ReaderErrorCode(str, Enum):
    INVALID_JSON = 'READER_INVALID_JSON'
    NOT_OBJECT = 'READER_NOT_OBJECT'
    DUPLICATE_UUID = 'READER_DUPLICATE_UUID'
    MISSING_UUID = 'READER_MISSING_UUID'
    PATH_NOT_FILE = 'READER_PATH_NOT_FILE'
    PATH_SYMLINK = 'READER_PATH_SYMLINK'
    IO_ERROR = 'READER_IO_ERROR'


class TranscriptReaderError(ValueError):
    def __init__(self, code: ReaderErrorCode, detail: str = ''):
        self.code = code
        msg = code.value if not detail else f'{code.value}:{detail}'
        super().__init__(msg)
        self.detail = detail


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _is_tool_result_only_user(message: Mapping[str, Any]) -> bool:
    content = message.get('content')
    if not isinstance(content, list) or not content:
        return False
    blocks = [b for b in content if isinstance(b, dict)]
    if not blocks:
        return False
    return all(b.get('type') == 'tool_result' for b in blocks)


def _classify_event(raw: Mapping[str, Any]) -> tuple[EventType, EventRole, bool]:
    is_sidechain = raw.get('isSidechain') is True
    etype_raw = str(raw.get('type') or '')
    message = raw.get('message') if isinstance(raw.get('message'), dict) else {}

    if etype_raw == 'summary':
        return EventType.SUMMARY, EventRole.SUMMARY, is_sidechain
    if etype_raw == 'assistant':
        role = EventRole.SIDECHAIN if is_sidechain else EventRole.ASSISTANT
        return EventType.ASSISTANT, role, is_sidechain
    if etype_raw == 'system':
        role = EventRole.SIDECHAIN if is_sidechain else EventRole.SYSTEM
        return EventType.SYSTEM, role, is_sidechain
    if etype_raw == 'user':
        if _is_tool_result_only_user(message):
            role = EventRole.SIDECHAIN if is_sidechain else EventRole.TOOL_RESULT_USER
        else:
            role = EventRole.SIDECHAIN if is_sidechain else EventRole.REAL_USER
        return EventType.USER, role, is_sidechain
    if etype_raw in {'queue-operation', 'last-prompt', 'result', 'file-history-snapshot'}:
        return EventType.UNKNOWN, EventRole.META, is_sidechain
    return EventType.UNKNOWN, EventRole.UNKNOWN, is_sidechain


def _extract_tool_refs(
    event: TranscriptEvent,
) -> tuple[list[ToolUseRef], list[ToolResultRef]]:
    uses: list[ToolUseRef] = []
    results: list[ToolResultRef] = []
    message = event.raw.get('message') if isinstance(event.raw.get('message'), dict) else {}
    for block in _content_blocks(message):
        btype = block.get('type')
        if btype == 'tool_use':
            tid = str(block.get('id') or '')
            if tid:
                uses.append(
                    ToolUseRef(
                        tool_use_id=tid,
                        event_uuid=event.event_uuid,
                        name=str(block.get('name') or ''),
                        line_number=event.line_number,
                    )
                )
        elif btype == 'tool_result':
            tid = str(block.get('tool_use_id') or '')
            if tid:
                results.append(
                    ToolResultRef(
                        tool_use_id=tid,
                        event_uuid=event.event_uuid,
                        line_number=event.line_number,
                        is_error=bool(block.get('is_error')),
                    )
                )
    return uses, results


def _build_real_rounds(events: list[TranscriptEvent]) -> list[RealConversationRound]:
    rounds: list[RealConversationRound] = []
    current_uuids: list[str] = []
    current_user: Optional[str] = None
    current_tools: list[str] = []
    has_assistant = False

    def flush() -> None:
        nonlocal current_uuids, current_user, current_tools, has_assistant
        if current_user and current_uuids:
            rounds.append(
                RealConversationRound(
                    real_user_event_uuid=current_user,
                    event_uuids=tuple(current_uuids),
                    tool_use_ids=tuple(current_tools),
                    has_assistant=has_assistant,
                )
            )
        current_uuids = []
        current_user = None
        current_tools = []
        has_assistant = False

    for evt in events:
        if evt.event_role == EventRole.REAL_USER and not evt.is_sidechain:
            flush()
            current_user = evt.event_uuid
            current_uuids = [evt.event_uuid]
            continue
        if current_user is None:
            continue
        if evt.event_role in {
            EventRole.ASSISTANT,
            EventRole.TOOL_RESULT_USER,
            EventRole.SYSTEM,
        } and not evt.is_sidechain:
            current_uuids.append(evt.event_uuid)
            if evt.event_role == EventRole.ASSISTANT:
                has_assistant = True
            uses, _ = _extract_tool_refs(evt)
            for use in uses:
                current_tools.append(use.tool_use_id)
        elif evt.event_role in {EventRole.SUMMARY, EventRole.META, EventRole.UNKNOWN}:
            # noise outside round chain — ignore for round assembly
            continue
        elif evt.is_sidechain or evt.event_role == EventRole.SIDECHAIN:
            continue
        else:
            continue
    flush()
    return rounds


def file_sha256(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_transcript(path: Union[str, Path]) -> TranscriptGraph:
    """Parse a Claude JSONL transcript into a TranscriptGraph.

    Opens the source file read-only. Never writes or truncates.
    """
    src = Path(path)
    if src.is_symlink():
        raise TranscriptReaderError(ReaderErrorCode.PATH_SYMLINK, str(src))
    if not src.is_file():
        raise TranscriptReaderError(ReaderErrorCode.PATH_NOT_FILE, str(src))

    graph = TranscriptGraph(session_id='', source_path=str(src))
    session_ids: set[str] = set()

    try:
        # O_RDONLY only — never create/truncate
        fd = os.open(str(src), os.O_RDONLY)
    except OSError as exc:
        raise TranscriptReaderError(ReaderErrorCode.IO_ERROR, str(exc)) from exc

    try:
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=True) as handle:
            byte_offset = 0
            for lineno, raw_line in enumerate(handle, 1):
                line_bytes = raw_line.encode('utf-8')
                line = raw_line.strip()
                if not line:
                    byte_offset += len(line_bytes)
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TranscriptReaderError(
                        ReaderErrorCode.INVALID_JSON,
                        f'line_{lineno}:{exc.msg}',
                    ) from exc
                if not isinstance(obj, dict):
                    raise TranscriptReaderError(
                        ReaderErrorCode.NOT_OBJECT,
                        f'line_{lineno}',
                    )

                uid = str(obj.get('uuid') or '')
                if not uid:
                    raise TranscriptReaderError(
                        ReaderErrorCode.MISSING_UUID,
                        f'line_{lineno}',
                    )
                if uid in graph.by_uuid:
                    raise TranscriptReaderError(
                        ReaderErrorCode.DUPLICATE_UUID,
                        uid,
                    )

                etype, erole, is_sidechain = _classify_event(obj)
                parent = obj.get('parentUuid')
                parent_uuid = None if parent is None else str(parent)
                sid = str(obj.get('sessionId') or '')
                if sid:
                    session_ids.add(sid)

                event = TranscriptEvent(
                    event_uuid=uid,
                    parent_uuid=parent_uuid,
                    session_id=sid,
                    event_role=erole,
                    event_type=etype,
                    raw=obj,
                    line_number=lineno,
                    is_sidechain=is_sidechain,
                    byte_offset=byte_offset,
                )
                graph.events.append(event)
                graph.by_uuid[uid] = event
                graph.children_by_parent.setdefault(parent_uuid, []).append(uid)

                if etype == EventType.SUMMARY:
                    graph.summary_uuids.append(uid)
                if is_sidechain or erole == EventRole.SIDECHAIN:
                    graph.sidechain_uuids.append(uid)
                if erole == EventRole.UNKNOWN:
                    graph.unknown_uuids.append(uid)

                uses, results = _extract_tool_refs(event)
                for use in uses:
                    if use.tool_use_id in graph.tool_uses:
                        graph.warnings.append(
                            f'duplicate_tool_use_id:{use.tool_use_id}'
                        )
                    graph.tool_uses[use.tool_use_id] = use
                for result in results:
                    if result.tool_use_id in graph.tool_results:
                        graph.warnings.append(
                            f'duplicate_tool_result_id:{result.tool_use_id}'
                        )
                    graph.tool_results[result.tool_use_id] = result

                byte_offset += len(line_bytes)
    except TranscriptReaderError:
        raise
    except OSError as exc:
        raise TranscriptReaderError(ReaderErrorCode.IO_ERROR, str(exc)) from exc

    if len(session_ids) == 1:
        graph.session_id = next(iter(session_ids))
    elif len(session_ids) > 1:
        graph.session_id = sorted(session_ids)[0]
        graph.warnings.append(f'multiple_session_ids:{len(session_ids)}')

    graph.real_rounds = _build_real_rounds(graph.events)
    return graph


@dataclass(frozen=True)
class SourceIntegritySnapshot:
    path: str
    sha256: str
    size: int


def snapshot_source(path: Union[str, Path]) -> SourceIntegritySnapshot:
    src = Path(path)
    return SourceIntegritySnapshot(
        path=str(src),
        sha256=file_sha256(src),
        size=src.stat().st_size,
    )


def assert_source_unchanged(
    before: SourceIntegritySnapshot,
    path: Optional[Union[str, Path]] = None,
) -> None:
    target = Path(path or before.path)
    after = snapshot_source(target)
    if after.sha256 != before.sha256 or after.size != before.size:
        raise AssertionError(
            'source transcript mutated: '
            f'sha {before.sha256} -> {after.sha256}; '
            f'size {before.size} -> {after.size}'
        )
    # byte-identical content check
    with open(before.path, 'rb') as a, open(target, 'rb') as b:
        if a.read() != b.read():
            raise AssertionError('source transcript byte content changed')
