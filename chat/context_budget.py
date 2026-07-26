"""Token budgeting and BP3 state send-payload helpers.

Raw state snapshots are always complete; send payloads omit unchanged volatile
fields unless user-relevant. Omission never means cleared.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Optional, Sequence

from chat.system_builder import format_state_diff, format_state_snapshot

EstimateFn = None  # re-exported via default_estimate_tokens

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


def normalize_state_dict(state: Optional[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (state or {}).items():
        out[str(key)] = str(value or '').strip()
    return out


def state_key_relevant(key: str, user_text: str) -> bool:
    pattern = _STATE_RELEVANCE.get(key)
    if not pattern:
        return True
    return bool(pattern.search(user_text or ''))


def build_state_send_payload(
    last_raw_state: Optional[Mapping[str, Any]],
    raw_state: Mapping[str, Any],
    *,
    user_text: str = '',
    is_cold: bool = False,
) -> dict[str, str]:
    """Build per-turn send dict. Omitted keys are not sent; empty string = tombstone."""
    last_raw = normalize_state_dict(last_raw_state)
    raw = normalize_state_dict(raw_state)
    if is_cold:
        return {k: v for k, v in raw.items() if v}

    send: dict[str, str] = {}
    keys = list(dict.fromkeys(list(last_raw.keys()) + list(raw.keys())))
    for key in keys:
        before = last_raw.get(key, '')
        after = raw.get(key, '')
        if after:
            if before != after:
                send[key] = after
            elif key in _VOLATILE_STATE_KEYS and state_key_relevant(key, user_text):
                send[key] = after
        elif before:
            send[key] = ''

    tb = raw.get('time_bucket', '')
    if tb:
        changed_other = any(
            send.get(k, last_raw.get(k, '')) != last_raw.get(k, '')
            for k in send
            if k != 'time_bucket'
        )
        if tb != last_raw.get('time_bucket', '') or changed_other:
            send['time_bucket'] = tb
    return send


def format_state_for_send(
    last_send_state: Optional[Mapping[str, Any]],
    send_state: Mapping[str, Any],
    *,
    is_cold: bool = False,
) -> tuple[str, str]:
    """Return (text, mode). Omitted keys in send_state are not treated as cleared."""
    send_state = normalize_state_dict(send_state)
    if is_cold:
        text = format_state_snapshot(send_state)
        return text, ('snapshot' if text else 'none')

    last_send = normalize_state_dict(last_send_state)
    lines = []
    labels = {
        'time_bucket': '当前时间段',
        'emotion': '情绪',
        'drive': '驱动',
        'lights': '灯',
        'pocket': 'Pocket',
        'todos': '留言板待办',
        'ledger': '记账',
        'reminders': '今日提醒',
        'recent_activity': '最近活动',
    }
    for key, after in send_state.items():
        before = last_send.get(key, '')
        label = labels.get(key, key)
        if before and not after:
            lines.append(f'- {label}：已清空')
        elif not before and after:
            lines.append(f'- {label}：{after}')
        elif before != after:
            if key in ('time_bucket', 'lights', 'pocket') and len(before) < 80 and len(after) < 80:
                lines.append(f'- {label}：{before} → {after}')
            else:
                lines.append(f'- {label}：{after}')
        elif after:
            lines.append(f'- {label}：{after}')
    if not lines:
        return '', 'none'
    return '【状态更新】\n' + '\n'.join(lines), 'delta'


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
        cost = int(piece) if isinstance(piece, (int, float)) else estimate_tokens(piece)
        if kept and total + cost > budget:
            break
        kept.append(row)
        total += cost
    kept.reverse()
    return kept, total


# Backward-compatible alias used in early PR #138 drafts.
filter_state_dict_for_turn = build_state_send_payload
