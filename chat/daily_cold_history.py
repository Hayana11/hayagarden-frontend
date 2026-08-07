"""Bounded newest-suffix selection for Daily Soft Window cold/respawn.

Recovery cold only: trims what a new resident *sees*, never mutates DB history
or membership. Reuses #218 token estimation; does not invent a second budget.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from chat.cold_bootstrap_budget import estimate_text_tokens
from chat.context_lean import cc_history_token_budget


def group_formal_history_rounds(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group assembled formal history into complete conversation rounds.

    Round = one formal user + following assistants until the next user.
    Leading orphan assistants are dropped. Trailing user-only rounds are kept
    (same semantic as ``group_carryover_rounds``, but on assembled dicts with
    full ``content`` — not truncated previews).
    """
    rounds: list[list[dict[str, Any]]] = []
    current: Optional[list[dict[str, Any]]] = None
    for msg in messages:
        role = str(msg.get('role') or '')
        if role == 'user':
            if current is not None:
                rounds.append(current)
            current = [msg]
        elif role == 'assistant' and current is not None:
            current.append(msg)
    if current is not None:
        rounds.append(current)
    return rounds


def _format_history_for_estimate(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get('role') or 'user'
        label = '用户' if role == 'user' else '费佳'
        lines.append('[%s] %s' % (label, msg.get('content') or ''))
    return '\n'.join(lines)


def estimate_history_tokens(
    messages: list[dict[str, Any]],
    *,
    estimate_fn: Optional[Callable[[str], int]] = None,
) -> int:
    fn = estimate_fn or estimate_text_tokens
    return int(fn(_format_history_for_estimate(messages)))


def select_newest_complete_rounds_under_budget(
    messages: list[dict[str, Any]],
    *,
    history_token_budget: Optional[int] = None,
    estimate_fn: Optional[Callable[[str], int]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep newest complete rounds whose rendered estimate fits the budget.

    Short windows (full history <= budget) are returned unchanged.
    When the single newest round alone exceeds budget, that whole round is
    kept (never mid-round sliced) and overflow is reported for the
    whole-prompt fence to fail closed if needed.
    """
    fn = estimate_fn or estimate_text_tokens
    budget = (
        cc_history_token_budget()
        if history_token_budget is None
        else max(1, int(history_token_budget))
    )
    before_count = len(messages)
    rounds_before = group_formal_history_rounds(messages)
    rounds_before_n = len(rounds_before)
    full_tokens = estimate_history_tokens(messages, estimate_fn=fn)

    stats: dict[str, Any] = {
        'cold_history_budget': budget,
        'cold_history_messages_before': before_count,
        'cold_history_rounds_before': rounds_before_n,
        'cold_history_tokens_before': full_tokens,
        'cold_history_trimmed': False,
        'cold_budget_mode': 'token_budget',
        'cold_history_messages_after': before_count,
        'cold_history_rounds_after': rounds_before_n,
        'cold_history_tokens_after': full_tokens,
        'cold_history_oldest_retained_message_id': (
            int(messages[0]['message_id']) if messages else 0
        ),
        'cold_history_round_overflow': False,
    }

    if before_count == 0 or full_tokens <= budget:
        return list(messages), stats

    selected_rounds: list[list[dict[str, Any]]] = []
    selected_tokens = 0
    for round_msgs in reversed(rounds_before):
        round_tokens = estimate_history_tokens(round_msgs, estimate_fn=fn)
        if not selected_rounds and round_tokens > budget:
            selected_rounds = [list(round_msgs)]
            selected_tokens = round_tokens
            stats['cold_history_round_overflow'] = True
            break
        if selected_rounds and selected_tokens + round_tokens > budget:
            break
        selected_rounds.insert(0, list(round_msgs))
        selected_tokens += round_tokens

    retained: list[dict[str, Any]] = []
    for rnd in selected_rounds:
        retained.extend(rnd)

    stats['cold_history_trimmed'] = True
    stats['cold_history_messages_after'] = len(retained)
    stats['cold_history_rounds_after'] = len(selected_rounds)
    stats['cold_history_tokens_after'] = selected_tokens
    stats['cold_history_oldest_retained_message_id'] = (
        int(retained[0]['message_id']) if retained else 0
    )
    return retained, stats
