"""Claude Code chat model authority (MODEL-1B).

Config key: CC_CHAT_MODEL
  ""        → default → no --model argv
  "<id>"    → explicit → ["--model", "<id>"]

Never reads MODEL / ACTIVE_RELAY / relay catalog.
"""

from __future__ import annotations

from typing import Any

import config_store

CC_CHAT_MODEL_KEY = 'CC_CHAT_MODEL'
CC_MODEL_NOT_ALLOWED = 'CC_MODEL_NOT_ALLOWED'

# Official Claude Code model IDs only. Not derived from relay models.json.
CC_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        'id': 'claude-sonnet-5',
        'label': 'Sonnet 5',
        'desc': '主力均衡',
        'primary': True,
        'dot': '#6a8a7c',
    },
    {
        'id': 'claude-opus-5',
        'label': 'Opus 5',
        'desc': '新一代旗舰',
        'primary': True,
        'dot': '#8a5a72',
    },
    {
        'id': 'claude-opus-4-8',
        'label': 'Opus 4.8',
        'desc': '上一代旗舰',
        'primary': True,
        'dot': '#8a6a7c',
    },
    {
        'id': 'claude-opus-4-6',
        'label': 'Opus 4.6',
        'desc': '深度推理',
        'primary': True,
        'dot': '#7c6a8a',
    },
    {
        'id': 'claude-sonnet-4-6',
        'label': 'Sonnet 4.6',
        'desc': '上一代均衡',
        'primary': False,
        'dot': '#8a7c6a',
    },
    {
        'id': 'claude-haiku-4-5-20251001',
        'label': 'Haiku 4.5',
        'desc': '更快更轻',
        'primary': False,
        'dot': '#6a7c8a',
    },
]


def get_cc_chat_model() -> str:
    """Return stripped CC_CHAT_MODEL, or '' for default."""
    return str(config_store.get(CC_CHAT_MODEL_KEY, '') or '').strip()


def cc_catalog_ids() -> frozenset[str]:
    return frozenset(
        str(row.get('id') or '').strip()
        for row in CC_MODEL_CATALOG
        if str(row.get('id') or '').strip()
    )


def is_allowed_cc_model(model_id: str) -> bool:
    """True only for CC_MODEL_CATALOG ids. Relay aliases never pass."""
    return str(model_id or '').strip() in cc_catalog_ids()


def cc_model_snapshot(model: str | None = None) -> tuple[str, str, list[str]]:
    """One read of CC_CHAT_MODEL → (raw_model, identity, argv_fragment).

    Spawn paths must use this so argv and stored identity cannot diverge
    across two separate DB reads.
    """
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    if not value:
        return '', 'default', []
    return value, 'explicit:%s' % value, ['--model', value]


def cc_model_mode(model: str | None = None) -> str:
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    return 'explicit' if value else 'default'


def cc_model_identity(model: str | None = None) -> str:
    """Stable identity for resident reuse checks."""
    _value, identity, _args = cc_model_snapshot(model)
    return identity


def cc_model_args(model: str | None = None) -> list[str]:
    """CLI argv fragment: [] or ['--model', '<id>']."""
    _value, _identity, args = cc_model_snapshot(model)
    return args


def cc_model_args_from_identity(identity: str) -> list[str]:
    """Convert a frozen CC authority identity to argv without reading config."""
    identity = str(identity or '').strip()
    if identity == 'default':
        return []
    prefix = 'explicit:'
    if not identity.startswith(prefix):
        raise ValueError('invalid CC model identity: %s' % (identity or '<empty>'))
    model = identity[len(prefix):].strip()
    if not model or not is_allowed_cc_model(model):
        raise ValueError('invalid CC model identity: %s' % identity)
    return ['--model', model]


def describe_cc_model_state() -> dict[str, Any]:
    model = get_cc_chat_model()
    mode = cc_model_mode(model)
    configured = model or None
    return {
        'provider': 'claude_code',
        'model_mode': mode,
        'configured_model': configured,
        'model': configured,
    }


def set_cc_chat_model(model: str | None) -> dict[str, Any]:
    """Write CC_CHAT_MODEL. None/'' clears to default.

    Non-empty values must be CC_MODEL_CATALOG ids. Relay aliases and other
    free-form strings are rejected without mutating CC_CHAT_MODEL.
    """
    if model is None:
        value = ''
    else:
        value = str(model).strip()
    if value and not is_allowed_cc_model(value):
        state = describe_cc_model_state()
        state['ok'] = False
        state['error'] = CC_MODEL_NOT_ALLOWED
        state['rejected_model'] = value
        state['scope'] = 'cc_chat_model'
        return state
    config_store.set(CC_CHAT_MODEL_KEY, value)
    state = describe_cc_model_state()
    state['ok'] = True
    state['effective_from'] = 'next_turn'
    state['scope'] = 'cc_chat_model'
    return state


def cc_catalog_label(model_id: str) -> str:
    mid = str(model_id or '').strip()
    for row in CC_MODEL_CATALOG:
        if row.get('id') == mid:
            return str(row.get('label') or mid)
    return mid
