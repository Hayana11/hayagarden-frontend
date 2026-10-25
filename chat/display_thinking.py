"""Fyodor solo-chat authored self-state/display compatibility.

This module parses model-authored self-state delimiters from streamed text.
It does not expose or request hidden chain-of-thought.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any


SELF_STATE_OPEN_TAG = '[[自我状态]]'
SELF_STATE_CLOSE_TAG = '[[/自我状态]]'
LEGACY_OPEN_TAG = '<思绪>'
LEGACY_CLOSE_TAG = '</思绪>'

# OPEN_TAG/CLOSE_TAG remain the write-protocol aliases for callers/tests.
OPEN_TAG = SELF_STATE_OPEN_TAG
CLOSE_TAG = SELF_STATE_CLOSE_TAG
VALID_MODES = frozenset({'off', 'native', 'authored', 'auto'})
DISPLAY_THINKING_PROMPT_KEY = 'DISPLAY_THINKING_PROMPT'
MAX_DISPLAY_THINKING_PROMPT_CHARS = 12000

AUTHORED_SELF_STATE_INSTRUCTION = """正式回复之前，先写一小段只用于界面展示的内心独白，并严格包在：

[[自我状态]]
...
[[/自我状态]]

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
- 必须闭合 [[/自我状态]]。
- [[/自我状态]] 后另起一行输出正常正式回复。
- 正式回复不能为空。"""

# Legacy import alias; authored output is now self-state, not native thinking.
AUTHORED_THINKING_INSTRUCTION = AUTHORED_SELF_STATE_INSTRUCTION


def normalize_display_thinking_mode(value: Any) -> str:
    mode = str(value or '').strip().lower()
    return mode if mode in VALID_MODES else 'auto'


def resolve_effective_display_thinking_mode(
    configured_mode: Any, model_identity: Any = None,
) -> str:
    """Return the configured display mode; model identity never owns self-state."""
    # Keep model_identity as a compatibility parameter for existing callers.
    return normalize_display_thinking_mode(configured_mode)


def configured_mode_from_authored_prompt_enabled(enabled: Any) -> str:
    """Map the UI authored-prompt switch onto DISPLAY_THINKING_MODE."""
    if enabled is True:
        return 'auto'
    if enabled is False:
        return 'native'
    raise ValueError('authored_prompt_enabled must be boolean')


def display_thinking_config_state(
    configured_mode: Any, model_identity: Any,
) -> dict[str, Any]:
    """Read-model for GET/POST /api/config/display-thinking."""
    configured = normalize_display_thinking_mode(configured_mode)
    identity = str(model_identity or '').strip()
    effective = resolve_effective_display_thinking_mode(configured, identity)
    return {
        'configured_mode': configured,
        'effective_mode': effective,
        'model_identity': identity,
        'authored_prompt_effective': bool(
            authored_self_state_instruction_suffix(effective),
        ),
    }


def get_display_thinking_mode(getter=None) -> str:
    if getter is None:
        import config_store
        getter = config_store.get
    try:
        value = getter('DISPLAY_THINKING_MODE', 'auto')
    except Exception:
        value = 'auto'
    return normalize_display_thinking_mode(value)


def validate_display_thinking_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError('可见自我状态 prompt 必须是文本')
    prompt = value.strip()
    if not prompt:
        raise ValueError('可见自我状态 prompt 不能为空')
    if len(prompt) > MAX_DISPLAY_THINKING_PROMPT_CHARS:
        raise ValueError(
            '可见自我状态 prompt 不能超过 %d 个字符'
            % MAX_DISPLAY_THINKING_PROMPT_CHARS
        )
    has_new = SELF_STATE_OPEN_TAG in prompt and SELF_STATE_CLOSE_TAG in prompt
    has_legacy = LEGACY_OPEN_TAG in prompt and LEGACY_CLOSE_TAG in prompt
    if not has_new and not has_legacy:
        raise ValueError(
            '可见自我状态 prompt 必须同时包含 [[自我状态]]/[[/自我状态]] '
            '或旧 <思绪>/</思绪>'
        )
    open_tag, close_tag = (
        (SELF_STATE_OPEN_TAG, SELF_STATE_CLOSE_TAG)
        if has_new else (LEGACY_OPEN_TAG, LEGACY_CLOSE_TAG)
    )
    if prompt.index(close_tag) < prompt.index(open_tag):
        raise ValueError('可见自我状态 prompt 的结束标签必须位于开始标签之后')
    # New writes use the self-state protocol while preserving all other text.
    return prompt.replace(LEGACY_OPEN_TAG, SELF_STATE_OPEN_TAG).replace(
        LEGACY_CLOSE_TAG, SELF_STATE_CLOSE_TAG,
    )


def get_display_thinking_prompt(getter=None) -> str:
    if getter is None:
        import config_store
        getter = config_store.get
    try:
        value = getter(DISPLAY_THINKING_PROMPT_KEY, '')
        if isinstance(value, str) and value.strip():
            return validate_display_thinking_prompt(value)
    except Exception:
        pass
    return AUTHORED_SELF_STATE_INSTRUCTION


def get_display_thinking_snapshot(getter=None) -> tuple[str, str]:
    """Read mode and effective authored prompt once for one provider turn."""
    if getter is None:
        import config_store
        getter = config_store.get
    return (
        get_display_thinking_mode(getter),
        get_display_thinking_prompt(getter),
    )


def authored_self_state_instruction_suffix(
    mode: Any,
    prompt: Any = None,
) -> str:
    mode = normalize_display_thinking_mode(mode)
    if mode not in ('authored', 'auto'):
        return ''
    instruction = (
        AUTHORED_SELF_STATE_INSTRUCTION
        if prompt is None
        else validate_display_thinking_prompt(prompt)
    )
    return '\n\n' + instruction


# Compatibility alias for existing config/plan callers. The authored channel is
# self-state; native Claude thinking remains a separate stream event.
authored_thinking_instruction_suffix = authored_self_state_instruction_suffix


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


def append_authored_self_state_instruction(
    content: Any,
    mode: Any,
    prompt: Any = None,
):
    """Append the authored self-state instruction without mutating caller-owned content."""
    return append_display_thinking_suffix(
        content, authored_self_state_instruction_suffix(mode, prompt),
    )


append_authored_thinking_instruction = append_authored_self_state_instruction


def prepare_daily_display_thinking_plan(
    plan: Any,
    mode: Any,
    prompt: Any = None,
):
    """Set a non-persistent provider-only Daily turn suffix."""
    effective_mode = normalize_display_thinking_mode(mode)
    plan.provider_display_thinking_mode = normalize_display_thinking_mode(mode)
    plan.provider_display_thinking_effective_mode = effective_mode
    plan.provider_display_thinking_suffix = authored_self_state_instruction_suffix(
        effective_mode, prompt,
    )
    plan.provider_display_thinking_prompt = (
        AUTHORED_SELF_STATE_INSTRUCTION
        if prompt is None
        else validate_display_thinking_prompt(prompt)
    )
    return plan


def _suffix_prefix_len(value: str, tag: str) -> int:
    limit = min(len(value), len(tag) - 1)
    for size in range(limit, 0, -1):
        if value.endswith(tag[:size]):
            return size
    return 0


def _all_control_tags() -> tuple[str, ...]:
    return (
        SELF_STATE_OPEN_TAG,
        SELF_STATE_CLOSE_TAG,
        LEGACY_OPEN_TAG,
        LEGACY_CLOSE_TAG,
    )


def _find_control_tag(value: str):
    candidates = []
    for tag in _all_control_tags():
        idx = value.find(tag)
        if idx >= 0:
            candidates.append((idx, tag))
    return min(candidates) if candidates else None


def _control_prefix_len(value: str) -> int:
    return max(
        *(_suffix_prefix_len(value, tag) for tag in _all_control_tags()),
    )


class DisplayThinkingStreamParser:
    """Streaming-safe parser with independent native-thinking/self-state channels."""

    BEFORE = 'BEFORE'
    SELF_STATE = 'SELF_STATE'
    # Compatibility state name for callers that only inspect parser constants.
    AUTHORED_THINKING = SELF_STATE

    def __init__(self, mode: Any):
        self.mode = normalize_display_thinking_mode(mode)
        self.state = self.BEFORE
        self._buffer = ''
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._self_state_parts: list[str] = []
        self.saw_text_event = False
        self.saw_native_event = False
        self._finished = False

    @property
    def text(self) -> str:
        return ''.join(self._text_parts)

    @property
    def thinking(self) -> str:
        return ''.join(self._thinking_parts)

    @property
    def self_state(self) -> str:
        return ''.join(self._self_state_parts)

    def _emit_text(self, value: str):
        if not value:
            return []
        self._text_parts.append(value)
        return [('text', value)]

    def _emit_self_state(self, value: str):
        if not value:
            return []
        self._self_state_parts.append(value)
        return [('self_state', value)]

    def feed_native(self, value: Any):
        self.saw_native_event = True
        chunk = str(value or '')
        if not chunk:
            return []
        # Native thinking is independent from the authored self-state channel.
        self._thinking_parts.append(chunk)
        return [('think', chunk)]

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
                    if tag in (SELF_STATE_OPEN_TAG, LEGACY_OPEN_TAG):
                        self.state = self.SELF_STATE
                    continue
                keep = _control_prefix_len(self._buffer)
                safe = self._buffer[:-keep] if keep else self._buffer
                self._buffer = self._buffer[-keep:] if keep else ''
                out.extend(self._emit_text(safe))
                break

            if found is not None:
                idx, tag = found
                out.extend(self._emit_self_state(self._buffer[:idx]))
                self._buffer = self._buffer[idx + len(tag):]
                if tag in (SELF_STATE_CLOSE_TAG, LEGACY_CLOSE_TAG):
                    self.state = self.BEFORE
                continue
            keep = _control_prefix_len(self._buffer)
            safe = self._buffer[:-keep] if keep else self._buffer
            self._buffer = self._buffer[-keep:] if keep else ''
            out.extend(self._emit_self_state(safe))
            break
        return out

    def finish(self):
        """Finish without leaking a partial self-state control tag."""
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
    parser = None
    done_seen = False

    def get_parser():
        nonlocal parser
        if parser is None:
            effective_mode = mode() if callable(mode) else mode
            parser = DisplayThinkingStreamParser(effective_mode)
        return parser

    for event, payload in events:
        if event in ('text', 'think', 'done'):
            current_parser = get_parser()
        else:
            current_parser = parser
        if event == 'text':
            yield from current_parser.feed_text(payload)
            continue
        if event == 'think':
            yield from current_parser.feed_native(payload)
            continue
        if event == 'done':
            done_seen = True
            if isinstance(payload, tuple):
                if not parser.saw_text_event and payload:
                    yield from current_parser.feed_text(payload[0])
                if (
                    len(payload) >= 2
                    and not parser.saw_native_event
                    and payload[1]
                ):
                    yield from current_parser.feed_native(payload[1])
            yield from current_parser.finish()
            yield ('done', _replace_done_payload(payload, current_parser))
            continue
        yield (event, payload)
    if not done_seen and parser is not None:
        yield from parser.finish()
