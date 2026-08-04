"""B1-1B — non-blocking Planner Shadow runtime.

Consumes frozen PlannerStateView V + CapabilitySkillView K.
Invokes an isolated Relay one-shot (never CC Wake resident).
Fail-open: any Shadow failure is observed then dropped; production continues.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
import datetime
from typing import Any, Mapping, Optional

_LOG = logging.getLogger('planner_shadow')

_DRIVE_KEYS = frozenset({
    'attachment', 'curiosity', 'reflection', 'social',
    'duty', 'libido', 'stress', 'fatigue',
})

_DEFAULT_OBSERVE_PATH = '/opt/frontend/planner_shadow.jsonl'
_SHADOW_TIMEOUT_SEC = 15
_SHADOW_MAX_TOKENS = 700

_SHADOW_SYSTEM = """你是 Wake Planner Shadow：只做结构化行为决策，不执行、不写给用户看的正文。

硬规则：
1. State modifies tendency, not command — 禁止固定 Drive→Action 映射。
2. 不得写 attachment→message / curiosity→explore / libido→flirt 这类命令表。
3. action_candidate=none 是合法正常 Decision。
4. 只能从 CapabilitySkillView.resolved_action_capability 中选择 action_candidate，或 none。
5. 不要写最终用户消息正文；不要调用工具。
6. 只输出一个 JSON 对象（不要 markdown 围栏外的解释）。
7. primary_drive / contributors 必须来自当前 Decision-time 状态推理，禁止从 action 反推 Drive。
8. source 必须是 "planner_shadow"；shadow_only 必须是 true。

输出 JSON schema：
{
  "intent": string,
  "action_candidate": "none"|"message"|"diary"|"explore",
  "confidence": number 0..1,
  "primary_drive": string|null,
  "contributors": [string],
  "blocked": boolean,
  "reason_codes": [string]
}
"""


def _now_beijing() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _now_str(dt: Optional[datetime.datetime] = None) -> str:
    return (dt or _now_beijing()).strftime('%Y-%m-%d %H:%M:%S')


def observation_path() -> str:
    return os.environ.get('PLANNER_SHADOW_OBSERVE_PATH', _DEFAULT_OBSERVE_PATH)


def append_shadow_observation(record: Mapping[str, Any]) -> None:
    """Non-authoritative JSONL append. Fail-open on IO errors."""
    path = observation_path()
    try:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(dict(record), ensure_ascii=False) + '\n')
    except Exception as exc:
        _LOG.warning('planner_shadow observation write failed: %s', exc)


def _view_to_dict(view: Any) -> dict:
    return {
        'state_version': int(view.state_version),
        'observed_at': view.observed_at,
        'wake_run_id': view.wake_run_id,
        'affect': dict(view.affect),
        'bond': dict(view.bond),
        'drives': dict(view.drives),
        'longing_derived': float(view.longing_derived),
        'interaction_clock': dict(view.interaction_clock),
        'immutable': True,
    }


def _skill_to_dict(skill: Any) -> dict:
    return {
        'wake_run_id': skill.wake_run_id,
        'captured_at': skill.captured_at,
        'provider': skill.provider,
        'model_identity': skill.model_identity,
        'wake_mode': skill.wake_mode,
        'resolved_action_capability': list(skill.resolved_action_capability),
        'allowed_action_families': list(skill.allowed_action_families),
        'tool_allowlist': list(skill.tool_allowlist),
        'available_tools': list(skill.available_tools),
        'provider_availability': dict(skill.provider_availability),
        'mode_contract': dict(skill.mode_contract),
        'preconditions': dict(skill.preconditions),
        'external_effect_class': dict(skill.external_effect_class),
        'external_effect_capabilities': dict(skill.external_effect_capabilities),
        'source': skill.source,
        'frozen_after': skill.frozen_after,
        'immutable': True,
    }


def build_shadow_user_payload(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
) -> str:
    payload = {
        'wake_run_id': wake_run_id,
        'PlannerStateView': _view_to_dict(planner_view),
        'CapabilitySkillView': _skill_to_dict(skill_view),
        'instructions': (
            '基于以上 V+K 形成一次 Shadow Decision JSON。'
            '不要输出用户可见正文。'
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _extract_json_object(text: str) -> Optional[dict]:
    raw = (text or '').strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def validate_shadow_decision(
    raw: Mapping[str, Any],
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    planner_decision_id: str,
    decision_attempt_id: str,
    captured_at: str,
) -> tuple[str, dict]:
    """Return (status, decision_or_error). status: valid | invalid."""
    allowed = set(skill_view.resolved_action_capability) | {'none'}
    action = str(raw.get('action_candidate') or '').strip()
    intent = str(raw.get('intent') or '').strip()
    try:
        confidence = float(raw.get('confidence'))
    except (TypeError, ValueError):
        return 'invalid', {'error': 'confidence_unreadable', 'raw': dict(raw)}
    if not (0.0 <= confidence <= 1.0):
        return 'invalid', {'error': 'confidence_out_of_range', 'raw': dict(raw)}
    if action not in allowed:
        return 'invalid', {'error': 'action_not_in_capability', 'raw': dict(raw)}
    if not intent:
        return 'invalid', {'error': 'intent_missing', 'raw': dict(raw)}

    primary = raw.get('primary_drive')
    if primary is not None:
        primary = str(primary).strip()
        if primary == '':
            primary = None
        elif primary not in _DRIVE_KEYS:
            return 'invalid', {'error': 'primary_drive_illegal', 'raw': dict(raw)}

    contributors = raw.get('contributors') or []
    if not isinstance(contributors, list):
        return 'invalid', {'error': 'contributors_not_list', 'raw': dict(raw)}
    contrib_out = [str(x) for x in contributors]

    reason_codes = raw.get('reason_codes') or []
    if not isinstance(reason_codes, list):
        return 'invalid', {'error': 'reason_codes_not_list', 'raw': dict(raw)}
    reasons = [str(x) for x in reason_codes]

    blocked = bool(raw.get('blocked'))

    # Non-none action requires a valid provenance-bearing primary when blocked=false
    # is not required by contract — but missing primary on non-none is allowed as
    # null only when Action semantics permit; Shadow marks invalid if non-none and
    # primary missing? Contract: "若存在必须是合法 Drive key" — null OK.
    # "action_candidate != none but provenance missing" — for Shadow observation,
    # treat missing primary on non-none as invalid (cannot invent from action).
    if action != 'none' and primary is None and not blocked:
        # still allow if blocked — quiet; for active propose, require primary
        return 'invalid', {
            'error': 'primary_drive_missing_for_active_action',
            'raw': dict(raw),
        }

    decision = {
        'planner_decision_id': planner_decision_id,
        'wake_run_id': wake_run_id,
        'decision_attempt_id': decision_attempt_id,
        'captured_at': captured_at,
        'state_version': int(planner_view.state_version),
        'intent': intent,
        'action_candidate': action,
        'confidence': confidence,
        'primary_drive': primary,
        'contributors': contrib_out,
        'blocked': blocked,
        'reason_codes': reasons,
        'source': 'planner_shadow',
        'shadow_only': True,
    }
    # Enforce identity fields even if model echoed wrong ones.
    if str(raw.get('wake_run_id') or wake_run_id) != wake_run_id:
        return 'invalid', {'error': 'wake_run_id_mismatch', 'raw': dict(raw)}
    try:
        if int(raw.get('state_version', planner_view.state_version)) != int(
            planner_view.state_version
        ):
            return 'invalid', {'error': 'state_version_mismatch', 'raw': dict(raw)}
    except (TypeError, ValueError):
        return 'invalid', {'error': 'state_version_unreadable', 'raw': dict(raw)}
    return 'valid', decision


def invoke_shadow_planner_relay(
    *,
    user_payload: str,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
) -> str:
    """Isolated Relay one-shot. Never touches CC Wake resident / tools."""
    from relay.manager import relay as _relay

    payload = {
        'max_tokens': _SHADOW_MAX_TOKENS,
        'system': _SHADOW_SYSTEM,
        'messages': [{'role': 'user', 'content': user_payload}],
        'metadata': {'source': 'planner_shadow', 'shadow_only': True},
    }
    # Explicitly no tools — Shadow must not execute Wake tools.
    result = _relay.call(payload, timeout=timeout_sec)
    return _relay.extract_text(result)


def run_shadow_attempt(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    legacy_provenance: Optional[Mapping[str, Any]] = None,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
    invoke_fn=None,
) -> dict:
    """Synchronous Shadow attempt → observation record (caller may thread it)."""
    attempt_id = str(uuid.uuid4())
    planner_decision_id = attempt_id
    captured_at = _now_str()
    base = {
        'wake_run_id': wake_run_id,
        'decision_attempt_id': attempt_id,
        'planner_decision_id': planner_decision_id,
        'state_version': int(planner_view.state_version),
        'observed_at': planner_view.observed_at,
        'captured_at': captured_at,
        'provider': skill_view.provider,
        'model_identity': skill_view.model_identity,
        'legacy_provenance': dict(legacy_provenance or {}),
        'capability': {
            'resolved_action_capability': list(skill_view.resolved_action_capability),
            'tool_allowlist': list(skill_view.tool_allowlist),
        },
        'shadow_only': True,
        'authoritative': False,
    }

    # Pairing invariant
    if (
        str(planner_view.wake_run_id or '') != str(wake_run_id)
        or str(skill_view.wake_run_id or '') != str(wake_run_id)
    ):
        rec = {
            **base,
            'shadow_status': 'invalid',
            'error_category': 'wake_run_id_pairing',
        }
        append_shadow_observation(rec)
        return rec

    try:
        user_payload = build_shadow_user_payload(
            planner_view=planner_view,
            skill_view=skill_view,
            wake_run_id=wake_run_id,
        )
        invoker = invoke_fn or invoke_shadow_planner_relay
        text = invoker(user_payload=user_payload, timeout_sec=timeout_sec)
        parsed = _extract_json_object(text)
        if not parsed:
            rec = {
                **base,
                'shadow_status': 'invalid',
                'error_category': 'parse_failure',
                'raw_text': (text or '')[:2000],
            }
            append_shadow_observation(rec)
            return rec
        status, decision = validate_shadow_decision(
            parsed,
            planner_view=planner_view,
            skill_view=skill_view,
            wake_run_id=wake_run_id,
            planner_decision_id=planner_decision_id,
            decision_attempt_id=attempt_id,
            captured_at=captured_at,
        )
        if status != 'valid':
            rec = {
                **base,
                'shadow_status': 'invalid',
                'error_category': decision.get('error', 'schema_invalid'),
                'raw_decision': decision.get('raw', parsed),
            }
            append_shadow_observation(rec)
            return rec
        rec = {
            **base,
            'shadow_status': 'valid',
            'shadow_decision': decision,
        }
        append_shadow_observation(rec)
        return rec
    except Exception as exc:
        rec = {
            **base,
            'shadow_status': 'error',
            'error_category': type(exc).__name__,
            'error': str(exc)[:500],
        }
        try:
            append_shadow_observation(rec)
        except Exception:
            pass
        return rec


def dispatch_planner_shadow(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    legacy_provenance: Optional[Mapping[str, Any]] = None,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
) -> None:
    """Freeze/dispatch Shadow on a daemon thread. Never blocks production."""

    def _worker():
        try:
            run_shadow_attempt(
                planner_view=planner_view,
                skill_view=skill_view,
                wake_run_id=wake_run_id,
                legacy_provenance=legacy_provenance,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            _LOG.warning('planner_shadow worker failed: %s', exc)

    try:
        t = threading.Thread(
            target=_worker,
            daemon=True,
            name='planner-shadow-%s' % (wake_run_id or 'anon')[:40],
        )
        t.start()
    except Exception as exc:
        _LOG.warning('planner_shadow dispatch failed: %s', exc)
        # Fail-open: also try to record dispatch failure without raising.
        try:
            append_shadow_observation({
                'wake_run_id': wake_run_id,
                'shadow_status': 'error',
                'error_category': 'dispatch_failed',
                'error': str(exc)[:500],
                'shadow_only': True,
                'authoritative': False,
            })
        except Exception:
            pass
