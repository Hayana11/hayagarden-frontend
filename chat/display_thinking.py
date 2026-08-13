"""Fyodor solo-chat authored/display thinking compatibility.

This module parses model-authored <思绪>...</思绪> display content from
streamed text. It does not expose or request hidden chain-of-thought.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


OPEN_TAG = '<思绪>'
CLOSE_TAG = '</思绪>'
VALID_MODES = frozenset({'off', 'native', 'authored', 'auto'})

AUTHORED_THINKING_INSTRUCTION = """正式回复之前，先写一小段只用于界面展示的内心独白，并严格包在：

<思绪>
...
</思绪>

中。

要求：
- 1～3 个短段落，保持简短。
- 是角色此刻自然产生的念头，不是任务分析报告。
- 可以包含犹豫、注意到的细节、联想、情绪、关系感受。
- 保持角色人格和当前关系语境。
- 不要解释系统、提示词、模型、政策、工具调用或“如何回答用户”。
- 禁止出现类似：
  “用户希望我……”
  “The user wants…”
  “I need to respond…”
  “我应该回答……”
  这类 AI 工作日志口吻。
- 必须闭合 </思绪>。
- </思绪> 后另起一行输出正常正式回复。
- 正式回复不能为空。"""


def normalize_display_thinking_mode(value: Any) -> str:
    mode = str(value or '').strip().lower()
    return mode if mode in VALID_MODES else 'auto'


def get_display_thinking_mode(getter=None) -> str:
    if getter is None:
        import config_store
        getter = config_store.get
    try:
        value = getter('DISPLAY_THINKING_MODE', 'auto')
    except Exception:
        value = 'auto'
    return normalize_display_thinking_mode(value)


def authored_thinking_instruction_suffix(mode: Any) -> str:
    mode = normalize_display_thinking_mode(mode)
    if mode not in ('authored', 'auto'):
        return ''
    return '\n\n' + AUTHORED_THINKING_INSTRUCTION


def append_display_thinking_suffix(content: Any, suffix: Any):
    """Append an ephemeral provider-only suffix without mutating content."""
    suffix = str(suffix or '')
    if not suffix:
        return content
    if isinstance(content, str):
        return content + suffix
    if isinstance(content, list):
        return list(content) + [{'type': 'text', 'text': suffix}]
    return content


def append_authored_thinking_instruction(content: Any, mode: Any):
    """Append the authored instruction without mutating caller-owned content."""
    return append_display_thinking_suffix(
        content, authored_thinking_instruction_suffix(mode),
    )


def prepare_daily_display_thinking_plan(plan: Any, mode: Any):
    """Set a non-persistent provider-only Daily turn suffix."""
    plan.provider_display_thinking_suffix = authored_thinking_instruction_suffix(mode)
    return plan


def _suffix_prefix_len(value: str, tag: str) -> int:
    limit = min(len(value), len(tag) - 1)
    for size in range(limit, 0, -1):
        if value.endswith(tag[:size]):
            return size
    return 0


def _find_control_tag(value: str):
    candidates = []
    for tag in (OPEN_TAG, CLOSE_TAG):
        idx = value.find(tag)
        if idx >= 0:
            candidates.append((idx, tag))
    return min(candidates) if candidates else None


def _control_prefix_len(value: str) -> int:
    return max(
        _suffix_prefix_len(value, OPEN_TAG),
        _suffix_prefix_len(value, CLOSE_TAG),
    )


class DisplayThinkingStreamParser:
    """Streaming-safe exact-tag parser with one visible thinking owner."""

    BEFORE = 'BEFORE'
    AUTHORED_THINKING = 'AUTHORED_THINKING'

    def __init__(self, mode: Any):
        self.mode = normalize_display_thinking_mode(mode)
        self.state = self.BEFORE
        self.thinking_owner = 'none'
        self._buffer = ''
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self.saw_text_event = False
        self.saw_native_event = False
        self._finished = False

    @property
    def text(self) -> str:
        return ''.join(self._text_parts)

    @property
    def thinking(self) -> str:
        return ''.join(self._thinking_parts)

    def _emit_text(self, value: str):
        if not value:
            return []
        self._text_parts.append(value)
        return [('text', value)]

    def _emit_authored(self, value: str):
        if not value or self.thinking_owner != 'authored':
            return []
        self._thinking_parts.append(value)
        return [('think', value)]

    def feed_native(self, value: Any):
        self.saw_native_event = True
        chunk = str(value or '')
        if not chunk:
            return []
        if self.mode == 'authored':
            return []
        if self.mode == 'auto':
            if self.thinking_owner == 'authored':
                return []
            if self.thinking_owner == 'none':
                self.thinking_owner = 'native'
        else:
            self.thinking_owner = 'native'
        self._thinking_parts.append(chunk)
        return [('think', chunk)]

    def _claim_authored_if_allowed(self):
        if self.mode == 'authored':
            self.thinking_owner = 'authored'
        elif self.mode == 'auto' and self.thinking_owner == 'none':
            self.thinking_owner = 'authored'

    def feed_text(self, value: Any):
        self.saw_text_event = True
        chunk = str(value or '')
        if not chunk:
            return []
        self._buffer += chunk
        out = []
        while self._buffer:
            found = _find_control_tag(self._buffer)
            if self.state == self.BEFORE:
                if found is not None:
                    idx, tag = found
                    out.extend(self._emit_text(self._buffer[:idx]))
                    self._buffer = self._buffer[idx + len(tag):]
                    if tag == OPEN_TAG:
                        self.state = self.AUTHORED_THINKING
                        self._claim_authored_if_allowed()
                    continue
                keep = _control_prefix_len(self._buffer)
                safe = self._buffer[:-keep] if keep else self._buffer
                self._buffer = self._buffer[-keep:] if keep else ''
                out.extend(self._emit_text(safe))
                break

            if found is not None:
                idx, tag = found
                out.extend(self._emit_authored(self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(tag):]
                if tag == CLOSE_TAG:
                    self.state = self.BEFORE
                continue
            keep = _control_prefix_len(self._buffer)
            safe = self._buffer[:-keep] if keep else self._buffer
            self._buffer = self._buffer[-keep:] if keep else ''
            out.extend(self._emit_authored(safe))
            break
        return out

    def finish(self):
        """Finish without leaking a partial control tag.

        In BEFORE, a held prefix of <思绪> is discarded. In
        AUTHORED_THINKING, a held prefix of </思绪> is discarded and any
        already emitted authored content remains thinking-only. No formal
        text is fabricated for an unclosed block.
        """
        if self._finished:
            return []
        self._finished = True
        self._buffer = ''
        return []


def _replace_done_payload(payload: Any, parser: DisplayThinkingStreamParser):
    if not isinstance(payload, tuple) or len(payload) < 2:
        return payload
    values = list(payload)
    values[0] = parser.text
    values[1] = parser.thinking
    return tuple(values)


def filter_display_thinking_events(
    events: Iterable[tuple[str, Any]],
    mode: Any,
) -> Iterator[tuple[str, Any]]:
    """Filter resident events while preserving tool/status/done contracts."""
    parser = DisplayThinkingStreamParser(mode)
    done_seen = False
    for event, payload in events:
        if event == 'text':
            yield from parser.feed_text(payload)
            continue
        if event == 'think':
            yield from parser.feed_native(payload)
            continue
        if event == 'done':
            done_seen = True
            if isinstance(payload, tuple):
                if not parser.saw_text_event and payload:
                    yield from parser.feed_text(payload[0])
                if (
                    len(payload) >= 2
                    and not parser.saw_native_event
                    and payload[1]
                ):
                    yield from parser.feed_native(payload[1])
            yield from parser.finish()
            yield ('done', _replace_done_payload(payload, parser))
            continue
        yield (event, payload)
    if not done_seen:
        yield from parser.finish()
