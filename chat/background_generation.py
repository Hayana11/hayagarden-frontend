"""Provider-neutral, one-shot background text generation.

This module is an execution adapter only. Callers own context and output
semantics; the frozen GenerationAuthoritySnapshot owns provider and model.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from chat.provider_router import GenerationAuthoritySnapshot


@dataclass(frozen=True)
class BackgroundGenerationRequest:
    system_text: str
    prompt_text: str
    max_tokens: int
    timeout_sec: float
    task_kind: str = ''


@dataclass(frozen=True)
class BackgroundGenerationResult:
    text: str
    provider: str
    model_identity: str
    actual_executor: str
    usage: dict[str, Any] | None = None


class BackgroundGenerationError(RuntimeError):
    """The selected provider failed; this adapter never cross-falls back."""


def _cc_text_from_stream(stdout: str) -> tuple[str, dict[str, Any] | None]:
    text = ''
    deltas: list[str] = []
    usage: dict[str, Any] | None = None
    for line in str(stdout or '').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result' and isinstance(event.get('result'), str):
            text = event['result']
            candidate = event.get('usage')
            usage = dict(candidate) if isinstance(candidate, dict) else usage
        stream_event = event.get('event')
        if event.get('type') == 'stream_event' and isinstance(stream_event, dict):
            delta = stream_event.get('delta')
            if isinstance(delta, dict) and delta.get('type') == 'text_delta':
                chunk = delta.get('text')
                if isinstance(chunk, str):
                    deltas.append(chunk)
    return (text or ''.join(deltas)).strip(), usage


def _generate_claude_code(
    request: BackgroundGenerationRequest,
    authority: GenerationAuthoritySnapshot,
    *,
    token_getter: Callable[[], str] | None,
) -> BackgroundGenerationResult:
    if token_getter is None:
        raise BackgroundGenerationError('cc_background_token_getter_required')
    try:
        token = str(token_getter() or '').strip()
    except Exception as exc:
        raise BackgroundGenerationError('cc_background_token_unavailable') from exc
    if not token:
        raise BackgroundGenerationError('cc_background_token_unavailable')

    from chat.cc_model import cc_model_args_from_identity
    from chat.cc_runtime import ClaudeRuntimeError, claude_cmd, repo_root, require_pinned_claude_version

    try:
        model_args = cc_model_args_from_identity(authority.model_identity)
    except ValueError as exc:
        raise BackgroundGenerationError('cc_background_invalid_model_identity') from exc

    root = repo_root()
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    env.pop('ANTHROPIC_API_KEY', None)
    try:
        require_pinned_claude_version(
            env=env,
            cwd=str(root),
            root=root,
            timeout=min(float(request.timeout_sec), 60.0),
        )
        proc = subprocess.run(
            claude_cmd(
                '-p', request.prompt_text,
                '--output-format', 'stream-json',
                '--verbose',
                '--max-turns', '1',
                '--tools', '',
                '--system-prompt', request.system_text,
                root=root,
            ) + model_args,
            cwd=str(root),
            env=env,
            text=True,
            capture_output=True,
            timeout=float(request.timeout_sec),
        )
    except ClaudeRuntimeError as exc:
        raise BackgroundGenerationError('cc_background_runtime:%s' % exc) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackgroundGenerationError('cc_background_timeout') from exc
    except OSError as exc:
        raise BackgroundGenerationError('cc_background_spawn_failed') from exc
    if proc.returncode != 0:
        raise BackgroundGenerationError(
            'cc_background_exit_%s:%s'
            % (proc.returncode, (proc.stderr or proc.stdout or '')[:300])
        )
    text, usage = _cc_text_from_stream(proc.stdout)
    return BackgroundGenerationResult(
        text=text,
        provider='claude_code',
        model_identity=authority.model_identity,
        actual_executor='claude_code_background_oneshot',
        usage=usage,
    )


def _generate_api_relay(
    request: BackgroundGenerationRequest,
    authority: GenerationAuthoritySnapshot,
    *,
    relay_factory: Callable[[], Any] | None,
) -> BackgroundGenerationResult:
    model = str(authority.model_identity or '').strip()
    if not model or model == 'unknown':
        raise BackgroundGenerationError('relay_background_unknown_model')
    if relay_factory is None:
        from relay.manager import RelayManager
        relay_factory = RelayManager
    try:
        manager = relay_factory()
        response = manager.call(
            {
                'model': model,
                'max_tokens': int(request.max_tokens),
                'system': request.system_text,
                'messages': [{'role': 'user', 'content': request.prompt_text}],
            },
            timeout=float(request.timeout_sec),
            use_ws_model=False,
        )
    except Exception as exc:
        raise BackgroundGenerationError('relay_background_failed:%s' % exc) from exc
    if not isinstance(response, dict):
        raise BackgroundGenerationError('relay_background_invalid_response')
    response_model = str(response.get('model') or '').strip()
    if response_model and response_model != model:
        raise BackgroundGenerationError('relay_background_model_mismatch')
    try:
        text = str(manager.extract_text(response) or '')
    except Exception as exc:
        raise BackgroundGenerationError('relay_background_text_extract_failed') from exc
    raw_usage = response.get('usage')
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else None
    return BackgroundGenerationResult(
        text=text,
        provider='api_relay',
        model_identity=authority.model_identity,
        actual_executor='api_relay_background',
        usage=usage,
    )


def generate_background(
    request: BackgroundGenerationRequest,
    authority: GenerationAuthoritySnapshot,
    *,
    cc_token_getter: Callable[[], str] | None = None,
    relay_factory: Callable[[], Any] | None = None,
) -> BackgroundGenerationResult:
    """Generate from the explicitly supplied frozen authority only."""
    if authority.provider == 'claude_code':
        return _generate_claude_code(request, authority, token_getter=cc_token_getter)
    if authority.provider == 'api_relay':
        return _generate_api_relay(request, authority, relay_factory=relay_factory)
    raise BackgroundGenerationError('background_unknown_provider:%s' % authority.provider)
