"""Deterministic transcript-backed projection for one completed Claude turn.

The stream is intentionally not read here.  It is a live preview only; this
module reads the closed JSONL range captured for the provider turn and builds
the durable content and ordered display timeline together.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from chat.choices_contract import extract_choices
from chat.display_segments import (
    DisplaySegmentAccumulator,
    finalize_display_segments,
    strip_save_markers,
)
from chat.display_thinking import DisplayThinkingStreamParser


class CanonicalTurnError(RuntimeError):
    def __init__(self, message: str, *, error_code: str = 'canonical_turn_unavailable'):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class CanonicalTurn:
    content: str
    thinking: str
    display_segments: str
    tool_calls: tuple[dict[str, Any], ...]
    choices: tuple[str, ...]
    provider_rounds: tuple[dict[str, Any], ...]
    stop_reason: str
    terminal_state: str
    transcript_identity: dict[str, Any]
    projection_hash: str


def _json_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get('content')
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}]
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping) and item.get('type') == 'text':
                parts.append(str(item.get('text') or ''))
            elif isinstance(item, str):
                parts.append(item)
        return ''.join(parts)
    if value is None:
        return ''
    try:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    except TypeError:
        return str(value)


def _read_closed_range(path: str, start_offset: int, end_offset: int) -> list[dict[str, Any]]:
    try:
        start = int(start_offset)
        end = int(end_offset)
    except (TypeError, ValueError) as exc:
        raise CanonicalTurnError(
            'transcript offsets are not integers',
            error_code='transcript_offset_invalid',
        ) from exc
    if start < 0 or end <= start:
        raise CanonicalTurnError('transcript range is empty', error_code='transcript_range_empty')
    source = Path(str(path or ''))
    if source.is_symlink() or not source.is_file():
        raise CanonicalTurnError(
            'transcript source is unavailable',
            error_code='transcript_source_unavailable',
        )
    try:
        size = source.stat().st_size
        if size < end:
            raise CanonicalTurnError(
                'transcript range is not final',
                error_code='transcript_range_not_final',
            )
        with source.open('rb') as handle:
            if start:
                handle.seek(start - 1)
                if handle.read(1) != b'\n':
                    raise CanonicalTurnError(
                        'transcript start is mid-line',
                        error_code='transcript_range_mid_line',
                    )
            handle.seek(start)
            data = handle.read(end - start)
        if len(data) != end - start or (end < size and not data.endswith(b'\n')):
            raise CanonicalTurnError(
                'transcript range is not line-closed',
                error_code='transcript_range_not_final',
            )
        rows: list[dict[str, Any]] = []
        for line in data.decode('utf-8').splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise CanonicalTurnError(
                    'transcript JSON is incomplete',
                    error_code='transcript_json_invalid',
                ) from exc
            if isinstance(value, dict):
                rows.append(value)
        return rows
    except UnicodeDecodeError as exc:
        raise CanonicalTurnError(
            'transcript is not valid UTF-8',
            error_code='transcript_decode_error',
        ) from exc
    except OSError as exc:
        raise CanonicalTurnError('transcript read failed', error_code='transcript_read_error') from exc


def _normalize_projection_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_projection_value(value[key])
            for key in sorted(value.keys(), key=lambda item: str(item))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_projection_value(item) for item in value]
    return value


def _coerce_display_segments(display_segments: str | Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(display_segments, str):
        try:
            parsed = json.loads(display_segments) if display_segments else []
        except (TypeError, ValueError):
            parsed = []
    else:
        parsed = list(display_segments)
    return parsed if isinstance(parsed, list) else []


def _hash_projection(
    content: str,
    thinking: str,
    segments: list[dict[str, Any]],
    tool_calls: Iterable[Mapping[str, Any]],
    choices: Iterable[Any],
) -> str:
    normalized_tool_calls = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        normalized_tool_calls.append({
            'id': str(call.get('id') or ''),
            'name': str(call.get('name') or ''),
            'args': _normalize_projection_value(call.get('args') or {}),
            'result': _normalize_projection_value(call.get('result') if call.get('result') is not None else ''),
            'success': bool(call.get('success')),
        })
    body = json.dumps(
        {
            'content': str(content or '').strip(),
            'thinking': str(thinking or ''),
            'display_segments': _normalize_projection_value(segments),
            'tool_calls': normalized_tool_calls,
            'choices': [str(choice) for choice in choices],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(body).hexdigest()


def _feed_parser(
    parser: DisplayThinkingStreamParser,
    accumulator: DisplaySegmentAccumulator,
    event: str,
    value: Any,
) -> None:
    emitted = parser.feed_native(value) if event == 'think' else parser.feed_text(value)
    for kind, text in emitted:
        if kind == 'think':
            accumulator.append_thinking(text)
        elif kind == 'text':
            accumulator.append_text(text)


def build_canonical_turn(
    transcript_path: str,
    *,
    start_offset: int,
    end_offset: int,
    session_id: str,
    mode: str = 'auto',
    stop_reason: str = 'end_turn',
    mapping_status: str = 'UNVERIFIED',
    message_id: int | None = None,
    context_id: int | None = None,
    context_epoch: int | None = None,
    resident_generation: int | None = None,
) -> CanonicalTurn:
    """Build one final projection from a closed provider transcript range."""
    rows = _read_closed_range(transcript_path, start_offset, end_offset)
    expected_sid = str(session_id or '').strip()
    if not expected_sid:
        raise CanonicalTurnError(
            'transcript session identity is missing',
            error_code='transcript_session_missing',
        )

    parser = DisplayThinkingStreamParser(mode)
    segments = DisplaySegmentAccumulator()
    tool_calls: list[dict[str, Any]] = []
    tool_index_by_id: dict[str, int] = {}
    tool_result_seen: set[str] = set()
    provider_rounds: list[dict[str, Any]] = []
    terminal_result = False
    terminal_assistant = False
    terminal_assistant_reason = ''

    for row in rows:
        row_sid = str(row.get('sessionId') or '').strip()
        if row_sid and row_sid != expected_sid:
            raise CanonicalTurnError(
                'transcript session identity mismatch',
                error_code='transcript_session_mismatch',
            )
        row_type = str(row.get('type') or '')
        if row_type == 'result':
            if bool(row.get('is_error')):
                raise CanonicalTurnError(
                    'provider result is not successful',
                    error_code='provider_result_error',
                )
            terminal_result = True
            if row.get('stop_reason'):
                terminal_assistant_reason = str(row.get('stop_reason') or '')
            continue
        if row_type == 'assistant':
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            blocks = _json_blocks(message)
            if not blocks:
                continue
            round_row = {
                'index': len(provider_rounds) + 1,
                'event_uuid': str(row.get('uuid') or ''),
                'text_block_count': 0,
                'tool_use_count': 0,
                'stop_reason': str(message.get('stop_reason') or ''),
            }
            for block in blocks:
                block_type = str(block.get('type') or '')
                if block_type == 'thinking':
                    _feed_parser(parser, segments, 'think', block.get('thinking', block.get('text', '')))
                elif block_type == 'text':
                    round_row['text_block_count'] += 1
                    _feed_parser(parser, segments, 'text', block.get('text', ''))
                elif block_type == 'tool_use':
                    tool_id = str(block.get('id') or '').strip()
                    if not tool_id:
                        raise CanonicalTurnError(
                            'tool_use identity is missing',
                            error_code='tool_use_identity_missing',
                        )
                    if tool_id in tool_index_by_id:
                        raise CanonicalTurnError(
                            'tool_use identity is duplicated',
                            error_code='tool_use_duplicate',
                        )
                    index = len(tool_calls)
                    tool_index_by_id[tool_id] = index
                    tool_calls.append({
                        'id': tool_id,
                        'name': str(block.get('name') or ''),
                        'args': block.get('input') or {},
                        'result': '',
                        'success': True,
                    })
                    round_row['tool_use_count'] += 1
                    segments.append_tool(index)
            provider_rounds.append(round_row)
            if round_row['text_block_count'] or round_row['tool_use_count']:
                terminal_assistant = True
            if message.get('stop_reason'):
                terminal_assistant_reason = str(message.get('stop_reason') or '')
        elif row_type == 'user':
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            for block in _json_blocks(message):
                if str(block.get('type') or '') != 'tool_result':
                    continue
                tool_id = str(block.get('tool_use_id') or '').strip()
                if tool_id not in tool_index_by_id:
                    raise CanonicalTurnError(
                        'tool_result has no preceding tool_use',
                        error_code='tool_result_unmatched',
                    )
                if tool_id in tool_result_seen:
                    raise CanonicalTurnError(
                        'tool_result is duplicated',
                        error_code='tool_result_duplicate',
                    )
                tool_result_seen.add(tool_id)
                idx = tool_index_by_id[tool_id]
                tool_calls[idx]['result'] = _tool_result_text(block.get('content'))
                tool_calls[idx]['success'] = not bool(block.get('is_error'))

    if not terminal_result:
        raise CanonicalTurnError('provider result is not final', error_code='provider_result_missing')
    if not terminal_assistant:
        raise CanonicalTurnError(
            'terminal assistant projection is missing',
            error_code='canonical_assistant_missing',
        )
    for call in tool_calls:
        if not call.get('id'):
            raise CanonicalTurnError(
                'tool identity is incomplete',
                error_code='tool_identity_incomplete',
            )
        if str(call.get('id') or '') not in tool_result_seen:
            raise CanonicalTurnError(
                'tool_result is missing',
                error_code='tool_result_missing',
            )

    for kind, value in parser.finish():
        if kind == 'think':
            segments.append_thinking(value)
        elif kind == 'text':
            segments.append_text(value)
    content, choices = extract_choices(strip_save_markers(parser.text))
    content = str(content or '').strip()
    if not content and choices:
        content = '[选项: ' + ' / '.join(choices) + ']'
    if not content:
        raise CanonicalTurnError(
            'canonical assistant content is empty',
            error_code='canonical_content_empty',
        )
    canonical_segments = finalize_display_segments(
        segments.as_list(),
        visible_text=content,
    )
    if not canonical_segments:
        raise CanonicalTurnError(
            'canonical display segments are empty',
            error_code='canonical_segments_empty',
        )
    terminal_stop_reason = str(stop_reason or terminal_assistant_reason or 'end_turn')
    identity = {
        'path': os.path.abspath(str(transcript_path)),
        'start_offset': int(start_offset),
        'end_offset': int(end_offset),
        'session_id': expected_sid,
        'mapping_status': str(mapping_status or 'UNVERIFIED'),
        'message_id': int(message_id) if message_id is not None else None,
        'context_id': int(context_id) if context_id is not None else None,
        'context_epoch': int(context_epoch) if context_epoch is not None else None,
        'resident_generation': int(resident_generation) if resident_generation is not None else None,
    }
    return CanonicalTurn(
        content=content,
        thinking=parser.thinking,
        display_segments=json.dumps(canonical_segments, ensure_ascii=False, separators=(',', ':')),
        tool_calls=tuple(tool_calls),
        choices=tuple(choices),
        provider_rounds=tuple(provider_rounds),
        stop_reason=terminal_stop_reason,
        terminal_state='confirmed',
        transcript_identity=identity,
        projection_hash=_hash_projection(
            content,
            parser.thinking,
            canonical_segments,
            tool_calls,
            choices,
        ),
    )


def projection_hash(
    content: str,
    display_segments: str | Iterable[dict[str, Any]],
    *,
    thinking: str = '',
    tool_calls: Iterable[Mapping[str, Any]] = (),
    choices: Iterable[Any] = (),
) -> str:
    return _hash_projection(
        str(content or '').strip(),
        str(thinking or ''),
        _coerce_display_segments(display_segments),
        tool_calls,
        choices,
    )
