"""Stage 1: State delta / re-anchor for Context Lean (CC resident path).

Raw state snapshots remain complete in commit_meta. Provider-visible send
payloads may be full anchors, structured deltas, or omitted when unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from chat.context_budget import (
    build_state_send_payload,
    merge_cumulative_state_send,
    normalize_state_dict,
)
from chat import context_lean as _context_lean
from chat.system_builder import format_state_diff, format_state_snapshot

_LOG = logging.getLogger('hayagarden.context_lean_state')

STATE_SCHEMA_VERSION = 1
# Re-anchor before CC_MAX_RESIDENT_TURNS (default 30); P-CONTEXT-OBS showed
# stable generation across cold→hot turns 1–3 — periodic anchor limits drift.
REANCHOR_TURN_INTERVAL = 24
# ~3–4 cumulative delta blocks before forced full anchor (observed state blocks
# often 800–1200 estimated tokens each on hot turns with changes).
REANCHOR_DELTA_CHAR_THRESHOLD = 3500

_STYLE_INSTRUCTION_RE = re.compile(
    r'话少|安静等待|简短|克制|语气|文风|更主动|更冷淡|因为她刚才',
    re.I,
)

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


def contains_style_instruction(text: str) -> bool:
    return bool(_STYLE_INSTRUCTION_RE.search(text or ''))


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
    value = str(value or '').strip()
    if not value:
        return f'{key}: cleared'
    if contains_style_instruction(value):
        _LOG.warning('state lean stripped style-like content in key=%s', key)
        value = re.sub(_STYLE_INSTRUCTION_RE, '', value).strip(' ，。;')
    return f'{key}: {value}'


def format_structured_state_anchor(
    send_delta: Mapping[str, str],
    *,
    state_version: str,
) -> str:
    send_delta = normalize_state_dict(send_delta)
    if not send_delta:
        return ''
    lines = [
        '【当前状态·锚点】',
        f'schema_version={STATE_SCHEMA_VERSION} state_version={state_version}',
    ]
    order = (
        'time_bucket', 'emotion', 'drive', 'lights', 'pocket',
        'todos', 'ledger', 'reminders', 'recent_activity',
    )
    seen = set()
    for key in order:
        if key in send_delta and send_delta[key]:
            lines.append(_structured_value_line(key, send_delta[key]))
            seen.add(key)
    for key, value in send_delta.items():
        if key not in seen and value:
            lines.append(_structured_value_line(key, value))
    return '\n'.join(lines)


def format_structured_state_delta(
    cumulative_before: Mapping[str, str],
    send_delta: Mapping[str, str],
    *,
    prev_version: str,
    curr_version: str,
) -> str:
    send_delta = normalize_state_dict(send_delta)
    if not send_delta:
        return ''
    cumulative = normalize_state_dict(cumulative_before)
    changed = sorted(send_delta.keys())
    lines = [
        '【状态更新·增量】',
        (
            f'schema_version={STATE_SCHEMA_VERSION} '
            f'anchor_version={prev_version} curr_version={curr_version} '
            f'changed={",".join(changed)}'
        ),
    ]
    for key in changed:
        after = send_delta[key]
        before = cumulative.get(key, '')
        if before and not after:
            lines.append(f'{key}: cleared')
        elif after:
            lines.append(_structured_value_line(key, after))
    return '\n'.join(lines)


def format_lean_state_for_send(
    cumulative_before: Optional[Mapping[str, Any]],
    send_delta: Mapping[str, Any],
    *,
    is_cold: bool,
    prev_version: str,
    curr_version: str,
) -> tuple[str, str, str]:
    """Return (text, legacy_state_mode, state_context_mode)."""
    send_delta = normalize_state_dict(send_delta)
    if is_cold:
        text = format_structured_state_anchor(send_delta, state_version=curr_version)
        if not text:
            return '', 'none', 'omitted'
        return text, 'snapshot', 'full_anchor'

    cumulative = normalize_state_dict(cumulative_before)
    if not send_delta:
        return '', 'none', 'omitted'

    text = format_structured_state_delta(
        cumulative,
        send_delta,
        prev_version=prev_version or compute_state_version(cumulative),
        curr_version=curr_version,
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
    changed_field_count: int,
    reanchor_reason: Optional[str],
    fallback_reason: Optional[str],
    resident_generation: int,
) -> dict[str, Any]:
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

    mode = state_context_mode if state_context_mode in _STATE_CONTEXT_MODES else 'omitted'
    return {
        'context_lean_state_enabled': bool(enabled),
        'state_context_mode': mode,
        'state_version': state_version,
        'anchor_version': anchor_version,
        'changed_field_count': int(changed_field_count),
        'state_context_chars': len(state_text or ''),
        'state_context_estimated_tokens': estimate_tokens_heuristic_cjk1_ascii4_v1(state_text or ''),
        'reanchor_reason': reanchor_reason,
        'fallback_reason': fallback_reason,
        'state_schema_version': STATE_SCHEMA_VERSION,
        'resident_generation': int(resident_generation),
        'observation_version': 3,
    }


def _legacy_state_context(
    *,
    raw_state: Mapping[str, str],
    is_cold: bool,
    last_state_snapshot: Optional[Mapping[str, Any]],
) -> StateContextResult:
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
        raw_state=dict(raw_state),
        send_payload={},
        commit_meta_extras={'lean_state_active': False},
        observation=build_state_lean_observation(
            enabled=False,
            state_context_mode=context_mode,
            state_text=state_text,
            state_version=compute_state_version(raw_state),
            anchor_version='',
            changed_field_count=0,
            reanchor_reason=None,
            fallback_reason=None,
            resident_generation=0,
        ),
        used_lean=False,
    )


def assemble_cc_state_context(
    *,
    raw_state: Mapping[str, str],
    is_cold: bool,
    user_text: str,
    resident,
) -> StateContextResult:
    """Build provider-visible state block for one CC resident turn."""
    raw = normalize_state_dict(raw_state)
    if not _context_lean.lean_state_enabled():
        return _legacy_state_context(
            raw_state=raw,
            is_cold=is_cold,
            last_state_snapshot=getattr(resident, 'last_state_snapshot', None),
        )

    try:
        prev_lean = bool(getattr(resident, 'last_successful_lean_state', False))
        cumulative = getattr(resident, 'last_state_send_snapshot', None) or {}
        cumulative_nonempty = bool(normalize_state_dict(cumulative))
        reanchor_reason = evaluate_reanchor_reason(
            lean_on=True,
            prev_lean_success=prev_lean,
            is_cold=is_cold,
            resident_generation=int(getattr(resident, 'generation', 0) or 0),
            last_anchor_generation=int(getattr(resident, 'last_state_anchor_generation', 0) or 0),
            last_schema_version=getattr(resident, 'last_state_schema_version', None),
            turns_since_anchor=int(getattr(resident, 'turns_since_state_anchor', 0) or 0),
            delta_chars_since_anchor=int(getattr(resident, 'state_delta_chars_since_anchor', 0) or 0),
            cumulative_send_nonempty=cumulative_nonempty,
        )
        needs_reanchor = reanchor_reason is not None
        last_raw = (
            {}
            if needs_reanchor
            else getattr(resident, 'last_state_snapshot', None) or {}
        )
        cumulative_before = {} if needs_reanchor else cumulative
        send_is_cold = bool(needs_reanchor or is_cold)

        send_payload = build_state_send_payload(
            last_raw,
            raw,
            user_text=user_text or '',
            is_cold=send_is_cold,
        )
        prev_version = (
            getattr(resident, 'last_state_anchor_version', None)
            or compute_state_version(cumulative_before)
        )
        curr_version = compute_state_version(raw)
        state_text, state_mode, context_mode = format_lean_state_for_send(
            cumulative_before,
            send_payload,
            is_cold=send_is_cold,
            prev_version=prev_version,
            curr_version=curr_version,
        )
        changed_count = len([k for k, v in send_payload.items() if v != ''])

        commit_extras = {
            'lean_state_active': True,
            'state_send_snapshot': send_payload,
            'state_schema_version': STATE_SCHEMA_VERSION,
            'state_version': curr_version,
            'state_anchor_version': prev_version if not send_is_cold else curr_version,
        }
        if needs_reanchor:
            commit_extras['lean_state_reanchor'] = True
            commit_extras['reanchor_reason'] = reanchor_reason
        commit_extras['state_context_chars'] = len(state_text or '')

        observation = build_state_lean_observation(
            enabled=True,
            state_context_mode=context_mode,
            state_text=state_text,
            state_version=curr_version,
            anchor_version=prev_version if not send_is_cold else curr_version,
            changed_field_count=changed_count,
            reanchor_reason=reanchor_reason if needs_reanchor else None,
            fallback_reason=None,
            resident_generation=int(getattr(resident, 'generation', 0) or 0),
        )
        return StateContextResult(
            state_text=state_text,
            state_mode=state_mode,
            state_context_mode=context_mode,
            raw_state=dict(raw),
            send_payload=dict(send_payload),
            commit_meta_extras=commit_extras,
            observation=observation,
            used_lean=True,
            reanchor_reason=reanchor_reason if needs_reanchor else None,
        )
    except Exception as exc:
        _LOG.exception('state lean failed; falling back to legacy path')
        legacy = _legacy_state_context(
            raw_state=raw,
            is_cold=is_cold,
            last_state_snapshot=getattr(resident, 'last_state_snapshot', None),
        )
        obs = dict(legacy.observation)
        obs.update(build_state_lean_observation(
            enabled=True,
            state_context_mode='fallback',
            state_text=legacy.state_text,
            state_version=compute_state_version(raw),
            anchor_version='',
            changed_field_count=0,
            reanchor_reason=None,
            fallback_reason=f'{type(exc).__name__}',
            resident_generation=int(getattr(resident, 'generation', 0) or 0),
        ))
        legacy = StateContextResult(
            state_text=legacy.state_text,
            state_mode=legacy.state_mode,
            state_context_mode='fallback',
            raw_state=legacy.raw_state,
            send_payload={},
            commit_meta_extras={'lean_state_active': False, 'state_lean_fallback': True},
            observation=obs,
            used_lean=False,
            fallback_reason=f'{type(exc).__name__}',
        )
        return legacy
