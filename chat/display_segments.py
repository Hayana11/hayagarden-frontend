"""Pure durable presentation cleanup for interleaved assistant streams."""
from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterable

from chat.choices_contract import extract_choices

_SAVE_RE = re.compile(r'\[\[SAVE(?::\s*(.*?))?\]\]', re.IGNORECASE | re.DOTALL)
_TOOL_FENCE_RE = re.compile(
    r'\x60\x60\x60tool_(?:use|result)\s.*?\x60\x60\x60\s*',
    re.DOTALL,
)


def extract_save_markers(text: str) -> list[str]:
    """Return marker payloads without performing the save side effect."""
    return [
        str(item or '').strip()
        for item in _SAVE_RE.findall(str(text or ''))
        if str(item or '').strip()
    ]


def has_save_markers(text: str) -> bool:
    return bool(_SAVE_RE.search(str(text or '')))


def strip_save_markers(text: str) -> str:
    """Purely remove SAVE markers; never imports or calls memory persistence."""
    return _SAVE_RE.sub('', str(text or ''))


def clean_display_text(text: str) -> str:
    """Apply the pure text transforms used by canonical durable presentation."""
    cleaned = strip_save_markers(text)
    cleaned, _choices = extract_choices(cleaned)
    cleaned = _TOOL_FENCE_RE.sub('', cleaned)
    return cleaned.strip()


class DisplaySegmentAccumulator:
    """Merge adjacent deltas while retaining thinking/text/tool presentation order."""

    def __init__(self) -> None:
        self._segments: list[dict[str, Any]] = []

    def _append_delta(self, kind: str, value: str) -> None:
        text = str(value or '')
        if not text:
            return
        if self._segments and self._segments[-1].get('type') == kind:
            self._segments[-1]['text'] = str(self._segments[-1].get('text') or '') + text
            return
        self._segments.append({'type': kind, 'text': text})

    def append_thinking(self, text: str) -> None:
        self._append_delta('thinking', text)

    def append_text(self, text: str) -> None:
        self._append_delta('text', text)

    def append_tool(self, tool_index: int) -> None:
        try:
            index = int(tool_index)
        except (TypeError, ValueError):
            return
        if index < 0:
            return
        if (
            self._segments
            and self._segments[-1].get('type') == 'tool'
            and self._segments[-1].get('tool_index') == index
        ):
            return
        self._segments.append({'type': 'tool', 'tool_index': index})

    def apply_tool_result(self, _tool_index: int) -> None:
        return

    def as_list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._segments]

    def to_json(self) -> str:
        return json.dumps(self._segments, ensure_ascii=False, separators=(',', ':')) if self._segments else ''


def _project_visible_text(segments: list[dict[str, Any]], visible_text: str) -> list[dict[str, Any]]:
    """Project canonical text back onto text slots without moving tool slots."""
    text_slots: list[tuple[int, int, int, str]] = []
    raw_offset = 0
    for index, segment in enumerate(segments):
        if segment.get('type') != 'text':
            continue
        raw = str(segment.get('text') or '')
        text_slots.append((index, raw_offset, raw_offset + len(raw), raw))
        raw_offset += len(raw)
    if not text_slots:
        return segments + ([{'type': 'text', 'text': visible_text}] if visible_text else [])

    raw_text = ''.join(slot[3] for slot in text_slots)
    projected = {index: [] for index, _start, _end, _raw in text_slots}

    def slot_for_offset(offset: int) -> int:
        for index, start, end, _raw in text_slots:
            if start <= offset <= end:
                return index
        return text_slots[-1][0]

    def append_equal(start: int, end: int, value: str) -> None:
        value_cursor = 0
        for index, slot_start, slot_end, _raw in text_slots:
            left = max(start, slot_start)
            right = min(end, slot_end)
            if right <= left:
                continue
            length = right - left
            projected[index].append(value[value_cursor:value_cursor + length])
            value_cursor += length

    matcher = difflib.SequenceMatcher(None, raw_text, visible_text, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            append_equal(i1, i2, visible_text[j1:j2])
        elif tag == 'insert':
            projected[slot_for_offset(i1)].append(visible_text[j1:j2])

    rendered: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(segments):
        if segment.get('type') == 'text':
            value = ''.join(projected[segment_index])
            if value:
                rendered.append({'type': 'text', 'text': value})
        else:
            rendered.append(dict(segment))
    joined = ''.join(item.get('text', '') for item in rendered if item.get('type') == 'text')
    if joined != visible_text:
        rendered = [item for item in rendered if item.get('type') != 'text']
        insert_at = next((i for i, item in enumerate(rendered) if item.get('type') == 'tool'), len(rendered))
        rendered.insert(insert_at, {'type': 'text', 'text': visible_text})
    return rendered


def finalize_display_segments(segments: Iterable[dict[str, Any]], *, visible_text: str | None = None) -> list[dict[str, Any]]:
    """Purely finalize raw segments to the canonical visible assistant text."""
    source = [dict(segment) for segment in segments]
    cleaned: list[dict[str, Any]] = []
    for segment in source:
        kind = segment.get('type')
        if kind in ('thinking', 'text'):
            text = clean_display_text(str(segment.get('text') or ''))
            if text:
                cleaned.append({'type': kind, 'text': text})
        elif kind == 'tool':
            try:
                index = int(segment.get('tool_index'))
            except (TypeError, ValueError):
                continue
            if index >= 0:
                cleaned.append({'type': 'tool', 'tool_index': index})
    if visible_text is None:
        return cleaned
    canonical = str(visible_text or '').strip()
    current = ''.join(item.get('text', '') for item in cleaned if item.get('type') == 'text').strip()
    if current == canonical:
        return cleaned
    return _project_visible_text(source, canonical)


def finalize_display_segments_json(raw_json: str | None, visible_text: str | None = None) -> str:
    """Finalize an accumulator payload immediately before durable persistence."""
    if not raw_json:
        return ''
    try:
        parsed = json.loads(raw_json)
    except (TypeError, ValueError):
        return ''
    if not isinstance(parsed, list):
        return ''
    final = finalize_display_segments(parsed, visible_text=visible_text)
    return json.dumps(final, ensure_ascii=False, separators=(',', ':')) if final else ''
