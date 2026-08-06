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

# Contract §4: contributors = participating Drive / Affect / Bond / Longing
# factors from Decision-time V — not free-form provenance strings.
_AFFECT_FACTORS = frozenset({
    'affect.pa', 'affect.na', 'affect.valence', 'affect.arousal',
    'affect.mood_word',
})
_BOND_FACTORS = frozenset({
    'bond.intimacy', 'bond.passion', 'bond.commitment',
})
_LONGING_FACTORS = frozenset({'longing', 'longing_derived'})
_CONTRIBUTOR_ALLOWLIST = (
    _DRIVE_KEYS | _AFFECT_FACTORS | _BOND_FACTORS | _LONGING_FACTORS
)

_SHADOW_PROVIDER = 'api_relay'
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
8. contributors 只能使用真实状态因子：八 Drive 名、affect.*、bond.*、longing/longing_derived。
9. source 必须是 "planner_shadow"；shadow_only 必须是 true。

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


def new_decision_attempt_id() -> str:
    """Gateway-owned attempt identity shared by Shadow + production outcome."""
    return str(uuid.uuid4())


def mark_production_attempt_outcome(
    *,
    wake_run_id: str,
    decision_attempt_id: str,
    status: str,
    action: Optional[str] = None,
    reason: Optional[str] = None,
    provider: Optional[str] = None,
) -> None:
    """Append production attempt outcome marker for C2 pairing / orphan rules.

    Accepted comparison evidence requires:
      shadow_decision (shadow_status=valid)
      + production_outcome (status=success)
      with the same decision_attempt_id.

    Failed / missing production outcomes leave Shadow records as pending/orphan.
    """
    st = str(status or '').strip() or 'failed'
    if st not in ('success', 'failed'):
        st = 'failed'
    append_shadow_observation({
        'record_kind': 'production_outcome',
        'wake_run_id': wake_run_id,
        'decision_attempt_id': decision_attempt_id,
        'production_status': st,
        'action': action,
        'reason': reason,
        'provider': provider,
        'captured_at': _now_str(),
        'shadow_only': True,
        'authoritative': False,
    })


def comparison_evidence_status(
    *,
    wake_run_id: str,
    decision_attempt_id: str,
    observations: Optional[list] = None,
) -> str:
    """Return accepted | orphan | pending for one attempt (test/helper)."""
    rows = observations
    if rows is None:
        path = observation_path()
        rows = []
        if os.path.exists(path):
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
    shadow_ok = False
    prod = None
    for row in rows:
        if str(row.get('decision_attempt_id') or '') != str(decision_attempt_id):
            continue
        if str(row.get('wake_run_id') or '') != str(wake_run_id):
            continue
        kind = row.get('record_kind') or 'shadow_decision'
        if kind == 'shadow_decision' and row.get('shadow_status') == 'valid':
            shadow_ok = True
        if kind == 'production_outcome':
            prod = row.get('production_status')
    if not shadow_ok:
        return 'pending'
    if prod == 'success':
        return 'accepted'
    if prod == 'failed':
        return 'orphan'
    return 'pending'


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
        'ritual_type': getattr(skill, 'ritual_type', '') or '',
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


def classify_production_outcome(
    exec_out: Optional[Mapping[str, Any]],
) -> tuple[str, str]:
    """Map executor return → (production_status, reason) for C2 markers.

    Accepted comparison evidence requires a real committed production attempt:
    ``delivered=True`` and ``settled=True``. Soft-window gate blocks return
    without raising and must become failed/orphan — never success.
    """
    if not isinstance(exec_out, Mapping):
        return 'failed', 'executor_result_missing'
    delivered = bool(exec_out.get('delivered'))
    settled = bool(exec_out.get('settled'))
    if delivered and settled:
        return 'success', ''
    gate = str(exec_out.get('gate_reason') or '').strip()
    settle_status = exec_out.get('settle_status')
    if not delivered:
        return 'failed', f'gate_blocked:{gate or "unknown"}'
    return 'failed', f'not_settled:{settle_status or gate or "unknown"}'


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
    contrib_out: list[str] = []
    for item in contributors:
        token = str(item or '').strip()
        if not token:
            return 'invalid', {
                'error': 'contributor_empty',
                'raw': dict(raw),
            }
        if token not in _CONTRIBUTOR_ALLOWLIST:
            return 'invalid', {
                'error': 'contributor_not_state_factor',
                'illegal_contributor': token,
                'raw': dict(raw),
            }
        contrib_out.append(token)

    reason_codes = raw.get('reason_codes') or []
    if not isinstance(reason_codes, list):
        return 'invalid', {'error': 'reason_codes_not_list', 'raw': dict(raw)}
    reasons = [str(x) for x in reason_codes]

    try:
        blocked = _parse_bool(raw.get('blocked'), default=False)
    except ValueError:
        return 'invalid', {'error': 'blocked_not_bool', 'raw': dict(raw)}

    if blocked and action != 'none':
        return 'invalid', {
            'error': 'blocked_requires_none_action',
            'raw': dict(raw),
        }

    # Active (non-none) proposals require Decision-time primary_drive.
    # Never infer primary from action_candidate.
    if action != 'none' and primary is None:
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


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    """Strict-ish bool parse — string 'false' must not become True."""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ('true', '1', 'yes'):
            return True
        if s in ('false', '0', 'no', ''):
            return False
    raise ValueError('blocked_not_bool')


def resolve_shadow_relay_model_identity() -> str:
    """Model identity of the isolated Shadow Relay thinker (not production K)."""
    from relay.manager import RelayManager
    mgr = RelayManager()
    return str(mgr.model or '').strip() or 'relay:unset'


def invoke_shadow_planner_relay(
    *,
    user_payload: str,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
) -> dict:
    """Isolated Relay one-shot. Never touches CC Wake resident / global relay.

    Returns ``{text, provider, model_identity}`` so observations attribute the
    Decision to the Shadow thinker (always api_relay), not production Wake.
    """
    from relay.manager import RelayManager

    # Fresh instance — do not mutate production relay singleton.
    mgr = RelayManager()
    # Anthropic Messages metadata allows only user_id. Shadow observation
    # fields (source / shadow_only) stay in Decision/JSONL — never in HTTP.
    payload = {
        'max_tokens': _SHADOW_MAX_TOKENS,
        'system': _SHADOW_SYSTEM,
        'messages': [{'role': 'user', 'content': user_payload}],
    }
    # Explicitly no tools — Shadow must not execute Wake tools.
    result = mgr.call(payload, timeout=timeout_sec)
    text = mgr.extract_text(result)
    return {
        'text': text,
        'provider': _SHADOW_PROVIDER,
        'model_identity': str(mgr.model or '').strip() or 'relay:unset',
    }


def _coerce_invoke_result(result: Any) -> tuple[str, str, str]:
    """Normalize invoke_fn return → (text, shadow_provider, shadow_model)."""
    if isinstance(result, dict):
        text = result.get('text')
        if text is None:
            text = result.get('raw_text')
        provider = str(result.get('provider') or _SHADOW_PROVIDER).strip()
        model = str(result.get('model_identity') or '').strip()
        if not model:
            model = resolve_shadow_relay_model_identity()
        return str(text or ''), provider or _SHADOW_PROVIDER, model
    # Plain-text invoke_fn (tests) — Shadow thinker is still Relay.
    return (
        str(result or ''),
        _SHADOW_PROVIDER,
        resolve_shadow_relay_model_identity(),
    )


def run_shadow_attempt(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    decision_attempt_id: str,
    legacy_provenance: Optional[Mapping[str, Any]] = None,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
    invoke_fn=None,
) -> dict:
    """Synchronous Shadow attempt → observation record (caller may thread it).

    ``decision_attempt_id`` must be created by gateway before dispatch and
    shared with the production outcome marker (C2).
    """
    attempt_id = str(decision_attempt_id or '').strip()
    if not attempt_id:
        attempt_id = new_decision_attempt_id()
    planner_decision_id = attempt_id
    captured_at = _now_str()
    # provider/model_identity = who generated the Shadow Decision (Relay).
    # Production Wake identity stays under capability.* for pairing context.
    base = {
        'record_kind': 'shadow_decision',
        'wake_run_id': wake_run_id,
        'decision_attempt_id': attempt_id,
        'planner_decision_id': planner_decision_id,
        'state_version': int(planner_view.state_version),
        'observed_at': planner_view.observed_at,
        'captured_at': captured_at,
        'provider': _SHADOW_PROVIDER,
        'model_identity': 'relay:pending',
        'legacy_provenance': dict(legacy_provenance or {}),
        'capability': {
            'production_provider': skill_view.provider,
            'production_model_identity': skill_view.model_identity,
            'resolved_action_capability': list(skill_view.resolved_action_capability),
            'tool_allowlist': list(skill_view.tool_allowlist),
        },
        'comparison_status': 'pending',
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
        text, shadow_provider, shadow_model = _coerce_invoke_result(
            invoker(user_payload=user_payload, timeout_sec=timeout_sec),
        )
        base['provider'] = shadow_provider
        base['model_identity'] = shadow_model
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
        # Best-effort: still attribute thinker as Relay if invoke never ran.
        if base.get('model_identity') == 'relay:pending':
            try:
                base['model_identity'] = resolve_shadow_relay_model_identity()
            except Exception:
                base['model_identity'] = 'relay:unset'
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


def run_authoritative_planner_decision(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    decision_attempt_id: str,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
    invoke_fn=None,
) -> tuple[str, dict]:
    """Synchronous production Planner Decision for B2 consumer.

    Does not read or write JSONL observations. Never blocks on Shadow threads.
    Returns (status, decision_or_error) where status is valid | invalid | error.
    """
    attempt_id = str(decision_attempt_id or '').strip()
    if not attempt_id:
        attempt_id = new_decision_attempt_id()
    planner_decision_id = attempt_id
    captured_at = _now_str()

    if (
        str(planner_view.wake_run_id or '') != str(wake_run_id)
        or str(skill_view.wake_run_id or '') != str(wake_run_id)
    ):
        return 'invalid', {'error': 'wake_run_id_pairing'}

    try:
        user_payload = build_shadow_user_payload(
            planner_view=planner_view,
            skill_view=skill_view,
            wake_run_id=wake_run_id,
        )
        invoker = invoke_fn or invoke_shadow_planner_relay
        text, _shadow_provider, _shadow_model = _coerce_invoke_result(
            invoker(user_payload=user_payload, timeout_sec=timeout_sec),
        )
        parsed = _extract_json_object(text)
        if not parsed:
            return 'invalid', {'error': 'parse_failure'}
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
            return status, decision
        decision = dict(decision)
        decision['source'] = 'planner_authority'
        decision['shadow_only'] = False
        decision['authoritative'] = True
        return 'valid', decision
    except Exception as exc:
        return 'error', {'error': type(exc).__name__, 'detail': str(exc)[:500]}


def dispatch_planner_shadow(
    *,
    planner_view: Any,
    skill_view: Any,
    wake_run_id: str,
    decision_attempt_id: str,
    legacy_provenance: Optional[Mapping[str, Any]] = None,
    timeout_sec: float = _SHADOW_TIMEOUT_SEC,
) -> None:
    """Freeze/dispatch Shadow on a daemon thread. Never blocks production."""

    attempt_id = str(decision_attempt_id or '').strip()

    def _worker():
        try:
            run_shadow_attempt(
                planner_view=planner_view,
                skill_view=skill_view,
                wake_run_id=wake_run_id,
                decision_attempt_id=attempt_id,
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
                'record_kind': 'shadow_decision',
                'wake_run_id': wake_run_id,
                'decision_attempt_id': attempt_id,
                'shadow_status': 'error',
                'error_category': 'dispatch_failed',
                'error': str(exc)[:500],
                'comparison_status': 'pending',
                'shadow_only': True,
                'authoritative': False,
            })
        except Exception:
            pass
