"""Read-only Claude Code JSONL transcript reader (v0.2 Transcript Core).

Responsibilities:
- open source JSONL read-only
- parse line-by-line
- shared raw→formal boundary (project non-conversation bookkeeping; fail-closed
  on conversation-shaped uuid-less rows) before strict ingest
- preserve raw event objects for canonical rows
- build UUID / parent / tool indexes
- classify event roles (candidate user vs tool_result user vs meta)
- attribute sidechain impact via full parent graph (order-independent)
- report illegal JSON / duplicate UUID with explicit errors

Non-responsibilities (explicit):
- maintaining a Claude raw metadata type allowlist
- confirming real kitten/user identity (requires app mapping at Transform)
- round selection, rewriting, resume, DB mapping, mtime session pick, --continue
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from chat.claude_transcript_model import (
    CandidateConversationRound,
    EventRole,
    EventType,
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
    OFFSET_INVALID = 'READER_OFFSET_INVALID'
    OFFSET_BEYOND_SIZE = 'READER_OFFSET_BEYOND_SIZE'
    RANGE_MID_LINE = 'READER_RANGE_MID_LINE'
    RANGE_DECODE = 'READER_RANGE_DECODE'


class TranscriptReaderError(ValueError):
    def __init__(self, code: ReaderErrorCode, detail: str = ''):
        self.code = code
        msg = code.value if not detail else f'{code.value}:{detail}'
        super().__init__(msg)
        self.detail = detail


# Formal EventRole.META classification for uuid-bearing rows only.
# Not used as the uuid-less safety gate (see shared raw boundary below).
_FORMAL_META_EVENT_TYPES = frozenset({
    'queue-operation',
    'last-prompt',
    'result',
    'file-history-snapshot',
})


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _message_role(obj: Mapping[str, Any]) -> str:
    message = obj.get('message')
    if isinstance(message, dict):
        return str(message.get('role') or '')
    return ''


def _is_assistant_usage_observation(obj: Mapping[str, Any]) -> bool:
    """Structural: assistant usage bookkeeping, not a conversation node.

    type=assistant + requestId + message.usage — Claude often omits uuid here.
    """
    if str(obj.get('type') or '') != 'assistant':
        return False
    request_id = str(obj.get('requestId') or obj.get('request_id') or '').strip()
    if not request_id:
        return False
    message = obj.get('message')
    if not isinstance(message, dict):
        return False
    return isinstance(message.get('usage'), Mapping)


def _is_conversation_shaped(obj: Mapping[str, Any]) -> bool:
    """True when a raw object looks like a formal conversation event.

    Used only on the uuid-less path: conversation-shaped → fail-closed;
    otherwise treat as raw bookkeeping and project away before ingestion.
    """
    if _is_assistant_usage_observation(obj):
        return False
    etype = str(obj.get('type') or '')
    if etype in {'user', 'assistant'}:
        return True
    if _message_role(obj) in {'user', 'assistant'}:
        return True
    return False


def _raw_observation_kind(obj: Mapping[str, Any]) -> str:
    """Diagnostic label only — never the authority for skip-vs-fail."""
    if _is_assistant_usage_observation(obj):
        return 'assistant_usage_observation'
    etype = str(obj.get('type') or '')
    if etype == 'system' and str(obj.get('subtype') or ''):
        return f'system/{obj.get("subtype")}'
    return etype or 'raw_bookkeeping'


def _project_raw_object(
    obj: Mapping[str, Any],
    *,
    lineno: int,
) -> Optional[Mapping[str, Any]]:
    """Shared raw→formal boundary.

    Returns the canonical object for strict formal ingestion, or None when the
    row is raw bookkeeping to skip. Conversation-shaped uuid-less rows raise
    READER_MISSING_UUID. Uuid-bearing rows always proceed to formal ingest.
    """
    if not isinstance(obj, dict):
        raise TranscriptReaderError(ReaderErrorCode.NOT_OBJECT, f'line_{lineno}')

    if str(obj.get('uuid') or ''):
        return obj

    if _is_conversation_shaped(obj):
        raise TranscriptReaderError(ReaderErrorCode.MISSING_UUID, f'line_{lineno}')

    # Non-conversation uuid-less observation: project away before formal ingest.
    return None


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
        # Old system rows are never candidate users; Transform strips them.
        role = EventRole.SIDECHAIN if is_sidechain else EventRole.SYSTEM
        return EventType.SYSTEM, role, is_sidechain
    if etype_raw == 'user':
        if _is_tool_result_only_user(message):
            role = EventRole.SIDECHAIN if is_sidechain else EventRole.TOOL_RESULT_USER
        else:
            # Candidate only — mapping must confirm real kitten message later.
            role = EventRole.SIDECHAIN if is_sidechain else EventRole.CANDIDATE_USER
        return EventType.USER, role, is_sidechain
    if etype_raw in _FORMAL_META_EVENT_TYPES:
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


def _has_sidechain_between(
    parent_evt: TranscriptEvent,
    child_evt: TranscriptEvent,
    events: list[TranscriptEvent],
) -> bool:
    parent_off = int(parent_evt.byte_offset or 0)
    child_off = int(child_evt.byte_offset or 0)
    for evt in events:
        off = int(evt.byte_offset or 0)
        if off <= parent_off or off >= child_off:
            continue
        if evt.is_sidechain or evt.event_role == EventRole.SIDECHAIN:
            return True
    return False


def _resolve_attachment_continuation_parent(
    evt: TranscriptEvent,
    direct_parent: TranscriptEvent,
    *,
    by_uuid: dict[str, TranscriptEvent],
) -> Optional[TranscriptEvent]:
    """Resolve a candidate user through attachment-only parent bridges."""
    session_id = str(evt.session_id or '')
    if not session_id:
        return None

    visited = {str(evt.event_uuid)}
    cursor = direct_parent
    while True:
        cursor_uuid = str(cursor.event_uuid or '')
        if not cursor_uuid or cursor_uuid in visited:
            return None
        visited.add(cursor_uuid)

        if (
            cursor.is_sidechain
            or cursor.event_role == EventRole.SIDECHAIN
            or str(cursor.session_id or '') != session_id
        ):
            return None

        if str(cursor.raw.get('type') or '') != 'attachment':
            if cursor.event_role != EventRole.CANDIDATE_USER:
                return None
            return cursor

        if str(cursor.raw.get('uuid') or '') != cursor_uuid:
            return None
        parent_uuid = str(cursor.parent_uuid or '')
        if not parent_uuid:
            return None
        cursor = by_uuid.get(parent_uuid)
        if cursor is None:
            return None


def _is_user_continuation(
    evt: TranscriptEvent,
    *,
    by_uuid: dict[str, TranscriptEvent],
    events: list[TranscriptEvent],
) -> bool:
    if evt.event_role != EventRole.CANDIDATE_USER or evt.is_sidechain:
        return False
    parent = by_uuid.get(evt.parent_uuid or '')
    if parent is None:
        return False

    # Preserve the original direct-parent contract exactly.
    if parent.event_role == EventRole.CANDIDATE_USER:
        if parent.is_sidechain:
            return False
        return not _has_sidechain_between(parent, evt, events)

    # Only raw attachment rows are transparent; all other roles stop traversal.
    if str(parent.raw.get('type') or '') != 'attachment':
        return False
    ancestor = _resolve_attachment_continuation_parent(evt, parent, by_uuid=by_uuid)
    if ancestor is None:
        return False
    return not _has_sidechain_between(ancestor, evt, events)


def _reclassify_user_continuations(
    events: list[TranscriptEvent],
) -> tuple[list[TranscriptEvent], dict[str, TranscriptEvent]]:
    """Parent-chained main-chain users stay in the same candidate round."""
    by_uuid = {evt.event_uuid: evt for evt in events}
    updated: list[TranscriptEvent] = []
    new_by_uuid: dict[str, TranscriptEvent] = {}
    for evt in events:
        if _is_user_continuation(evt, by_uuid=by_uuid, events=events):
            evt = replace(evt, event_role=EventRole.USER_CONTINUATION)
        updated.append(evt)
        new_by_uuid[evt.event_uuid] = evt
    return updated, new_by_uuid


def _build_candidate_rounds(events: list[TranscriptEvent]) -> list[CandidateConversationRound]:
    """Phase 1: assemble main-chain candidate rounds only.

    SYSTEM / summary / meta / sidechain never join ``event_uuids``.
    Sidechain impact is attached later via ``_attach_sidechain_impacts``.
    """
    rounds: list[CandidateConversationRound] = []
    current_uuids: list[str] = []
    current_user: Optional[str] = None
    current_tools: list[str] = []
    has_assistant = False

    def flush() -> None:
        nonlocal current_uuids, current_user, current_tools, has_assistant
        if current_user and current_uuids:
            rounds.append(
                CandidateConversationRound(
                    candidate_user_event_uuid=current_user,
                    event_uuids=tuple(current_uuids),
                    tool_use_ids=tuple(current_tools),
                    has_assistant=has_assistant,
                    sidechain_impact_uuids=(),
                )
            )
        current_uuids = []
        current_user = None
        current_tools = []
        has_assistant = False

    for evt in events:
        if evt.event_role == EventRole.CANDIDATE_USER and not evt.is_sidechain:
            flush()
            current_user = evt.event_uuid
            current_uuids = [evt.event_uuid]
            continue

        if current_user is None:
            continue

        # Sidechain deferred to phase-2 parent-graph attribution
        if evt.is_sidechain or evt.event_role == EventRole.SIDECHAIN:
            continue

        # Old SYSTEM never joins a migrateable round
        if evt.event_role == EventRole.SYSTEM:
            continue

        if evt.event_role in {
            EventRole.ASSISTANT,
            EventRole.TOOL_RESULT_USER,
            EventRole.USER_CONTINUATION,
        }:
            current_uuids.append(evt.event_uuid)
            if evt.event_role == EventRole.ASSISTANT:
                has_assistant = True
            uses, _ = _extract_tool_refs(evt)
            for use in uses:
                current_tools.append(use.tool_use_id)
            continue

        if evt.event_role in {EventRole.SUMMARY, EventRole.META, EventRole.UNKNOWN}:
            continue

    flush()
    return rounds


def _attach_sidechain_impacts(
    events: list[TranscriptEvent],
    candidate_rounds: list[CandidateConversationRound],
    by_uuid: dict[str, TranscriptEvent],
) -> tuple[list[CandidateConversationRound], list[str]]:
    """Phase 2: attribute sidechain events via full parent graph (order-independent).

    Walks each sidechain's ``parentUuid`` ancestors until a candidate main-chain
    UUID is hit. Delayed / out-of-order sidechain rows still pollute their true
    parent round. Missing parent → ``unattributed_sidechain:<uuid>``. Parent
    cycle → ``sidechain_parent_cycle:<uuid>``. Neither migrates; neither
    contaminates unrelated rounds.
    """
    main_to_round: dict[str, int] = {}
    for idx, rnd in enumerate(candidate_rounds):
        for uid in rnd.event_uuids:
            main_to_round[uid] = idx

    impacts: list[list[str]] = [[] for _ in candidate_rounds]
    seen_impact: list[set[str]] = [set() for _ in candidate_rounds]
    warnings: list[str] = []

    for evt in events:
        if not (evt.is_sidechain or evt.event_role == EventRole.SIDECHAIN):
            continue

        visited: set[str] = set()
        cursor: TranscriptEvent = evt
        attributed: Optional[int] = None
        warning: Optional[str] = None

        while True:
            if cursor.event_uuid in visited:
                warning = f'sidechain_parent_cycle:{evt.event_uuid}'
                break
            visited.add(cursor.event_uuid)

            parent = cursor.parent_uuid
            if parent is None:
                warning = f'unattributed_sidechain:{evt.event_uuid}'
                break
            if parent not in by_uuid:
                warning = f'unattributed_sidechain:{evt.event_uuid}'
                break
            if parent in main_to_round:
                attributed = main_to_round[parent]
                break
            cursor = by_uuid[parent]

        if warning is not None:
            warnings.append(warning)
            continue
        if attributed is None:
            warnings.append(f'unattributed_sidechain:{evt.event_uuid}')
            continue
        if evt.event_uuid in seen_impact[attributed]:
            continue
        seen_impact[attributed].add(evt.event_uuid)
        impacts[attributed].append(evt.event_uuid)

    updated: list[CandidateConversationRound] = []
    for idx, rnd in enumerate(candidate_rounds):
        updated.append(
            CandidateConversationRound(
                candidate_user_event_uuid=rnd.candidate_user_event_uuid,
                event_uuids=rnd.event_uuids,
                tool_use_ids=rnd.tool_use_ids,
                has_assistant=rnd.has_assistant,
                sidechain_impact_uuids=tuple(impacts[idx]),
            )
        )
    return updated, warnings


def file_sha256(path: Union[str, Path]) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _finalize_graph(graph: TranscriptGraph) -> TranscriptGraph:
    session_ids = {e.session_id for e in graph.events if e.session_id}
    if len(session_ids) == 1:
        graph.session_id = next(iter(session_ids))
    elif len(session_ids) > 1:
        graph.session_id = sorted(session_ids)[0]
        graph.warnings.append(f'multiple_session_ids:{len(session_ids)}')

    events, by_uuid = _reclassify_user_continuations(graph.events)
    graph.events = events
    graph.by_uuid = by_uuid
    graph.children_by_parent = {}
    for evt in events:
        graph.children_by_parent.setdefault(evt.parent_uuid, []).append(evt.event_uuid)

    main_rounds = _build_candidate_rounds(graph.events)
    graph.candidate_rounds, side_warnings = _attach_sidechain_impacts(
        graph.events,
        main_rounds,
        graph.by_uuid,
    )
    graph.warnings.extend(side_warnings)
    return graph


def _ingest_json_object(
    graph: TranscriptGraph,
    obj: Mapping[str, Any],
    *,
    lineno: int,
    byte_offset: int,
) -> None:
    canonical = _project_raw_object(obj, lineno=lineno)
    if canonical is None:
        graph.warnings.append(
            f'ignored_raw_observation:line_{lineno}:{_raw_observation_kind(obj)}',
        )
        return

    uid = str(canonical.get('uuid') or '')
    if not uid:
        # Defensive: projection must fail-closed conversation-shaped uuid-less.
        raise TranscriptReaderError(ReaderErrorCode.MISSING_UUID, f'line_{lineno}')
    if uid in graph.by_uuid:
        raise TranscriptReaderError(ReaderErrorCode.DUPLICATE_UUID, uid)

    etype, erole, is_sidechain = _classify_event(canonical)
    parent = canonical.get('parentUuid')
    parent_uuid = None if parent is None else str(parent)
    sid = str(canonical.get('sessionId') or '')

    event = TranscriptEvent(
        event_uuid=uid,
        parent_uuid=parent_uuid,
        session_id=sid,
        event_role=erole,
        event_type=etype,
        raw=canonical,
        line_number=lineno,
        is_sidechain=is_sidechain,
        byte_offset=byte_offset,
    )
    graph.events.append(event)
    graph.by_uuid[uid] = event
    graph.children_by_parent.setdefault(parent_uuid, []).append(uid)

    if etype == EventType.SUMMARY:
        graph.summary_uuids.append(uid)
    if etype == EventType.SYSTEM or erole == EventRole.SYSTEM:
        graph.system_uuids.append(uid)
    if is_sidechain or erole == EventRole.SIDECHAIN:
        graph.sidechain_uuids.append(uid)
    if erole == EventRole.UNKNOWN:
        graph.unknown_uuids.append(uid)

    uses, results = _extract_tool_refs(event)
    for use in uses:
        if use.tool_use_id in graph.tool_uses:
            graph.warnings.append(f'duplicate_tool_use_id:{use.tool_use_id}')
        graph.tool_uses[use.tool_use_id] = use
    for result in results:
        if result.tool_use_id in graph.tool_results:
            graph.warnings.append(f'duplicate_tool_result_id:{result.tool_use_id}')
        graph.tool_results[result.tool_use_id] = result


def _parse_transcript_bytes(
    data: bytes,
    *,
    base_offset: int,
    source_path: str,
    start_lineno: int = 1,
) -> TranscriptGraph:
    """Parse a contiguous UTF-8 JSONL byte slice into a TranscriptGraph."""
    graph = TranscriptGraph(session_id='', source_path=source_path)
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise TranscriptReaderError(ReaderErrorCode.RANGE_DECODE, str(exc)) from exc

    # Split keeping track of absolute byte offsets via UTF-8 lengths.
    pos = 0
    byte_pos = 0
    lineno = start_lineno
    length = len(text)
    while pos < length:
        nl = text.find('\n', pos)
        if nl < 0:
            raw_line = text[pos:]
            line_end = length
        else:
            raw_line = text[pos:nl + 1]
            line_end = nl + 1

        line_bytes = raw_line.encode('utf-8')
        abs_offset = base_offset + byte_pos
        line = raw_line.strip()
        if line:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TranscriptReaderError(
                    ReaderErrorCode.INVALID_JSON,
                    f'line_{lineno}:{exc.msg}',
                ) from exc
            _ingest_json_object(graph, obj, lineno=lineno, byte_offset=abs_offset)
        pos = line_end
        byte_pos += len(line_bytes)
        lineno += 1
        if nl < 0:
            break

    return _finalize_graph(graph)


def _open_readonly_file(path: Path) -> int:
    if path.is_symlink():
        raise TranscriptReaderError(ReaderErrorCode.PATH_SYMLINK, str(path))
    if not path.is_file():
        raise TranscriptReaderError(ReaderErrorCode.PATH_NOT_FILE, str(path))
    try:
        return os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        raise TranscriptReaderError(ReaderErrorCode.IO_ERROR, str(exc)) from exc


def read_transcript(path: Union[str, Path]) -> TranscriptGraph:
    """Parse a Claude JSONL transcript into a TranscriptGraph.

    Opens the source file read-only and streams line-by-line (constant-ish
    memory). Never writes or truncates. Does not load the whole file at once.
    """
    src = Path(path)
    fd = _open_readonly_file(src)
    graph = TranscriptGraph(session_id='', source_path=str(src))
    try:
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=True) as handle:
            byte_offset = 0
            for lineno, raw_line in enumerate(handle, 1):
                line_bytes = raw_line.encode('utf-8')
                line = raw_line.strip()
                if line:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise TranscriptReaderError(
                            ReaderErrorCode.INVALID_JSON,
                            f'line_{lineno}:{exc.msg}',
                        ) from exc
                    _ingest_json_object(
                        graph, obj, lineno=lineno, byte_offset=byte_offset,
                    )
                byte_offset += len(line_bytes)
    except TranscriptReaderError:
        raise
    except OSError as exc:
        raise TranscriptReaderError(ReaderErrorCode.IO_ERROR, str(exc)) from exc

    return _finalize_graph(graph)


def read_transcript_range(
    path: Union[str, Path],
    start_offset: int,
    end_offset: int,
) -> TranscriptGraph:
    """Read-only parse of ``[start_offset, end_offset)`` bytes.

    Fail-closed rules:
    - offsets must be non-negative ints with ``start <= end``
    - file size must be ``>= end_offset`` (truncation / short file refused)
    - unless ``start == 0``, the byte immediately before start must be ``\\n``
    - range must not end mid-line when more file bytes follow the slice
    - never scans past ``end_offset`` (caller's observed end is authoritative)
    """
    try:
        start = int(start_offset)
        end = int(end_offset)
    except (TypeError, ValueError) as exc:
        raise TranscriptReaderError(ReaderErrorCode.OFFSET_INVALID, 'non_int') from exc
    if start < 0 or end < 0 or end < start:
        raise TranscriptReaderError(
            ReaderErrorCode.OFFSET_INVALID,
            f'{start}:{end}',
        )

    src = Path(path)
    fd = _open_readonly_file(src)
    try:
        size = int(os.fstat(fd).st_size)
        if size < start or size < end:
            raise TranscriptReaderError(
                ReaderErrorCode.OFFSET_BEYOND_SIZE,
                f'size={size}:start={start}:end={end}',
            )
        if start == end:
            return _finalize_graph(
                TranscriptGraph(session_id='', source_path=str(src)),
            )
        # Start must be on a line boundary (except offset 0).
        if start > 0:
            os.lseek(fd, start - 1, os.SEEK_SET)
            prev = os.read(fd, 1)
            if prev != b'\n':
                raise TranscriptReaderError(
                    ReaderErrorCode.RANGE_MID_LINE,
                    f'start={start}:prev={prev!r}',
                )
        else:
            os.lseek(fd, 0, os.SEEK_SET)
        data = os.read(fd, end - start)
        if len(data) != end - start:
            raise TranscriptReaderError(
                ReaderErrorCode.IO_ERROR,
                f'short_read:{len(data)}!={end - start}',
            )
        # Mid-line cut at end: slice does not end with newline but file continues.
        if end < size and (not data.endswith(b'\n')):
            raise TranscriptReaderError(
                ReaderErrorCode.RANGE_MID_LINE,
                f'end={end}:size={size}',
            )
    except TranscriptReaderError:
        raise
    except OSError as exc:
        raise TranscriptReaderError(ReaderErrorCode.IO_ERROR, str(exc)) from exc
    finally:
        os.close(fd)

    return _parse_transcript_bytes(data, base_offset=start, source_path=str(src))


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
    with open(before.path, 'rb') as a, open(target, 'rb') as b:
        if a.read() != b.read():
            raise AssertionError('source transcript byte content changed')
