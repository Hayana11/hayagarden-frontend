"""Compact durable presentation order for interleaved assistant streams."""
from __future__ import annotations

import json
from typing import Any


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
        # Results mutate the parallel tool_calls payload, never presentation order.
        return

    def as_list(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._segments]

    def to_json(self) -> str:
        return json.dumps(self._segments, ensure_ascii=False, separators=(',', ':')) if self._segments else ''
