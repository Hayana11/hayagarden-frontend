"""Claude Code credentials read from the existing provider authorities."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

_ENV_PATH = Path('/opt/frontend/.env')
_GATEWAY_ENV = (
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_FABLE_MODEL',
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC',
    'CLAUDE_CODE_ATTRIBUTION_HEADER',
)


class CCProviderEnvError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def read_cc_oauth_token(env_path: str | Path | None = None) -> str:
    """Read the existing deployment token without logging or retaining it."""
    path = Path(env_path) if env_path is not None else _ENV_PATH
    try:
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.startswith('CLAUDE_CODE_OAUTH_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ''


def build_cc_provider_env(
    provider: str,
    model_identity: str,
    *,
    token_getter: Callable[[], str] | None = None,
    base_env: dict[str, str] | None = None,
    relay_factory: Callable[[], object] | None = None,
) -> dict[str, str]:
    """Select authentication for the existing Claude Code process boundary."""
    env = dict(base_env if base_env is not None else os.environ)
    for name in _GATEWAY_ENV:
        env.pop(name, None)

    provider = str(provider or '').strip().lower()
    model_identity = str(model_identity or '').strip()
    if provider == 'claude_code':
        getter = token_getter or read_cc_oauth_token
        try:
            token = str(getter() or '').strip()
        except Exception as exc:
            raise CCProviderEnvError('cc_oauth_token_unavailable') from exc
        if not token:
            raise CCProviderEnvError('cc_oauth_token_unavailable')
        env['CLAUDE_CODE_OAUTH_TOKEN'] = token
        env['HAYAGARDEN_CC_PROVIDER'] = provider
        fingerprint = hashlib.sha256(
            ('claude_code|' + model_identity + '|' + token).encode('utf-8')
        ).hexdigest()
        env['HAYAGARDEN_CC_PROVIDER_IDENTITY'] = fingerprint
        return env

    if provider != 'api_relay':
        raise CCProviderEnvError('cc_provider_unavailable')

    model = model_identity
    if model.startswith('explicit:'):
        model = model[len('explicit:'):].strip()
    if not model or model.lower() == 'unknown':
        raise CCProviderEnvError('api_relay_model_unavailable')

    factory = relay_factory
    if factory is None:
        from relay.manager import RelayManager
        factory = RelayManager
    try:
        relay = factory()
        api_url = str(getattr(relay, 'api_url', '') or '').strip().rstrip('/')
        key = str(getattr(relay, 'api_key', '') or '').strip()
    except Exception as exc:
        raise CCProviderEnvError('api_relay_gateway_unavailable') from exc
    if not api_url or not key:
        raise CCProviderEnvError('api_relay_gateway_unavailable')

    suffix = '/v1/messages'
    base_url = api_url[:-len(suffix)] if api_url.endswith(suffix) else api_url
    base_url = base_url.rstrip('/')
    if not base_url:
        raise CCProviderEnvError('api_relay_gateway_unavailable')

    env.update({
        'ANTHROPIC_BASE_URL': base_url,
        'ANTHROPIC_AUTH_TOKEN': key,
        'ANTHROPIC_MODEL': model,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
        'HAYAGARDEN_CC_PROVIDER': provider,
    })
    env['HAYAGARDEN_CC_PROVIDER_IDENTITY'] = hashlib.sha256(
        ('api_relay|' + model + '|' + base_url + '|' + key).encode('utf-8')
    ).hexdigest()
    return env
