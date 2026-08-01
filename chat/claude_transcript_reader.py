"""Read-only Claude Code JSONL transcript reader (v0.2 Transcript Core).

Responsibilities:
- open source JSONL read-only
- parse line-by-line
- preserve raw event objects
- build UUID / parent / tool indexes
- classify event roles (candidate user vs tool_result user vs meta)
- attribute sidechain impact via full parent graph (order-independent)
- report illegal JSON / duplicate UUID with explicit errors

Non-responsibilities (explicit):
- confirming real kitten/user identity (requires app mapping at Transform)
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


# Claude Code raw JSONL bookkeeping rows that may omit uuid. Keep this list
# aligned with tools/claude_forge_live_gate.KNOWN_METADATA_TYPES (+ turn_duration
# and assistant usage observations). Unknown uuid-less objects still fail closed.
_IGNORABLE_RAW_METADATA_TYPES = frozenset({
    'file-history-snapshot',
    'queue-operation',
    'agent-name',
    'custom-title',
    'progress',
})


def _content_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _is_assistant_usage_observation(obj: Mapping[str, Any]) -> bool:
    """True for Claude assistant usage bookkeeping (not a conversation node).

    Mirrors tools.claude_forge_live_gate.classify_raw_jsonl_event for the
    uuid-less / non-canonical path: type=assistant + requestId + message.usage.
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


def _is_ignorable_raw_metadata(obj: Mapping[str, Any]) -> bool:
    """Return True when a uuid-less raw object is known non-conversation metadata.

    Must stay narrow: never treat bare missing-uuid conversation events
    (user / real assistant / tool rows / unknown types) as ignorable.
    """
    etype = str(obj.get('type') or '')
    if etype in _IGNORABLE_RAW_METADATA_TYPES:
        return True
    if etype == 'system' and str(obj.get('subtype') or '') == 'turn_duration':
        return True
    if _is_assistant_usage_observation(obj):
        return True
    return False


def _raw_metadata_kind(obj: Mapping[str, Any]) -> str:
    etype = str(obj.get('type') or '')
    if etype in _IGNORABLE_RAW_METADATA_TYPES:
        return etype
    if etype == 'system' and str(obj.get('subtype') or '') == 'turn_duration':
        return 'system/turn_duration'
    if _is_assistant_usage_observation(obj):
        return 'assistant_usage_observation'
    return etype or '<missing>'


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

        if evt.event_role in {EventRole.ASSISTANT, EventRole.TOOL_RESULT_USER}:
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
    if not isinstance(obj, dict):
        raise TranscriptReaderError(ReaderErrorCode.NOT_OBJECT, f'line_{lineno}')

    uid = str(obj.get('uuid') or '')
    if not uid:
        # Known Claude raw bookkeeping may omit uuid; skip without entering the
        # graph. Conversational / unknown uuid-less rows still fail closed.
        if _is_ignorable_raw_metadata(obj):
            graph.warnings.append(
                f'ignored_raw_metadata:line_{lineno}:{_raw_metadata_kind(obj)}',
            )
            return
        raise TranscriptReaderError(ReaderErrorCode.MISSING_UUID, f'line_{lineno}')
    if uid in graph.by_uuid:
        raise TranscriptReaderError(ReaderErrorCode.DUPLICATE_UUID, uid)

    etype, erole, is_sidechain = _classify_event(obj)
    parent = obj.get('parentUuid')
    parent_uuid = None if parent is None else str(parent)
    sid = str(obj.get('sessionId') or '')

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
