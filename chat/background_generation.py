"""Provider-neutral, one-shot background text generation.

This module is an execution adapter only. Callers own context and output
semantics; the supplied frozen authority owns provider and model.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from chat.provider_router import GenerationAuthoritySnapshot


@dataclass(frozen=True)
class BackgroundGenerationRequest:
    system_text: str
    prompt_text: str
    # Best-effort output budget; the pinned Claude Code CLI has no hard output-token flag.
    max_tokens_hint: int
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


@dataclass(frozen=True)
class _CcTerminal:
    result_seen: bool
    result_subtype: str
    result_is_error: object
    result_text: str
    usage: dict[str, Any] | None
    init_model: str
    delta_text: str


def _cc_terminal_from_stream(stdout: str) -> _CcTerminal:
    result_seen = False
    result_subtype = ''
    result_is_error: object = None
    result_text = ''
    init_model = ''
    deltas: list[str] = []
    usage: dict[str, Any] | None = None
    for line in str(stdout or '').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            init_model = str(event.get('model') or '').strip()
        if event.get('type') == 'result':
            result_seen = True
            result_subtype = str(event.get('subtype') or '').strip()
            result_is_error = event.get('is_error')
            result_text = str(event.get('result') or '') if isinstance(event.get('result'), str) else ''
            candidate = event.get('usage')
            usage = dict(candidate) if isinstance(candidate, dict) else usage
        stream_event = event.get('event')
        if event.get('type') == 'stream_event' and isinstance(stream_event, dict):
            delta = stream_event.get('delta')
            if isinstance(delta, dict) and delta.get('type') == 'text_delta':
                chunk = delta.get('text')
                if isinstance(chunk, str):
                    deltas.append(chunk)
    return _CcTerminal(
        result_seen=result_seen,
        result_subtype=result_subtype,
        result_is_error=result_is_error,
        result_text=result_text,
        usage=usage,
        init_model=init_model,
        delta_text=''.join(deltas),
    )


def _generate_claude_code(
    request: BackgroundGenerationRequest,
    authority: GenerationAuthoritySnapshot,
    *,
    token_getter: Callable[[], str] | None,
    relay_factory: Callable[[], Any] | None,
) -> BackgroundGenerationResult:
    from chat.cc_auth import CCProviderEnvError, build_cc_provider_env
    from chat.cc_model import cc_model_args_from_identity, cc_model_from_identity
    from chat.cc_runtime import ClaudeRuntimeError, claude_cmd_for_version, repo_root, require_managed_claude_runtime

    if authority.provider == 'api_relay':
        expected_model = str(authority.model_identity or '').strip()
        if expected_model.startswith('explicit:'):
            expected_model = expected_model[len('explicit:'):].strip()
        if not expected_model or expected_model.lower() == 'unknown':
            raise BackgroundGenerationError('api_relay_model_unavailable')
        model_args = ['--model', expected_model]
    else:
        try:
            expected_model = cc_model_from_identity(authority.model_identity)
            model_args = cc_model_args_from_identity(authority.model_identity)
        except ValueError as exc:
            raise BackgroundGenerationError('cc_background_invalid_model_identity') from exc

    try:
        env = build_cc_provider_env(
            authority.provider,
            authority.model_identity,
            token_getter=token_getter,
            relay_factory=relay_factory,
        )
    except CCProviderEnvError as exc:
        raise BackgroundGenerationError(str(exc)) from exc

    env.pop('HAYAGARDEN_CC_PROVIDER', None)
    env.pop('HAYAGARDEN_CC_PROVIDER_IDENTITY', None)
    root = repo_root()
    try:
        runtime_version = require_managed_claude_runtime(
            env=env,
            cwd=str(root),
            timeout=min(float(request.timeout_sec), 60.0),
        )
        # The prompt goes through stdin: as one argv element it hits Linux
        # MAX_ARG_STRLEN (128 KiB) on large inputs (e.g. a full DayBlock day,
        # ~43k CJK chars) and fails with E2BIG before the process starts.
        proc = subprocess.run(
            claude_cmd_for_version(
                runtime_version,
                '-p',
                '--output-format', 'stream-json',
                '--verbose',
                '--max-turns', '1',
                '--tools', '',
                '--system-prompt', request.system_text,
                '--safe-mode',
                '--no-session-persistence',
                env=env,
            ) + model_args,
            cwd=str(root),
            env=env,
            input=request.prompt_text,
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
    terminal = _cc_terminal_from_stream(proc.stdout)
    if not terminal.result_seen:
        raise BackgroundGenerationError('cc_background_result_missing')
    if terminal.result_subtype != 'success' or terminal.result_is_error is not False:
        raise BackgroundGenerationError(
            'cc_background_result_not_success:%s' % (terminal.result_subtype or '<missing>')
        )
    if expected_model and not terminal.init_model:
        raise BackgroundGenerationError('cc_background_model_unattested')
    if expected_model and terminal.init_model != expected_model:
        raise BackgroundGenerationError('cc_background_model_mismatch')
    usage = dict(terminal.usage or {})
    if terminal.init_model:
        usage['actual_init_model'] = terminal.init_model
    return BackgroundGenerationResult(
        text=(terminal.result_text or terminal.delta_text).strip(),
        provider=authority.provider,
        model_identity=authority.model_identity,
        actual_executor='claude_code_background_oneshot',
        usage=usage or None,
    )


def generate_background(
    request: BackgroundGenerationRequest,
    authority: GenerationAuthoritySnapshot,
    *,
    cc_token_getter: Callable[[], str] | None = None,
    relay_factory: Callable[[], Any] | None = None,
) -> BackgroundGenerationResult:
    """Generate from the explicitly supplied frozen authority only."""
    if authority.provider not in ('claude_code', 'api_relay'):
        raise BackgroundGenerationError('background_unknown_provider:%s' % authority.provider)
    return _generate_claude_code(
        request,
        authority,
        token_getter=cc_token_getter,
        relay_factory=relay_factory,
    )
