"""Stage 1: State delta / re-anchor for Context Lean (CC resident path).

Raw state snapshots in commit_meta remain complete. Cumulative send snapshots
track exactly what the model was told — the same bytes as state_text.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from chat.context_budget import (
    build_state_send_payload,
    merge_cumulative_state_send,
    normalize_state_dict,
)
from chat.system_builder import format_state_snapshot, merge_partial_lean_lights

_LOG = logging.getLogger('hayagarden.context_lean_state')

STATE_SCHEMA_VERSION = 1
# Re-anchor before CC_MAX_RESIDENT_TURNS (default 30); P-CONTEXT-OBS showed
# stable generation across cold→hot turns 1–3 — periodic anchor limits drift.
REANCHOR_TURN_INTERVAL = 24
# ~3–4 cumulative delta blocks before forced full anchor.
REANCHOR_DELTA_CHAR_THRESHOLD = 3500

_STATE_CONTEXT_MODES = frozenset({'full_anchor', 'delta', 'omitted', 'fallback'})


@dataclass(frozen=True)
class StateContextResult:
    state_text: str
    state_mode: str
    state_context_mode: str
    raw_state: dict[str, str]
    send_payload: dict[str, str]
    commit_meta_extras: dict[str, Any] = field(default_factory=dict)
    observation: dict[str, Any] = field(default_factory=dict)
    used_lean: bool = False
    fallback_reason: Optional[str] = None
    reanchor_reason: Optional[str] = None


def compute_state_version(state: Optional[Mapping[str, Any]]) -> str:
    normalized = normalize_state_dict(state)
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def evaluate_reanchor_reason(
    *,
    lean_on: bool,
    prev_lean_success: bool,
    is_cold: bool,
    resident_generation: int,
    last_anchor_generation: int,
    last_schema_version: Optional[int],
    turns_since_anchor: int,
    delta_chars_since_anchor: int,
    cumulative_send_nonempty: bool,
) -> Optional[str]:
    if not lean_on:
        return None
    if is_cold:
        return 'cold_start'
    if not prev_lean_success:
        return 'lean_first_enable'
    if resident_generation != last_anchor_generation:
        return 'resident_generation_change'
    if last_schema_version != STATE_SCHEMA_VERSION:
        return 'schema_version_change'
    if not cumulative_send_nonempty:
        return 'anchor_missing'
    if turns_since_anchor >= REANCHOR_TURN_INTERVAL:
        return 'reanchor_turn_interval'
    if delta_chars_since_anchor >= REANCHOR_DELTA_CHAR_THRESHOLD:
        return 'delta_accumulation_threshold'
    return None


def _structured_value_line(key: str, value: str) -> str:
    """Pure serialization — must not alter field semantics."""
    value = str(value or '').strip()
    if not value:
        return f'{key}: cleared'
    return f'{key}: {value}'


def format_structured_state_anchor(
    effective_send_payload: Mapping[str, str],
    *,
    state_version: str,
) -> str:
    payload = normalize_state_dict(effective_send_payload)
    if not payload:
        return ''
    lines = [
        '【当前状态·锚点】',
        f'schema_version={STATE_SCHEMA_VERSION} state_version={state_version}',
    ]
    order = (
        'time_bucket', 'emotion', 'drive', 'lights', 'pocket',
        'todos', 'ledger', 'reminders', 'recent_activity',
    )
    seen: set[str] = set()
    for key in order:
        if key in payload and payload[key]:
            lines.append(_structured_value_line(key, payload[key]))
            seen.add(key)
    for key, value in payload.items():
        if key not in seen and value:
            lines.append(_structured_value_line(key, value))
    return '\n'.join(lines)


def format_structured_state_delta(
    cumulative_before: Mapping[str, str],
    effective_send_payload: Mapping[str, str],
    *,
    anchor_version: str,
    previous_version: str,
    current_version: str,
) -> str:
    payload = normalize_state_dict(effective_send_payload)
    if not payload:
        return ''
    cumulative = normalize_state_dict(cumulative_before)
    changed = sorted(payload.keys())
    lines = [
        '【状态更新·增量】',
        (
            f'schema_version={STATE_SCHEMA_VERSION} '
            f'anchor_version={anchor_version} '
            f'previous_version={previous_version} '
            f'current_version={current_version} '
            f'changed={",".join(changed)}'
        ),
    ]
    for key in changed:
        after = payload[key]
        before = cumulative.get(key, '')
        if before and not after:
            lines.append(f'{key}: cleared')
        elif after:
            lines.append(_structured_value_line(key, after))
    return '\n'.join(lines)


def format_lean_state_for_send(
    cumulative_before: Optional[Mapping[str, Any]],
    effective_send_payload: Mapping[str, Any],
    *,
    is_cold: bool,
    anchor_version: str,
    previous_version: str,
    current_version: str,
) -> tuple[str, str, str]:
    """Return (text, legacy_state_mode, state_context_mode)."""
    payload = normalize_state_dict(effective_send_payload)
    if is_cold:
        text = format_structured_state_anchor(payload, state_version=current_version)
        if not text:
            return '', 'none', 'omitted'
        return text, 'snapshot', 'full_anchor'

    if not payload:
        return '', 'none', 'omitted'

    text = format_structured_state_delta(
        cumulative_before or {},
        payload,
        anchor_version=anchor_version,
        previous_version=previous_version,
        current_version=current_version,
    )
    if not text:
        return '', 'none', 'omitted'
    return text, 'delta', 'delta'


def build_state_lean_observation(
    *,
    enabled: bool,
    state_context_mode: str,
    state_text: str,
    state_version: str,
    anchor_version: str,
    previous_version: str,
    changed_field_count: int,
    reanchor_reason: Optional[str],
    fallback_reason: Optional[str],
    resident_generation: int,
    lights_source_meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

    mode = state_context_mode if state_context_mode in _STATE_CONTEXT_MODES else 'omitted'
    obs: dict[str, Any] = {
        'context_lean_state_enabled': bool(enabled),
        'state_context_mode': mode,
        'state_version': state_version,
        'anchor_version': anchor_version,
        'previous_version': previous_version,
        'changed_field_count': int(changed_field_count),
        'state_context_chars': len(state_text or ''),
        'state_context_estimated_tokens': estimate_tokens_heuristic_cjk1_ascii4_v1(state_text or ''),
        'reanchor_reason': reanchor_reason,
        'fallback_reason': fallback_reason,
        'state_schema_version': STATE_SCHEMA_VERSION,
        'resident_generation': int(resident_generation),
        'observation_version': 3,
    }
    if lights_source_meta:
        for key in (
            'lights_source_status',
            'lights_main_available',
            'lights_bedside_available',
            'lights_last_success_at',
        ):
            if key in lights_source_meta:
                obs[key] = lights_source_meta[key]
    return obs


def _parse_lights_source_meta(raw_state: Mapping[str, Any]) -> dict[str, Any]:
    raw = (raw_state or {}).get('_lights_source')
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _apply_lights_source_to_raw(
    raw: Mapping[str, str],
    lights_meta: Mapping[str, Any],
    known_state: Mapping[str, str],
) -> dict[str, str]:
    """Normalize lean lights field before diff/anchor (device-level source semantics only)."""
    out = dict(normalize_state_dict(raw))
    known_lights = normalize_state_dict(known_state).get('lights', '')
    status = lights_meta.get('lights_source_status')

    if status == 'partial':
        merged = merge_partial_lean_lights(
            out.get('lights', ''),
            known_lights,
            main_available=bool(lights_meta.get('lights_main_available')),
            bedside_available=bool(lights_meta.get('lights_bedside_available')),
        )
        if merged:
            out['lights'] = merged
        else:
            out.pop('lights', None)
    elif status == 'unavailable':
        if known_lights:
            out['lights'] = known_lights
        else:
            out.pop('lights', None)
    return out


def _last_raw_for_lights_diff(
    last_raw: Mapping[str, Any],
    known_state: Mapping[str, str],
) -> dict[str, str]:
    """Backfill last-known lights when a prior outage left a hole in last_state_snapshot."""
    last = normalize_state_dict(last_raw)
    known_lights = normalize_state_dict(known_state).get('lights', '')
    if known_lights and 'lights' not in last:
        patched = dict(last)
        patched['lights'] = known_lights
        return patched
    return dict(last)


def _legacy_state_context(
    *,
    raw_state: Mapping[str, str],
    is_cold: bool,
    last_state_snapshot: Optional[Mapping[str, Any]],
    resident_generation: int = 0,
    lights_source_meta: Optional[Mapping[str, Any]] = None,
) -> StateContextResult:
    from chat.system_builder import format_state_diff

    if is_cold:
        state_text = format_state_snapshot(raw_state)
        state_mode = 'snapshot' if state_text else 'none'
    else:
        state_text = format_state_diff(last_state_snapshot or {}, raw_state)
        state_mode = 'delta' if state_text else 'none'
    context_mode = {
        'snapshot': 'full_anchor',
        'delta': 'delta',
        'none': 'omitted',
    }.get(state_mode, 'omitted')
    return StateContextResult(
        state_text=state_text,
        state_mode=state_mode,
        state_context_mode=context_mode,
        raw_state=dict(normalize_state_dict(raw_state)),
        send_payload={},
        commit_meta_extras={'lean_state_active': False},
        observation=build_state_lean_observation(
            enabled=False,
            state_context_mode=context_mode,
            state_text=state_text,
            state_version=compute_state_version(raw_state),
            anchor_version='',
            previous_version='',
            changed_field_count=0,
            reanchor_reason=None,
            fallback_reason=None,
            resident_generation=resident_generation,
            lights_source_meta=lights_source_meta,
        ),
        used_lean=False,
    )


def assemble_legacy_full_fallback(
    *,
    legacy_raw_state: Mapping[str, str],
    fallback_reason: str,
    resident,
) -> StateContextResult:
    """True legacy fallback: full snapshot from lean=False raw state."""
    lights_meta = _parse_lights_source_meta(legacy_raw_state)
    raw = normalize_state_dict(legacy_raw_state)
    state_text = format_state_snapshot(raw)
    state_mode = 'snapshot' if state_text else 'none'
    version = compute_state_version(raw)
    observation = build_state_lean_observation(
        enabled=True,
        state_context_mode='fallback',
        state_text=state_text,
        state_version=version,
        anchor_version='',
        previous_version='',
        changed_field_count=0,
        reanchor_reason=None,
        fallback_reason=fallback_reason,
        resident_generation=int(getattr(resident, 'generation', 0) or 0),
        lights_source_meta=lights_meta,
    )
    return StateContextResult(
        state_text=state_text,
        state_mode=state_mode,
        state_context_mode='fallback',
        raw_state=dict(raw),
        send_payload={},
        commit_meta_extras={
            'lean_state_active': False,
            'state_lean_fallback': True,
        },
        observation=observation,
        used_lean=False,
        fallback_reason=fallback_reason,
    )


def assemble_cc_state_context(
    *,
    raw_state: Mapping[str, str],
    is_cold: bool,
    user_text: str,
    resident,
    lean_on: bool,
) -> StateContextResult:
    """Build provider-visible state block for one CC resident turn."""
    lights_meta = _parse_lights_source_meta(raw_state)
    raw = normalize_state_dict(raw_state)
    generation = int(getattr(resident, 'generation', 0) or 0)

    if not lean_on:
        return _legacy_state_context(
            raw_state=raw,
            is_cold=is_cold,
            last_state_snapshot=getattr(resident, 'last_state_snapshot', None),
            resident_generation=generation,
            lights_source_meta=lights_meta,
        )

    prev_lean = bool(getattr(resident, 'last_successful_lean_state', False))
    cumulative = getattr(resident, 'last_state_send_snapshot', None) or {}
    cumulative_nonempty = bool(normalize_state_dict(cumulative))
    reanchor_reason = evaluate_reanchor_reason(
        lean_on=True,
        prev_lean_success=prev_lean,
        is_cold=is_cold,
        resident_generation=generation,
        last_anchor_generation=int(getattr(resident, 'last_state_anchor_generation', 0) or 0),
        last_schema_version=getattr(resident, 'last_state_schema_version', None),
        turns_since_anchor=int(getattr(resident, 'turns_since_state_anchor', 0) or 0),
        delta_chars_since_anchor=int(getattr(resident, 'state_delta_chars_since_anchor', 0) or 0),
        cumulative_send_nonempty=cumulative_nonempty,
    )
    needs_reanchor = reanchor_reason is not None
    known_state_before_reanchor = cumulative
    last_raw = (
        {}
        if needs_reanchor
        else getattr(resident, 'last_state_snapshot', None) or {}
    )
    cumulative_before = {} if needs_reanchor else cumulative
    send_is_cold = bool(needs_reanchor or is_cold)

    raw = _apply_lights_source_to_raw(raw, lights_meta, known_state_before_reanchor)
    last_raw = _last_raw_for_lights_diff(last_raw, known_state_before_reanchor)

    effective_send_payload = build_state_send_payload(
        last_raw,
        raw,
        user_text=user_text or '',
        is_cold=send_is_cold,
    )
    if send_is_cold:
        effective_state_after = normalize_state_dict(effective_send_payload)
    else:
        effective_state_after = merge_cumulative_state_send(
            cumulative_before,
            effective_send_payload,
        )
    current_version = compute_state_version(effective_state_after)
    if send_is_cold:
        anchor_version = current_version
        previous_version = ''
    else:
        anchor_version = (
            getattr(resident, 'last_state_anchor_version', None)
            or compute_state_version(cumulative_before)
        )
        previous_version = compute_state_version(cumulative_before)
    state_text, state_mode, context_mode = format_lean_state_for_send(
        cumulative_before,
        effective_send_payload,
        is_cold=send_is_cold,
        anchor_version=anchor_version,
        previous_version=previous_version,
        current_version=current_version,
    )
    changed_count = len(effective_send_payload)

    commit_extras = {
        'lean_state_active': True,
        'state_send_snapshot': dict(effective_send_payload),
        'state_schema_version': STATE_SCHEMA_VERSION,
        'state_version': current_version,
        'state_anchor_version': anchor_version,
        'state_previous_version': previous_version,
        'state_context_chars': len(state_text or ''),
    }
    if needs_reanchor:
        commit_extras['lean_state_reanchor'] = True
        commit_extras['reanchor_reason'] = reanchor_reason

    observation = build_state_lean_observation(
        enabled=True,
        state_context_mode=context_mode,
        state_text=state_text,
        state_version=current_version,
        anchor_version=anchor_version,
        previous_version=previous_version,
        changed_field_count=changed_count,
        reanchor_reason=reanchor_reason if needs_reanchor else None,
        fallback_reason=None,
        resident_generation=generation,
        lights_source_meta=lights_meta,
    )
    return StateContextResult(
        state_text=state_text,
        state_mode=state_mode,
        state_context_mode=context_mode,
        raw_state=dict(raw),
        send_payload=dict(effective_send_payload),
        commit_meta_extras=commit_extras,
        observation=observation,
        used_lean=True,
        reanchor_reason=reanchor_reason if needs_reanchor else None,
    )


def assemble_cc_state_for_resident_turn(
    *,
    is_cold: bool,
    user_text: str,
    resident,
):
    """Build CC state context with lean fail-safe to legacy full snapshot."""
    from chat import context_lean as _context_lean
    from chat.system_builder import build_cc_state

    lean_state_on = _context_lean.lean_state_enabled()
    try:
        raw_state = build_cc_state(lean=lean_state_on)
        state_ctx = assemble_cc_state_context(
            raw_state=raw_state,
            is_cold=is_cold,
            user_text=user_text or '',
            resident=resident,
            lean_on=lean_state_on,
        )
    except Exception as exc:
        legacy_raw_state = build_cc_state(lean=False)
        raw_state = legacy_raw_state
        state_ctx = assemble_legacy_full_fallback(
            legacy_raw_state=legacy_raw_state,
            fallback_reason=type(exc).__name__,
            resident=resident,
        )
    return raw_state, state_ctx
