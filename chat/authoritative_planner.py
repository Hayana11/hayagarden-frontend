"""Minimal authoritative Planner for basic normal Unified Wake.

This module is deliberately smaller than Shadow Planner.  It accepts only
GuardClock facts, makes one bounded Claude Code call, and returns only the
basic none/message Decision.  It never creates a resident, session, transcript
row, Wake log entry, or tool call.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from chat.cc_model import cc_model_args
from chat.cc_runtime import (
    claude_cmd,
    repo_root,
    require_pinned_claude_version,
)

_PLANNER_TIMEOUT_SEC = 25.0


def _main_chat_oauth_token() -> str:
    """Read the live OAuth token used by the main gateway chat."""
    try:
        from gateway import CC_TOKEN
    except Exception as exc:
        raise RuntimeError('main_chat_oauth_token_unavailable') from exc
    token = str(CC_TOKEN or '').strip()
    if not token:
        raise RuntimeError('main_chat_oauth_token_missing')
    return token


_SYSTEM_PROMPT = (
    'You are the authoritative basic Unified Wake Planner. '
    'Return exactly one JSON object and do not use tools. '
    'Choose only action_candidate none or message. '
    'For none, intent is a short internal reason and content is empty. '
    'For message, intent is a short internal intent; do not write visible content. '
    'Do not infer emotion, drive, longing, internal state, history, or memory. '
    'If uncertain, return none with blocked true and a reason_codes list. '
    'The JSON keys must be intent, action_candidate, confidence, blocked, '
    'reason_codes, wake_run_id.'
)


@dataclass(frozen=True)
class BasicWakePlannerInput:
    wake_run_id: str
    mode: str
    observed_at: datetime.datetime
    user_idle_hours: float
    effective_idle_hours: float
    allowed_actions: tuple[str, ...] = ('none', 'message')
    extra_context: dict[str, Any] = field(default_factory=dict)

    def build_user_payload(self) -> str:
        payload = {
            'wake_run_id': self.wake_run_id,
            'mode': self.mode,
            'observed_at': self.observed_at.isoformat(),
            'user_idle_hours': round(float(self.user_idle_hours), 3),
            'effective_idle_hours': round(float(self.effective_idle_hours), 3),
            'allowed_actions': list(self.allowed_actions),
            'extra_context': {},
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def make_basic_wake_planner_input(
    *,
    wake_run_id: str,
    observed_at: datetime.datetime,
    user_idle_hours: float,
    effective_idle_hours: float,
    mode: str = 'normal',
) -> BasicWakePlannerInput:
    return BasicWakePlannerInput(
        wake_run_id=str(wake_run_id or ''),
        mode=str(mode or 'normal'),
        observed_at=observed_at,
        user_idle_hours=float(user_idle_hours),
        effective_idle_hours=float(effective_idle_hours),
    )


def _input_from_view(
    planner_input: Any,
    *,
    wake_run_id: str,
    observed_at: Optional[datetime.datetime] = None,
) -> BasicWakePlannerInput:
    captured_at = observed_at or getattr(
        planner_input, 'observed_at', datetime.datetime.now()
    )
    return make_basic_wake_planner_input(
        wake_run_id=wake_run_id,
        observed_at=captured_at,
        user_idle_hours=float(
            getattr(planner_input, 'user_idle_hours', 0.0) or 0.0
        ),
        effective_idle_hours=float(
            getattr(planner_input, 'effective_idle_hours', 0.0) or 0.0
        ),
        mode=str(getattr(planner_input, 'mode', 'normal') or 'normal'),
    )


def _json_object(text: str) -> dict[str, Any]:
    raw = str(text or '').strip()
    if not raw:
        raise ValueError('planner_empty_response')
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
        for line in reversed(raw.splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and (
                candidate.get('type') == 'result'
                or 'action_candidate' in candidate
            ):
                value = candidate.get('result', candidate)
                break
        if value is None:
            raise ValueError('planner_invalid_json')
    if not isinstance(value, dict):
        raise ValueError('planner_result_not_object')
    return value


def _stream_result(stdout: str) -> str:
    result_text = ''
    deltas: list[str] = []
    for line in str(stdout or '').splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result':
            result = event.get('result')
            if isinstance(result, str):
                result_text = result
        if event.get('type') == 'content_block_delta':
            delta = event.get('delta')
            if isinstance(delta, dict) and isinstance(delta.get('text'), str):
                deltas.append(delta['text'])
    return result_text or ''.join(deltas)


def invoke_cc_planner(
    *,
    user_payload: str,
    timeout_sec: float = _PLANNER_TIMEOUT_SEC,
) -> dict[str, str]:
    root = repo_root()
    env = os.environ.copy()
    env.pop('ANTHROPIC_API_KEY', None)
    env['CLAUDE_CODE_OAUTH_TOKEN'] = _main_chat_oauth_token()
    runtime_version = require_pinned_claude_version(
        env=env,
        cwd=str(root),
        root=root,
        timeout=min(float(timeout_sec), 60.0),
    )
    argv = claude_cmd(
        '-p',
        user_payload,
        '--output-format',
        'stream-json',
        '--verbose',
        '--max-turns',
        '1',
        '--tools',
        '',
        '--system-prompt',
        _SYSTEM_PROMPT,
        root=root,
    ) + cc_model_args()
    proc = subprocess.run(
        argv,
        cwd=str(root),
        env=env,
        text=True,
        capture_output=True,
        timeout=float(timeout_sec),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            'claude_planner_exit_%s:%s'
            % (proc.returncode, (proc.stderr or proc.stdout or '')[:300])
        )
    return {
        'text': _stream_result(proc.stdout),
        'provider': 'claude_code',
        'model_identity': 'claude_code_planner:%s' % runtime_version,
    }


def _validate_decision(
    value: dict[str, Any],
    *,
    wake_run_id: str,
) -> dict[str, Any]:
    action = str(value.get('action_candidate') or '').strip()
    if action not in ('none', 'message'):
        raise ValueError('action_not_in_basic_capability')
    intent = str(value.get('intent') or '').strip()
    if not intent:
        raise ValueError('planner_intent_missing')
    try:
        confidence = float(value.get('confidence'))
    except (TypeError, ValueError):
        raise ValueError('planner_confidence_invalid')
    if not 0.0 <= confidence <= 1.0:
        raise ValueError('planner_confidence_invalid')
    reason_codes = value.get('reason_codes', [])
    if not isinstance(reason_codes, list):
        raise ValueError('planner_reason_codes_invalid')
    blocked = bool(value.get('blocked', False))
    if blocked and action != 'none':
        raise ValueError('blocked_message_not_allowed')
    echoed_run_id = str(value.get('wake_run_id') or '').strip()
    if echoed_run_id != str(wake_run_id or '').strip():
        raise ValueError('wake_run_id_mismatch')
    return {
        'intent': intent,
        'action_candidate': action,
        'confidence': confidence,
        'blocked': blocked,
        'reason_codes': [str(code) for code in reason_codes[:8]],
        'wake_run_id': str(wake_run_id),
        'source': 'authoritative_cc_planner',
        'shadow_only': False,
        'authoritative': True,
        'extra_context': {},
    }


def run_authoritative_cc_planner(
    *,
    planner_input: Any,
    wake_run_id: str,
    decision_attempt_id: str,
    timeout_sec: float = _PLANNER_TIMEOUT_SEC,
    invoke_fn: Optional[Callable] = None,
) -> tuple[str, dict[str, Any]]:
    del decision_attempt_id
    try:
        basic_input = (
            planner_input
            if isinstance(planner_input, BasicWakePlannerInput)
            else _input_from_view(planner_input, wake_run_id=wake_run_id)
        )
        payload = basic_input.build_user_payload()
        invoker = invoke_fn or invoke_cc_planner
        result = invoker(user_payload=payload, timeout_sec=timeout_sec)
        if isinstance(result, dict):
            text = result.get('text', result.get('result', ''))
        else:
            text = result
        parsed = _json_object(text)
        return 'valid', _validate_decision(parsed, wake_run_id=wake_run_id)
    except Exception as exc:
        return 'error', {
            'error': str(exc)[:500],
            'source': 'authoritative_cc_planner',
            'shadow_only': False,
            'authoritative': True,
            'wake_run_id': str(wake_run_id),
        }
