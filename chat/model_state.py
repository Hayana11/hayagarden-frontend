"""Provider-aware chat model state (MODEL-1A).

CC and api_relay keep independent model spaces. This module only describes
and gates read/write of the chat model UI; it does not drive Claude Code
`--model` (that is MODEL-1B).
"""

from __future__ import annotations

from typing import Any

CC_MODEL_SWITCH_NOT_AVAILABLE = 'CC_MODEL_SWITCH_NOT_AVAILABLE'


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
        return {
            'provider': 'claude_code',
            'model_mode': 'default',
            'configured_model': None,
            # Backward-compatible key: must not surface relay/global MODEL.
            'model': None,
        }

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


def reject_cc_model_switch(provider: str) -> dict[str, str] | None:
    """Fail-closed gate for POST model changes under Claude Code."""
    if str(provider or '').strip().lower() == 'claude_code':
        return {'error': CC_MODEL_SWITCH_NOT_AVAILABLE}
    return None
