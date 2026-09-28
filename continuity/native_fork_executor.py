"""R2 — Continuity Native-Fork execution adapter.

Given an *eligible* ``ContinuityForkPlan`` (see
``continuity/native_fork_eligibility.py``), forks the live main-chat Claude
session at its frozen boundary event and asks the forked child — which
already carries the full parent persona/system prompt and transcript — to
write the compression summary. This buys prompt-cache reuse of everything the
child inherited from the parent: native mode sends neither the first-five
persona sections nor the materialized evidence text, only the frozen
compression instruction plus an explicit turn-count scope (never left for the
model to guess).

Reuses primitives already validated elsewhere in this codebase rather than
inventing new ones:
  * ``claude_agent_sdk.fork_session`` — the same official primitive staged
    rewrite already uses (``chat/rewrite_native_fork.py``).
  * ``-p`` + ``--resume`` + ``--input-format stream-json`` — the exact
    invocation shape the nightly Forge canary proved works for a tool-isolated
    resumed one-shot (``chat/context_window_nightly_forge.py``).
  * ``tools/claude_forge_live_gate.py``'s stdout/JSONL-prefix helpers for the
    parent-transcript-unchanged guard.
  * ``chat/background_generation.py``'s own stream terminal parser for
    model-identity attestation and usage extraction, so the resulting
    ``BackgroundGenerationResult`` is byte-for-byte the same shape
    ``continuity/chunk_generation.py`` already validates and persists.

Failures raise ``NativeForkGenerationError`` and touch nothing durable. Its
``provider_started`` flag splits them: before the child's provider process is
launched the caller may run the existing isolated one-shot instead; once it
is launched a model request may have consumed tokens, so the caller must fail
the job rather than issue a second model call. This module never marks a job,
never writes a Chunk, and never mutates sealing or settings authority. The
child session is used once and then abandoned.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

from chat.background_generation import (
    BackgroundGenerationResult,
    _cc_terminal_from_stream,
)
from chat.rewrite_native_fork import import_fork_session
from continuity.native_fork_eligibility import ContinuityForkPlan
from tools.cc_jsonl_usage import session_jsonl_path
from tools.claude_forge_subprocess import run_subprocess_with_timeout

NATIVE_FORK_EXECUTOR = 'claude_code_continuity_native_fork'


class NativeForkGenerationError(RuntimeError):
    """Native fork could not complete.

    ``provider_started`` is False only when no provider process was launched,
    so the caller may safely run the isolated one-shot instead. Once it is
    True a model request may already have consumed tokens: the caller must
    fail the job and must not issue a second model call.
    """

    def __init__(self, reason: str, *, provider_started: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.provider_started = bool(provider_started)
        stage = 'post_provider' if self.provider_started else 'pre_provider'
        self.error_code = 'native_fork_%s:%s' % (stage, reason)


def _redact(value: str) -> str:
    raw = str(value or '').strip().encode('utf-8')
    if not raw:
        return ''
    return hashlib.sha256(raw).hexdigest()[:16]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _scope_instruction(
    prompt_body: str,
    scope_completed_turns: int,
    scope_wake_count: int = 0,
) -> str:
    n = max(0, int(scope_completed_turns or 0))
    wakes = max(0, int(scope_wake_count or 0))
    scope_note = (
        f'只总结这个 candidate 中最近 {n} 个 completed turns 和 {wakes} 个 canonical Wake；'
        'Wake 可以位于候选范围首尾或中间。更早的历史仅用于理解指代，不要展开复述。'
    )
    return f'{prompt_body}\n\n{scope_note}'


def execute_continuity_native_fork(
    plan: ContinuityForkPlan,
    *,
    prompt_body: str,
    authority: Any,
    cwd: str,
    claude_home: Optional[str] = None,
    timeout_sec: float = 120.0,
    fork_session_fn: Optional[Callable[..., Any]] = None,
    popen_factory: Optional[Callable[..., Any]] = None,
    token_getter: Optional[Callable[[], str]] = None,
    on_provider_start: Optional[Callable[[], None]] = None,
) -> BackgroundGenerationResult:
    """Fork the parent session and run one scoped resume turn on the child."""
    if not plan.eligible:
        raise NativeForkGenerationError('plan_not_eligible')

    provider = str(getattr(authority, 'provider', '') or '')
    if provider != 'claude_code':
        raise NativeForkGenerationError('unsupported_provider')

    parent_cwd = str(getattr(plan, 'parent_cwd', '') or '').strip()
    if not parent_cwd or not Path(parent_cwd).is_absolute():
        raise NativeForkGenerationError('parent_cwd_missing')

    parent_path = Path(plan.parent_transcript_path)
    try:
        before_hash = _sha256_file(parent_path)
    except OSError as exc:
        raise NativeForkGenerationError('parent_transcript_unreadable') from exc

    fork_fn = fork_session_fn
    if fork_fn is None:
        fork_fn, err = import_fork_session()
        if fork_fn is None:
            raise NativeForkGenerationError('sdk_unavailable:%s' % err)

    try:
        result = fork_fn(
            plan.parent_session_id,
            directory=parent_cwd,
            up_to_message_id=plan.fork_event_uuid,
            title=None,
        )
        child_sid = str(getattr(result, 'session_id', '') or '').strip()
        if not child_sid:
            raise RuntimeError('fork_session returned empty session_id')
    except Exception as exc:
        after_hash = before_hash
        try:
            after_hash = _sha256_file(parent_path)
        except OSError:
            pass
        if after_hash != before_hash:
            raise NativeForkGenerationError('parent_mutated') from exc
        raise NativeForkGenerationError('fork_failed:%s' % type(exc).__name__) from exc

    after_fork_hash = before_hash
    try:
        after_fork_hash = _sha256_file(parent_path)
    except OSError:
        pass
    if after_fork_hash != before_hash:
        raise NativeForkGenerationError('parent_mutated')

    child_path = session_jsonl_path(parent_cwd, child_sid, claude_home=claude_home)
    if child_path is None or not child_path.is_file():
        raise NativeForkGenerationError('child_transcript_missing')

    from chat.cc_model import cc_model_args_from_identity, cc_model_from_identity
    from chat.cc_runtime import claude_cmd_for_version, repo_root, require_managed_claude_runtime

    try:
        expected_model = cc_model_from_identity(authority.model_identity)
        model_args = cc_model_args_from_identity(authority.model_identity)
    except ValueError as exc:
        raise NativeForkGenerationError('invalid_model_identity') from exc

    getter = token_getter
    if getter is None:
        from chat.cc_auth import read_cc_oauth_token
        getter = read_cc_oauth_token
    try:
        token = str(getter() or '').strip()
    except Exception as exc:
        raise NativeForkGenerationError('token_unavailable') from exc
    if not token:
        raise NativeForkGenerationError('token_unavailable')

    root = repo_root()
    env = dict(os.environ)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = token
    env.pop('ANTHROPIC_API_KEY', None)

    try:
        runtime_version = require_managed_claude_runtime(
            env=env, cwd=str(root), timeout=min(float(timeout_sec), 60.0),
        )
    except Exception as exc:
        raise NativeForkGenerationError('runtime_unavailable') from exc

    user_message = _scope_instruction(
        prompt_body, plan.scope_completed_turns, plan.scope_wake_count,
    )
    stdin_payload = json.dumps(
        {'type': 'user', 'message': {'role': 'user', 'content': user_message}},
        ensure_ascii=False,
    ) + '\n'

    cmd = claude_cmd_for_version(
        runtime_version,
        '-p',
        '--resume', child_sid,
        '--input-format', 'stream-json',
        '--output-format', 'stream-json',
        '--verbose',
        '--max-turns', '1',
        '--tools', '',
        '--allowedTools', '',
        '--safe-mode',
        env=env,
    ) + model_args

    # From here on a model request may be in flight: every failure is
    # post-provider and must never be followed by a second model call.
    if on_provider_start is not None:
        on_provider_start()
    try:
        run = run_subprocess_with_timeout(
            cmd=cmd,
            cwd=parent_cwd,
            env=env,
            stdin_payload=stdin_payload,
            timeout_seconds=float(timeout_sec),
            popen_factory=popen_factory,
        )
    except Exception as exc:
        raise NativeForkGenerationError(
            'spawn_error:%s' % type(exc).__name__, provider_started=True,
        ) from exc
    if not run.process_started:
        raise NativeForkGenerationError('spawn_failed', provider_started=True)
    if run.timed_out:
        raise NativeForkGenerationError('timeout', provider_started=True)
    if run.exit_code != 0:
        raise NativeForkGenerationError('exit_%s' % run.exit_code, provider_started=True)

    # Parent must still be untouched after the whole generation window.
    final_parent_hash = before_hash
    try:
        final_parent_hash = _sha256_file(parent_path)
    except OSError:
        pass
    if final_parent_hash != before_hash:
        raise NativeForkGenerationError('parent_mutated', provider_started=True)

    terminal = _cc_terminal_from_stream(''.join(run.stdout_lines))
    if not terminal.result_seen:
        raise NativeForkGenerationError('result_missing', provider_started=True)
    if terminal.result_subtype != 'success' or terminal.result_is_error is not False:
        raise NativeForkGenerationError(
            'result_not_success:%s' % (terminal.result_subtype or '<missing>'),
            provider_started=True,
        )
    if expected_model and not terminal.init_model:
        raise NativeForkGenerationError('model_unattested', provider_started=True)
    if expected_model and terminal.init_model != expected_model:
        raise NativeForkGenerationError('model_mismatch', provider_started=True)

    text = (terminal.result_text or terminal.delta_text).strip()
    if not text:
        raise NativeForkGenerationError('empty_output', provider_started=True)

    usage = dict(terminal.usage or {})
    if terminal.init_model:
        usage['actual_init_model'] = terminal.init_model
    usage['continuity_native_fork_parent_session_hash'] = _redact(plan.parent_session_id)
    usage['continuity_native_fork_boundary_event_hash'] = _redact(plan.fork_event_uuid)
    usage['continuity_native_fork_child_session_hash'] = _redact(child_sid)
    usage['continuity_native_fork_scope_completed_turns'] = int(plan.scope_completed_turns or 0)
    usage['continuity_native_fork_scope_wake_count'] = int(plan.scope_wake_count or 0)

    return BackgroundGenerationResult(
        text=text,
        provider='claude_code',
        model_identity=authority.model_identity,
        actual_executor=NATIVE_FORK_EXECUTOR,
        usage=usage,
    )


__all__ = [
    'NATIVE_FORK_EXECUTOR',
    'NativeForkGenerationError',
    'execute_continuity_native_fork',
]
