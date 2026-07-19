"""Provider configuration semantics shared by chat/background/wake callers."""

from __future__ import annotations

from typing import Literal

import config_store


ProviderScope = Literal['chat', 'wake', 'background']
_VALID_PROVIDERS = frozenset(('claude_code', 'api_relay'))
_VALID_FALLBACKS = frozenset(('none', 'deepseek', 'claude_code', 'api_relay'))


class ProviderConfigError(ValueError):
    pass


def _validated(value: str, *, key: str) -> str:
    value = str(value or '').strip().lower()
    if value not in _VALID_PROVIDERS:
        raise ProviderConfigError('%s 配置无效: %s' % (key, value or '<empty>'))
    return value


def resolve_provider(scope: ProviderScope) -> str:
    if scope == 'chat':
        value = config_store.get('CHAT_PROVIDER', '')
        if not str(value or '').strip():
            value = config_store.get('GW_PROVIDER', 'api_relay')
        return _validated(value, key='CHAT_PROVIDER')
    if scope == 'wake':
        value = str(config_store.get('WAKE_PROVIDER', 'inherit') or '').strip().lower()
        if not value or value == 'inherit':
            return resolve_provider('chat')
        return _validated(value, key='WAKE_PROVIDER')
    if scope == 'background':
        return _validated(
            config_store.get('BACKGROUND_PROVIDER', 'api_relay'),
            key='BACKGROUND_PROVIDER',
        )
    raise ProviderConfigError('未知 provider scope: %s' % scope)


def resolve_fallback_provider() -> str:
    value = str(config_store.get('FALLBACK_PROVIDER', 'none') or 'none').strip().lower()
    if value not in _VALID_FALLBACKS:
        raise ProviderConfigError('FALLBACK_PROVIDER 配置无效: %s' % value)
    return value


def fallback_for_http_status(status: int) -> str:
    fallback = resolve_fallback_provider()
    if fallback == 'deepseek' and int(status) in (401, 403, 503):
        return fallback
    return 'none'
