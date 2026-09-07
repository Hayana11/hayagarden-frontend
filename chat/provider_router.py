"""Provider configuration semantics shared by chat/background/wake callers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

import config_store


ProviderScope = Literal['chat', 'wake', 'background']
_VALID_PROVIDERS = frozenset(('claude_code', 'api_relay'))
_VALID_FALLBACKS = frozenset(('none', 'deepseek', 'claude_code', 'api_relay'))


class ProviderConfigError(ValueError):
    pass


class GenerationClass(str, Enum):
    """Provider-authority contract for future surface migrations.

    Only IDENTITY_BEARING and CONTINUITY_AUTHORING will consume the primary
    generation authority. INFRASTRUCTURE_HELPER intentionally remains allowed
    to use an independent provider. This is a classification contract, not a
    surface registry or an execution-routing change.
    """

    IDENTITY_BEARING = 'identity_bearing'
    CONTINUITY_AUTHORING = 'continuity_authoring'
    INFRASTRUCTURE_HELPER = 'infrastructure_helper'


@dataclass(frozen=True)
class GenerationAuthoritySnapshot:
    """Immutable primary-generation authority captured once at task entry."""

    provider: str
    model_identity: str


def _validated(value: str, *, key: str) -> str:
    value = str(value or '').strip().lower()
    if value not in _VALID_PROVIDERS:
        raise ProviderConfigError('%s 配置无效: %s' % (key, value or '<empty>'))
    return value


def resolve_generation_provider() -> str:
    """Resolve the sole primary provider authority for generation.

    CHAT_PROVIDER is canonical. GW_PROVIDER is read-only compatibility for
    deployments that have not yet written CHAT_PROVIDER. WAKE_PROVIDER,
    BACKGROUND_PROVIDER and FALLBACK_PROVIDER are deliberately excluded:
    they remain legacy surface/fallback routing contracts, not primary
    generation authority.
    """
    value = config_store.get('CHAT_PROVIDER', '')
    if not str(value or '').strip():
        value = config_store.get('GW_PROVIDER', 'api_relay')
    return _validated(value, key='CHAT_PROVIDER')


def capture_generation_authority() -> GenerationAuthoritySnapshot:
    """Capture provider and formal model authority once, without model calls."""
    provider = resolve_generation_provider()
    if provider == 'claude_code':
        # `default` is the CC resolver's explicit default identity; it never
        # guesses a CLI default model name.
        from chat.cc_model import cc_model_identity
        model_identity = cc_model_identity()
    else:
        # RelayManager's own default-model seam: preset.default_model, then
        # the RelayManager legacy MODEL fallback. Never WS_MODEL.
        from relay.manager import resolve_active_relay_model_identity
        model_identity = resolve_active_relay_model_identity()
    return GenerationAuthoritySnapshot(
        provider=provider,
        model_identity=model_identity,
    )


def resolve_provider(scope: ProviderScope) -> str:
    if scope == 'chat':
        return resolve_generation_provider()
    if scope == 'wake':
        # Deprecated legacy surface contract. Do not use as a primary
        # generation source until Wake is explicitly migrated.
        value = str(config_store.get('WAKE_PROVIDER', 'inherit') or '').strip().lower()
        if not value or value == 'inherit':
            return resolve_provider('chat')
        return _validated(value, key='WAKE_PROVIDER')
    if scope == 'background':
        # Deprecated legacy surface contract. Dream/background execution is
        # intentionally unchanged in A1 and is not primary authority.
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
