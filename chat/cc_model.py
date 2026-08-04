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
        'id': 'claude-opus-4-8',
        'label': 'Opus 4.8',
        'desc': '新一代旗舰',
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


def cc_model_mode(model: str | None = None) -> str:
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    return 'explicit' if value else 'default'


def cc_model_identity(model: str | None = None) -> str:
    """Stable identity for resident reuse checks."""
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    if not value:
        return 'default'
    return 'explicit:%s' % value


def cc_model_args(model: str | None = None) -> list[str]:
    """CLI argv fragment: [] or ['--model', '<id>']."""
    value = get_cc_chat_model() if model is None else str(model or '').strip()
    if not value:
        return []
    return ['--model', value]


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
    """Write CC_CHAT_MODEL. None/'' clears to default."""
    if model is None:
        value = ''
    else:
        value = str(model).strip()
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
