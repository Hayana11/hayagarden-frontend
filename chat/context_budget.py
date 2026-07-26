"""Token budgeting and BP3 relevance helpers for context reduction.

Keeps persona / static system / tool surface unchanged; only trims volatile slices.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

EstimateFn = Callable[[Optional[str]], int]

_STATE_RELEVANCE = {
    'lights': re.compile(r'灯|光|亮|暗|床头|照明', re.I),
    'pocket': re.compile(r'pocket|手机|浏览器|在线|离线', re.I),
    'ledger': re.compile(r'账|预算|花钱|收入|支出|记账|结余', re.I),
    'todos': re.compile(r'待办|活儿|留言板|board|给活儿', re.I),
    'reminders': re.compile(r'提醒|倒计时|待办事项|todo', re.I),
}

_VOLATILE_STATE_KEYS = frozenset({'lights', 'pocket', 'ledger', 'todos', 'reminders'})
_ALWAYS_SEND_STATE_KEYS = frozenset({'emotion', 'drive', 'recent_activity'})


def default_estimate_tokens(text: Optional[str]) -> int:
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
    return estimate_tokens_heuristic_cjk1_ascii4_v1(text)


def state_key_relevant(key: str, user_text: str) -> bool:
    pattern = _STATE_RELEVANCE.get(key)
    if not pattern:
        return True
    return bool(pattern.search(user_text or ''))


def filter_state_dict_for_turn(
    state: Mapping[str, Any],
    *,
    user_text: str = '',
    last_snapshot: Optional[Mapping[str, Any]] = None,
    is_cold: bool = False,
) -> dict[str, str]:
    """Drop volatile BP3 keys unless changed or user-relevant."""
    state = state or {}
    last_snapshot = last_snapshot or {}
    out: dict[str, str] = {}
    for key, value in state.items():
        text = str(value or '').strip()
        if not text:
            continue
        if key in _ALWAYS_SEND_STATE_KEYS or key == 'time_bucket':
            out[key] = text
            continue
        if key not in _VOLATILE_STATE_KEYS:
            out[key] = text
            continue
        before = str(last_snapshot.get(key) or '').strip()
        if is_cold or before != text:
            out[key] = text
            continue
        if state_key_relevant(key, user_text):
            out[key] = text
    if not is_cold and out.get('time_bucket'):
        changed_non_time = any(
            str(out.get(k) or '').strip() != str(last_snapshot.get(k) or '').strip()
            for k in out
            if k != 'time_bucket'
        )
        relevant_non_time = any(
            k != 'time_bucket' and bool(str(out.get(k) or '').strip())
            for k in out
        ) and any(state_key_relevant(k, user_text) for k in out if k != 'time_bucket')
        if not changed_non_time and not relevant_non_time:
            out.pop('time_bucket', None)
    return out


def trim_rows_to_token_budget(
    rows: Sequence[Any],
    *,
    budget: int,
    text_fn: Callable[[Any], str],
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[Any], int]:
    """Keep newest rows; drop oldest until estimated tokens <= budget."""
    if budget <= 0 or not rows:
        return list(rows), 0
    kept: list[Any] = []
    total = 0
    for row in reversed(rows):
        piece = text_fn(row)
        cost = estimate_tokens(piece)
        if kept and total + cost > budget:
            break
        kept.append(row)
        total += cost
    kept.reverse()
    return kept, total


def trim_tool_history_lines(
    lines: list[str],
    *,
    total_budget: int,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> list[str]:
    """Preserve header; trim oldest tool result blocks to fit total budget."""
    if total_budget <= 0 or len(lines) <= 1:
        return lines
    header = lines[0]
    body = lines[1:]
    if not body:
        return lines
    while body:
        joined = '\n'.join([header] + body)
        if estimate_tokens(joined) <= total_budget:
            return [header] + body
        body.pop(0)
    return [header]


def file_revisit_summary(body: str, *, preview_chars: int = 400) -> str:
    text = (body or '').strip()
    if len(text) <= preview_chars:
        return text
    return text[:preview_chars] + '\n...(此前已全文注入，以上为摘要)'


def collect_messages_file_text(messages: Iterable[Mapping[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages or ():
        content = msg.get('content')
        chunks: list[str] = []
        if isinstance(content, str) and '[用户发来文件:' in content:
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = str(block.get('text') or '')
                    if '[用户发来文件:' in text:
                        chunks.append(text)
        if chunks:
            parts.extend(chunks)
    return '\n\n'.join(parts)


def estimate_messages_file_tokens(messages: Iterable[Mapping[str, Any]], estimate_tokens: EstimateFn = default_estimate_tokens) -> int:
    return estimate_tokens(collect_messages_file_text(messages))
