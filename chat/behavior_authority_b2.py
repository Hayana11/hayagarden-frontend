"""B2-1 — Planner none takeover + deterministic Action Gate.

Default OFF. When enabled, owned ``none`` decisions bypass legacy Wake runner
and cannot fall back to legacy on Gate BLOCK.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

import config_store

_LOG = logging.getLogger('behavior_authority_b2')

_CONFIG_KEY = 'BEHAVIOR_AUTHORITY_B2_CONSUMER_ENABLED'
_OWNED_ACTIONS = frozenset({'none'})

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


@dataclass(frozen=True)
class B2WakePlan:
    """Routing outcome for one Wake attempt after authoritative Planner input."""

    route: str  # legacy | blocked | none_takeover | message_takeover
    gate_reason: Optional[str] = None
    planner_decision: Optional[dict] = None
    planner_provenance: Optional[dict] = None


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


def freeze_provenance_from_planner_decision(decision: Mapping[str, Any]) -> dict:
    """Decision-time provenance for authoritative V3 Settlement."""
    return {
        'source': 'planner_authority',
        'captured_at': str(decision.get('captured_at') or '').strip(),
        'primary_drive': decision.get('primary_drive'),
        'contributors': list(decision.get('contributors') or []),
        'blocked': bool(decision.get('blocked')),
        'suggested_action': decision.get('action_candidate'),
        'planner_decision_id': decision.get('planner_decision_id'),
        'decision_attempt_id': decision.get('decision_attempt_id'),
        'state_version': decision.get('state_version'),
    }


def evaluate_action_gate(
    *,
    planner_decision: Mapping[str, Any],
    skill_view: Any,
    wake_run_id: str,
    get_db_fn: Callable,
    now: datetime.datetime,
    chat_busy: bool = False,
    wake_run_id_seen: Callable[[str], bool] = lambda _rid: False,
    min_idle_minutes: float = 30.0,
    mode: str = 'normal',
) -> tuple[str, str]:
    """Deterministic reality gate. Returns (ALLOW|BLOCK, reason_code)."""
    action = str(planner_decision.get('action_candidate') or '').strip()
    if action not in _OWNED_ACTIONS:
        return _GATE_BLOCK, 'precondition_failed'

    blocks: list[str] = []
    rid = str(wake_run_id or '').strip()
    if rid and wake_run_id_seen(rid):
        blocks.append('duplicate')

    if chat_busy:
        blocks.append('user_active')
    else:
        try:
            from chat.interaction_state import read_interaction_clock, wake_guard_reason
            clock = read_interaction_clock(get_db_fn, now=now)
            guard = wake_guard_reason(
                clock,
                mode=mode,
                min_idle_minutes=min_idle_minutes,
                chat_busy=False,
                wake_busy=False,
            )
            if guard in ('chat_generating', 'recent_interaction'):
                blocks.append('user_active')
            elif guard == 'clock_unreliable':
                blocks.append('precondition_failed')
        except Exception as exc:
            _LOG.warning('b2 gate clock read failed: %s', exc)
            blocks.append('precondition_failed')

    allowed = set(skill_view.resolved_action_capability) | {'none'}
    if action not in allowed:
        blocks.append('tool_unavailable')

    for reason in _BLOCK_PRIORITY:
        if reason in blocks:
            return _GATE_BLOCK, reason
    return _GATE_ALLOW, _REASON_OK


def plan_b2_wake_action(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    decision_attempt_id: str,
    get_db_fn: Callable,
    now: datetime.datetime,
    chat_busy_fn: Optional[Callable[[], bool]] = None,
    wake_run_id_seen: Callable[[str], bool] = lambda _rid: False,
    min_idle_minutes: float = 30.0,
    mode: str = 'normal',
    invoke_fn: Optional[Callable] = None,
) -> B2WakePlan:
    """Produce authoritative Planner Decision and ownership routing."""
    if not consumer_enabled():
        return B2WakePlan(route='legacy')

    from chat.planner_shadow import run_authoritative_planner_decision

    status, decision = run_authoritative_planner_decision(
        planner_view=planner_view,
        skill_view=skill_view,
        wake_run_id=wake_run_id,
        decision_attempt_id=decision_attempt_id,
        invoke_fn=invoke_fn,
    )
    if status != 'valid' or not isinstance(decision, dict):
        return B2WakePlan(route='legacy')

    action = str(decision.get('action_candidate') or '').strip()

    if action == 'message':
        from chat.behavior_authority_b3 import (
            effective_b3_enabled,
            evaluate_message_gate,
        )
        if not effective_b3_enabled():
            return B2WakePlan(route='legacy')
        # B3 ownership established; post-ownership failures must not legacy.
        try:
            chat_busy = bool(chat_busy_fn()) if chat_busy_fn is not None else False
            verdict, gate_reason = evaluate_message_gate(
                planner_decision=decision,
                skill_view=skill_view,
                wake_run_id=wake_run_id,
                get_db_fn=get_db_fn,
                now=now,
                chat_busy=chat_busy,
                wake_run_id_seen=wake_run_id_seen,
                min_idle_minutes=min_idle_minutes,
                mode=mode,
            )
        except Exception as exc:
            _LOG.warning('b3 owned gate evaluation failed: %s', exc)
            return B2WakePlan(
                route='blocked',
                gate_reason='precondition_failed',
                planner_decision=decision,
            )
        if verdict == _GATE_BLOCK:
            return B2WakePlan(
                route='blocked',
                gate_reason=gate_reason,
                planner_decision=decision,
            )
        return B2WakePlan(
            route='message_takeover',
            gate_reason=gate_reason,
            planner_decision=decision,
            planner_provenance=freeze_provenance_from_planner_decision(decision),
        )

    if not is_owned_action(action):
        return B2WakePlan(route='legacy')

    # Ownership established for owned none; post-ownership reality failures must
    # not bubble to gateway fail-open legacy fallback.
    try:
        chat_busy = bool(chat_busy_fn()) if chat_busy_fn is not None else False
        verdict, gate_reason = evaluate_action_gate(
            planner_decision=decision,
            skill_view=skill_view,
            wake_run_id=wake_run_id,
            get_db_fn=get_db_fn,
            now=now,
            chat_busy=chat_busy,
            wake_run_id_seen=wake_run_id_seen,
            min_idle_minutes=min_idle_minutes,
            mode=mode,
        )
    except Exception as exc:
        _LOG.warning('b2 owned gate evaluation failed: %s', exc)
        return B2WakePlan(
            route='blocked',
            gate_reason='precondition_failed',
            planner_decision=decision,
        )

    if verdict == _GATE_BLOCK:
        return B2WakePlan(route='blocked', gate_reason=gate_reason, planner_decision=decision)

    if action == 'none':
        return B2WakePlan(
            route='none_takeover',
            gate_reason=gate_reason,
            planner_decision=decision,
            planner_provenance=freeze_provenance_from_planner_decision(decision),
        )
    return B2WakePlan(route='legacy')
