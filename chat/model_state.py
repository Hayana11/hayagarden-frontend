"""Provider-aware chat model state (MODEL-1A / MODEL-1B).

CC and api_relay keep independent model spaces.
"""

from __future__ import annotations

from typing import Any

ACTIVE_RELAY_NOT_FOUND = 'ACTIVE_RELAY_NOT_FOUND'
ACTIVE_RELAY_DELETE_NOT_ALLOWED = 'ACTIVE_RELAY_DELETE_NOT_ALLOWED'


def describe_chat_model_state(
    provider: str,
    *,
    relay_id: str | None = None,
    relay_name: str | None = None,
    relay_model: str | None = None,
) -> dict[str, Any]:
    """Return the model payload for the current chat provider.

    Never mixes relay / global MODEL into the Claude Code branch.
    """
    provider = str(provider or '').strip().lower()
    if provider == 'claude_code':
        from chat.cc_model import describe_cc_model_state
        return describe_cc_model_state()

    configured = (relay_model or '').strip() or None
    payload: dict[str, Any] = {
        'provider': 'api_relay',
        'configured_model': configured,
        'model': configured or '',
    }
    relay = (relay_id or '').strip()
    if relay:
        payload['relay'] = relay
    name = (relay_name or '').strip()
    if name:
        payload['relay_name'] = name
    return payload
