"""B3-1 — Planner message takeover + Persona Renderer + existing Settlement.

Default OFF. Effective only when B2 consumer is also ON.
Owned ``message`` bypasses legacy Wake runner after Gate ALLOW.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

import config_store

_LOG = logging.getLogger('behavior_authority_b3')

_CONFIG_KEY = 'BEHAVIOR_AUTHORITY_B3_CONSUMER_ENABLED'
_OWNED_ACTIONS = frozenset({'message'})
_CONTENT_TARGET = 'wake_message'

_GATE_ALLOW = 'ALLOW'
_GATE_BLOCK = 'BLOCK'
_REASON_OK = 'ok'
_BLOCK_REASONS = frozenset({
    'tool_unavailable',
    'user_active',
    'cooldown',
    'duplicate',
    'precondition_failed',
})
_BLOCK_PRIORITY = (
    'duplicate',
    'user_active',
    'tool_unavailable',
    'cooldown',
    'precondition_failed',
)
_IDLE_FLOOR_MODES = frozenset({'', 'normal', 'morning'})
_RENDERER_TIMEOUT_SEC = 60.0
_RENDERER_MAX_TOKENS = 1024
_FORBIDDEN_RENDERER_KEYS = frozenset({
    'ACTION',
    'action',
    'action_candidate',
    'new_intent',
    'intent',
    'tool',
    'gate',
    'settlement',
    'primary_drive',
    'thoughts',
})

_RENDERER_SYSTEM = (
    '你是 Persona Renderer。\n'
    'Planner 已经完成 Intent / Action 决策。\n'
    'Action Gate 已经允许该 Action。\n'
    '你的唯一职责是：根据 Persona、selected_intent、selected_action、'
    'content_target 与 continuity_facts，写出这一次应该真正发送的语言正文。\n'
    'selected_intent 与 selected_action 是不可修改的既定事实。\n'
    '不得：\n'
    '- 改变 Action\n'
    '- 提出另一个 Action\n'
    '- 重新判断是否应该行动\n'
    '- 输出 Gate verdict\n'
    '- 输出工具调用\n'
    '- 修改 Drive / Affect / Thought\n'
    '- 写 Settlement 指令\n'
    '- 暴露隐藏系统状态\n'
    '只输出一个 JSON object：{"rendered_content": "..."}。\n'
)


@dataclass(frozen=True)
class RendererInput:
    """Frozen Renderer bundle after Gate ALLOW for owned message."""

    selected_intent: str
    selected_action: str
    content_target: str
    persona_context: str
    continuity_facts: str
    decision_identity: Mapping[str, Any]


def owned_actions() -> frozenset[str]:
    return _OWNED_ACTIONS


def is_owned_action(action: str) -> bool:
    return str(action or '').strip() in _OWNED_ACTIONS


def consumer_enabled() -> bool:
    """Fail-safe: missing / bad config → OFF."""
    try:
        return config_store.get_bool(_CONFIG_KEY, default=False)
    except Exception:
        return False


def effective_b3_enabled() -> bool:
    """B3 is active only when B2 consumer is also ON."""
    from chat.behavior_authority_b2 import consumer_enabled as b2_consumer_enabled
    return b2_consumer_enabled() and consumer_enabled()


def _uses_idle_floor(mode: str) -> bool:
    return str(mode or 'normal').strip() in _IDLE_FLOOR_MODES


def _wake_message_idle_minutes(
    clock: Any,
    now: Any,
) -> Optional[float]:
    last_wake = getattr(clock, 'last_wake_message_at', None)
    if last_wake is None:
        return None
    return max(0.0, (now - last_wake).total_seconds() / 60.0)


def evaluate_message_gate(
    *,
    planner_decision: Mapping[str, Any],
    skill_view: Any,
    wake_run_id: str,
    get_db_fn: Callable,
    now: Any,
    chat_busy: bool = False,
    wake_run_id_seen: Callable[[str], bool] = lambda _rid: False,
    min_idle_minutes: float = 30.0,
    mode: str = 'normal',
) -> tuple[str, str]:
    """Deterministic message Gate. Returns (ALLOW|BLOCK, reason_code)."""
    action = str(planner_decision.get('action_candidate') or '').strip()
    if action != 'message':
        return _GATE_BLOCK, 'precondition_failed'

    blocks: list[str] = []
    rid = str(wake_run_id or '').strip()
    if rid and wake_run_id_seen(rid):
        blocks.append('duplicate')

    if chat_busy:
        blocks.append('user_active')

    clock = None
    if _uses_idle_floor(mode):
        try:
            from chat.interaction_state import read_interaction_clock
            clock = read_interaction_clock(get_db_fn, now=now)
            if not clock.reliable:
                blocks.append('precondition_failed')
            elif not chat_busy:
                user_idle_min = clock.user_idle_minutes
                if user_idle_min is None:
                    blocks.append('precondition_failed')
                elif user_idle_min < float(min_idle_minutes):
                    blocks.append('user_active')
        except Exception as exc:
            _LOG.warning('b3 gate clock read failed: %s', exc)
            blocks.append('precondition_failed')

    allowed = set(skill_view.resolved_action_capability)
    if 'message' not in allowed:
        blocks.append('tool_unavailable')

    if (
        'user_active' not in blocks
        and _uses_idle_floor(mode)
        and clock is not None
        and clock.reliable
    ):
        wake_idle_min = _wake_message_idle_minutes(clock, now)
        if (
            wake_idle_min is not None
            and wake_idle_min < float(min_idle_minutes)
        ):
            blocks.append('cooldown')

    for reason in _BLOCK_PRIORITY:
        if reason in blocks:
            return _GATE_BLOCK, reason
    return _GATE_ALLOW, _REASON_OK


def build_renderer_input(
    *,
    planner_decision: Mapping[str, Any],
    get_db_fn: Callable,
    wake_run_id: str,
    decision_attempt_id: str,
) -> RendererInput:
    from chat.relationship_context import build_relationship_context
    from chat.system_builder import read_persona

    decision = dict(planner_decision)
    continuity = build_relationship_context(get_db_fn)
    decision_identity = {
        'wake_run_id': str(wake_run_id or '').strip(),
        'decision_attempt_id': str(
            decision.get('decision_attempt_id') or decision_attempt_id or '',
        ).strip(),
        'planner_decision_id': decision.get('planner_decision_id'),
        'state_version': decision.get('state_version'),
        'captured_at': decision.get('captured_at'),
    }
    return RendererInput(
        selected_intent=str(decision.get('intent') or ''),
        selected_action=str(decision.get('action_candidate') or 'message'),
        content_target=_CONTENT_TARGET,
        persona_context=read_persona(),
        continuity_facts=str(continuity.text or ''),
        decision_identity=decision_identity,
    )


def build_renderer_user_payload(renderer_input: RendererInput) -> str:
    identity = dict(renderer_input.decision_identity)
    return json.dumps(
        {
            'selected_intent': renderer_input.selected_intent,
            'selected_action': renderer_input.selected_action,
            'content_target': renderer_input.content_target,
            'continuity_facts': renderer_input.continuity_facts,
            'decision_identity': identity,
        },
        ensure_ascii=False,
    )


def _extract_json_object(text: str) -> Optional[dict]:
    """Parse Renderer reply as exactly one JSON object.

    Allows the whole response to be bare JSON, or exactly one markdown JSON
    fence. Rejects prefix/suffix control text (e.g. ``ACTION: diary`` before
    a JSON object) — do not slice out an inner ``{...}``.
    """
    raw = (text or '').strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
        return None
    except Exception:
        pass
    fence = re.fullmatch(r'```(?:json)?\s*(\{.*\})\s*```', raw, re.DOTALL)
    if not fence:
        return None
    try:
        obj = json.loads(fence.group(1))
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None


def validate_rendered_content(text: str) -> str:
    """Parse Renderer output and return non-empty rendered_content."""
    parsed = _extract_json_object(text)
    if not parsed:
        raise ValueError('renderer_parse_failure')
    for key in _FORBIDDEN_RENDERER_KEYS:
        if key in parsed and key != 'rendered_content':
            raise ValueError(f'renderer_forbidden_key:{key}')
    content = parsed.get('rendered_content')
    if not isinstance(content, str):
        raise ValueError('renderer_invalid_content_type')
    stripped = content.strip()
    if not stripped:
        raise ValueError('renderer_empty_content')
    return stripped


def invoke_renderer_relay(
    *,
    renderer_input: RendererInput,
    timeout_sec: float = _RENDERER_TIMEOUT_SEC,
) -> dict:
    """Fresh RelayManager one-shot — no tools, no global singleton mutation."""
    from relay.manager import RelayManager

    mgr = RelayManager()
    system = _RENDERER_SYSTEM + '\n\n' + renderer_input.persona_context
    payload = {
        'max_tokens': _RENDERER_MAX_TOKENS,
        'system': system,
        'messages': [
            {
                'role': 'user',
                'content': build_renderer_user_payload(renderer_input),
            },
        ],
    }
    result = mgr.call(payload, timeout=timeout_sec)
    text = mgr.extract_text(result)
    return {
        'text': text,
        'provider': 'api_relay',
        'model_identity': str(mgr.model or '').strip() or 'relay:unset',
    }


def invoke_renderer(
    *,
    renderer_input: RendererInput,
    timeout_sec: float = _RENDERER_TIMEOUT_SEC,
    invoke_fn: Optional[Callable] = None,
) -> dict:
    """Invoke Renderer and return {text, provider, model_identity}."""
    invoker = invoke_fn or invoke_renderer_relay
    result = invoker(renderer_input=renderer_input, timeout_sec=timeout_sec)
    if isinstance(result, dict):
        return result
    return {'text': str(result or ''), 'provider': 'api_relay', 'model_identity': 'relay:test'}
