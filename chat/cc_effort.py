"""Claude Code chat effort authority.

Config key: CC_CHAT_EFFORT
  ""        -> default -> no --effort argv
  "<level>" -> explicit -> ["--effort", "<level>"]

The empty value intentionally means that Claude Code chooses its own default;
it is never represented as a CLI value such as ``default`` or ``medium``.
"""

from __future__ import annotations

from typing import Any

import config_store

CC_CHAT_EFFORT_KEY = 'CC_CHAT_EFFORT'
CC_EFFORT_ALLOWED = ('low', 'medium', 'high', 'xhigh', 'max')
CC_EFFORT_NOT_ALLOWED = 'CC_EFFORT_NOT_ALLOWED'


def _normalize_effort(value: Any) -> str:
    return str(value or '').strip().lower()


def get_cc_chat_effort() -> str:
    """Return normalized configured effort, or ``''`` for Claude default."""
    value = _normalize_effort(config_store.get(CC_CHAT_EFFORT_KEY, ''))
    return value if value in CC_EFFORT_ALLOWED else ''


def cc_effort_snapshot(effort: str | None = None) -> tuple[str, str, list[str]]:
    """Return one config snapshot for argv construction and identity binding."""
    value = get_cc_chat_effort() if effort is None else _normalize_effort(effort)
    if not value:
        return '', 'default', []
    if value not in CC_EFFORT_ALLOWED:
        raise ValueError(CC_EFFORT_NOT_ALLOWED)
    return value, 'explicit:%s' % value, ['--effort', value]


def cc_effort_identity(effort: str | None = None) -> str:
    """Stable identity used by resident lazy-reuse checks."""
    _value, identity, _args = cc_effort_snapshot(effort)
    return identity


def describe_cc_effort_state() -> dict[str, Any]:
    effort = get_cc_chat_effort()
    return {
        'configured_effort': effort or None,
        'effort_mode': 'explicit' if effort else 'default',
        'allowed_efforts': list(CC_EFFORT_ALLOWED),
    }


def set_cc_chat_effort(value: str | None) -> dict[str, Any]:
    """Persist effort or clear it to default, failing closed on bad values."""
    normalized = _normalize_effort(value)
    if normalized and normalized not in CC_EFFORT_ALLOWED:
        state = describe_cc_effort_state()
        state.update({
            'ok': False,
            'error': CC_EFFORT_NOT_ALLOWED,
            'rejected_effort': normalized,
            'scope': 'cc_chat_effort',
        })
        return state
    config_store.set(CC_CHAT_EFFORT_KEY, normalized)
    state = describe_cc_effort_state()
    state.update({
        'ok': True,
        'effective_from': 'next_turn',
        'scope': 'cc_chat_effort',
    })
    return state

