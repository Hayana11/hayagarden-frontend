"""B3-1 — Planner message takeover + Persona Renderer + existing Settlement.

Default OFF. Effective only when B2 consumer is also ON.
Owned ``message`` bypasses legacy Wake runner after Gate ALLOW.

UH-A1 adds a flag-off shared-resident Renderer path for ``normal`` Wake only.
It reuses an already-hot formal Chat resident and never changes system/tool
profile. Planner/Gate/Settlement contracts remain unchanged.
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
_UNIFIED_NORMAL_WAKE_CONFIG_KEY = 'UNIFIED_NORMAL_WAKE_ENABLED'
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

_SHARED_RENDERER_INSTRUCTIONS = (
    '【内部 normal Wake 表达轮｜不是用户消息】\n'
    '后台 Planner 与 Action Gate 已经完成决定；你只负责用当前这段主聊天里'
    '一直在使用的人格和上下文，把既定意图写成一条自然消息。\n'
    '不要重新决定要不要行动，不要改变 Action，不要调用工具，不要解释后台状态，'
    '不要输出 THOUGHTS/ACTION/Gate/Settlement。\n'
    '只输出一个 JSON object：{"rendered_content": "..."}。'
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


def unified_normal_wake_enabled() -> bool:
    """UH-A1 transport flag. Missing / invalid config fails OFF."""
    try:
        return config_store.get_bool(_UNIFIED_NORMAL_WAKE_CONFIG_KEY, default=False)
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


def build_shared_renderer_user_payload(renderer_input: RendererInput) -> str:
    """Minimal dynamic turn for the already-hot Chat resident.

    Persona, relationship dump, PlannerStateView, Drive/Affect/Thought and tool
    brochure are intentionally absent: the hot Chat resident already owns
    persona/conversation context, and raw private state must not become durable
    ordinary-chat transcript.
    """
    return _SHARED_RENDERER_INSTRUCTIONS + '\n\n' + json.dumps(
        {
            'selected_intent': renderer_input.selected_intent,
            'selected_action': renderer_input.selected_action,
            'content_target': renderer_input.content_target,
        },
        ensure_ascii=False,
    )


def _current_request_is_normal_wake() -> bool:
    """Only HTTP normal Wake is eligible in UH-A1 first cut."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return False
        data = request.get_json(silent=True) or {}
        return str(data.get('mode') or 'normal').strip() == 'normal'
    except Exception:
        return False


def _hot_chat_resident_ready(resident: Any, *, db_path: str) -> tuple[bool, str]:
    """Read-only same-worker door lock. Never takes ownership or respawns."""
    try:
        import cc_resident
        from chat import daily_context as dc
        from chat import daily_runtime as dr

        binding = dr.get_local_binding()
        if binding is None:
            return False, 'no_local_binding'

        alive = getattr(resident, '_alive', None)
        if not callable(alive) or not bool(alive()):
            return False, 'resident_not_hot'

        live_profile = str(getattr(resident, 'tool_profile', '') or '')
        if live_profile != cc_resident.TOOL_PROFILE_UH_A0:
            return False, 'tool_profile_not_uh_a0'
        if str(binding.tool_profile or '') != cc_resident.TOOL_PROFILE_UH_A0:
            return False, 'binding_tool_profile_not_uh_a0'

        # Read-only Chat contract probe. A resident can still be alive and
        # structurally bound while its durable rewrite epoch or another Chat
        # respawn condition already makes shared Wake unsafe.
        peek_respawn_reason = getattr(resident, 'peek_respawn_reason', None)
        if not callable(peek_respawn_reason):
            return False, 'stale_probe_unavailable'
        bound_system_text = getattr(resident, '_system_text', None)
        try:
            respawn_reason = peek_respawn_reason(
                bound_system_text,
                tool_profile=cc_resident.TOOL_PROFILE_UH_A0,
            )
        except Exception:
            return False, 'stale_probe_error'
        respawn_reason = str(respawn_reason or '').strip()
        if respawn_reason:
            return False, f'resident_stale:{respawn_reason}'

        live_generation = int(getattr(resident, 'generation', 0) or 0)
        if int(binding.process_generation or 0) != live_generation:
            return False, 'process_generation_mismatch'

        ctx = dc.get_daily_context_by_id(
            int(binding.context_id), db_path=db_path,
        )
        if not ctx:
            return False, 'context_missing'
        if ctx.get('closed_at') is not None:
            return False, 'context_closed'
        if int(ctx.get('context_epoch') or -1) != int(binding.context_epoch):
            return False, 'context_epoch_mismatch'
        if int(ctx.get('resident_generation') or -1) != int(binding.resident_generation):
            return False, 'resident_generation_mismatch'

        owner = dc.get_resident_owner(
            int(binding.context_id),
            int(binding.resident_generation),
            db_path=db_path,
        )
        if not owner:
            return False, 'owner_missing'
        if str(owner.get('worker_id') or '') != str(dr.WORKER_ID):
            return False, 'owner_other_worker'
        if str(owner.get('resident_key') or '') != str(binding.resident_key):
            return False, 'owner_key_mismatch'
        owner_generation = owner.get('process_generation')
        if (
            owner_generation is not None
            and int(owner_generation or 0) != live_generation
        ):
            return False, 'owner_process_generation_mismatch'
        return True, 'ok'
    except Exception as exc:
        _LOG.warning('UH-A1 hot resident check failed: %s', exc)
        return False, 'eligibility_error'


class _SharedPreTurnUnavailable(RuntimeError):
    """Shared route became unavailable before stdin was sent."""


class UnifiedNormalWakeSharedUnavailable(RuntimeError):
    """Unified normal Wake must skip when no safe shared hot route exists."""


def invoke_renderer_cc_hot(
    *,
    renderer_input: RendererInput,
    resident: Any,
    turn_lease: Mapping[str, Any],
    final_probe: Optional[Callable[[], tuple[bool, str]]] = None,
    on_guarded_probe_pass: Optional[Callable[[], None]] = None,
) -> dict:
    """Run one Renderer turn on an already-hot UH-A0 resident.

    This function deliberately never calls ``ensure_alive`` and never mutates
    system text, allowed tools or tool profile. Tool events are drained to keep
    the resident stream coherent, then rejected for this Renderer contract.
    """
    from chat.cc_history_rewrite import guard_cc_generation

    text = ''
    usage: dict[str, Any] = {}
    candidate_cache_refresh_at = None
    candidate_cache_refresh_monotonic = None
    saw_done = False
    saw_tool = False
    payload = build_shared_renderer_user_payload(renderer_input)

    def guarded_events():
        # The guard is acquired before this generator is first advanced.
        # Therefore the final stale probe and the following stdin write share
        # one history-rewrite lock lifetime.
        if final_probe is not None:
            ready, reason = final_probe()
            if not ready:
                raise _SharedPreTurnUnavailable(reason)
        if on_guarded_probe_pass is not None:
            on_guarded_probe_pass()
        yield from resident.send_turn(
            payload,
            turn_lease=dict(turn_lease),
        )

    # Hold the existing cross-process shared lock for the final probe and the
    # complete stream consumption so no rewrite can land in between.
    for evt, raw in guard_cc_generation(guarded_events()):
        if evt in ('tool_use', 'tool_result'):
            saw_tool = True
        if evt != 'done':
            continue
        saw_done = True
        if isinstance(raw, tuple) and len(raw) >= 3:
            text = str(raw[0] or '')
            if isinstance(raw[2], dict):
                usage_obj = raw[2]
                candidate_cache_refresh_at = getattr(
                    usage_obj, '_candidate_cache_refresh_at', None,
                )
                candidate_cache_refresh_monotonic = getattr(
                    usage_obj, '_candidate_cache_refresh_monotonic', None,
                )
                usage = dict(usage_obj)
        else:
            text = str(raw or '')
    if not saw_done:
        raise RuntimeError('uh_a1_shared_renderer_missing_done')
    if saw_tool:
        raise RuntimeError('uh_a1_shared_renderer_tool_use')
    respawn_reason = str(usage.get('respawn_reason') or '').strip()
    if respawn_reason:
        raise RuntimeError(
            f'uh_a1_shared_renderer_respawn_reason:{respawn_reason}'
        )
    jsonl_finality = usage.get('jsonl_usage')
    if not isinstance(jsonl_finality, Mapping):
        raise RuntimeError('uh_a1_shared_renderer_jsonl_finality_missing')
    if jsonl_finality.get('stream_totals_match') is not True:
        raise RuntimeError('uh_a1_shared_renderer_jsonl_not_final')
    result = {
        'text': text,
        'provider': 'claude_code',
        'model_identity': str(
            getattr(resident, '_model_identity', None)
            or 'claude-code:shared-resident'
        ),
        'cache_info': usage,
        'transcript_finality': dict(jsonl_finality),
        'shared_resident': True,
    }
    if (
        candidate_cache_refresh_at is not None
        and candidate_cache_refresh_monotonic is not None
    ):
        result['_cache_refresh_candidate_at'] = candidate_cache_refresh_at
        result['_cache_refresh_candidate_monotonic'] = (
            candidate_cache_refresh_monotonic
        )
    return result


def _try_invoke_shared_renderer(
    *,
    renderer_input: RendererInput,
) -> Optional[dict]:
    """Return shared result, or raise an explicit skip for Unified normal Wake.

    Once the shared turn starts, errors propagate. We never auto-render a
    second answer after a possibly-partial shared resident turn.
    """
    is_unified_normal_wake = (
        unified_normal_wake_enabled() and _current_request_is_normal_wake()
    )
    if not is_unified_normal_wake:
        return None
    try:
        import gateway
        from chat.unified_heartbeat_a1 import (
            begin_shared_wake_delivery_fence,
            commit_shared_transcript_watermark,
            prepare_shared_transcript_watermark,
        )
        from tools.lease_signer import issue_turn_lease
    except Exception as exc:
        _LOG.warning('UH-A1 shared renderer import unavailable: %s', exc)
        raise UnifiedNormalWakeSharedUnavailable(
            'shared_import_unavailable'
        ) from exc

    resident = getattr(gateway, '_CC_RESIDENT', None)
    db_path = str(getattr(gateway, 'DB_PATH', '') or '')
    if resident is None or not db_path:
        raise UnifiedNormalWakeSharedUnavailable('resident_or_db_unavailable')

    ready, reason = _hot_chat_resident_ready(resident, db_path=db_path)
    if not ready:
        _LOG.info('UH-A1 shared renderer unavailable before turn: %s', reason)
        raise UnifiedNormalWakeSharedUnavailable(reason)

    acquired = False
    shared_started = False
    watermark = None
    delivery_fence = None
    result = None
    try:
        mode, _ = gateway._gen_acquire_or_wait(wait_timeout=0)
        if mode != 'own':
            raise UnifiedNormalWakeSharedUnavailable(
                'generation_lock_unavailable'
            )
        acquired = True

        # Close the race between the first check and generation lock.
        ready, reason = _hot_chat_resident_ready(resident, db_path=db_path)
        if not ready:
            _LOG.info('UH-A1 shared renderer unavailable after lock: %s', reason)
            raise UnifiedNormalWakeSharedUnavailable(reason)

        # The formal Chat mapping Registry must already be exactly at EOF.
        # Otherwise a shared Wake turn would hide an older mapping backlog.
        watermark, reason = prepare_shared_transcript_watermark(
            resident,
            db_path=db_path,
        )
        if watermark is None:
            _LOG.info('UH-A1 shared renderer unavailable before turn: %s', reason)
            raise UnifiedNormalWakeSharedUnavailable(reason)

        identity = dict(renderer_input.decision_identity)
        turn_id = (
            'uh-a1-render:'
            + str(
                identity.get('decision_attempt_id')
                or identity.get('wake_run_id')
                or 'normal'
            ).strip()
        )
        lease = issue_turn_lease(
            turn_id=turn_id,
            turn_mode='wake',
            issued_from='default_policy',
        )
        def final_shared_probe():
            return _hot_chat_resident_ready(resident, db_path=db_path)

        def establish_shared_delivery_fence():
            nonlocal delivery_fence, shared_started
            delivery_fence = begin_shared_wake_delivery_fence(
                gateway=gateway,
                resident=resident,
            )
            shared_started = True

        result = invoke_renderer_cc_hot(
            renderer_input=renderer_input,
            resident=resident,
            turn_lease=lease,
            final_probe=final_shared_probe,
            on_guarded_probe_pass=establish_shared_delivery_fence,
        )

        # Do not keep malformed Renderer output in the hot Chat lineage. The
        # caller validates again, but this pre-watermark check protects the
        # shared resident before we declare the provider-only range consumed.
        try:
            validate_rendered_content(str(result.get('text') or ''))
        except Exception as exc:
            raise RuntimeError('uh_a1_shared_renderer_invalid_output') from exc

        # This provider-only Wake round is deliberately not a formal Chat
        # mapping pair. Advance the exact Registry scan watermark past it so
        # the next Chat mapping starts at the next formal user turn.
        try:
            result['transcript_skip'] = commit_shared_transcript_watermark(
                watermark,
                resident,
                db_path=db_path,
                jsonl_finality=result.get('transcript_finality'),
            )
        except Exception as exc:
            raise RuntimeError(
                'uh_a1_transcript_watermark_commit_failed'
            ) from exc
        candidate_wall = result.pop('_cache_refresh_candidate_at', None)
        candidate_monotonic = result.pop(
            '_cache_refresh_candidate_monotonic', None,
        )
        if candidate_wall is not None and candidate_monotonic is not None:
            resident.commit_cache_freshness(
                wall_at=candidate_wall,
                monotonic_at=candidate_monotonic,
            )
        result['_shared_delivery_fence'] = delivery_fence
        return result
    except _SharedPreTurnUnavailable as exc:
        # This is still pre-turn: no delivery fence was created and no stdin
        # was sent. Unified normal Wake must stop here; it cannot use Relay.
        _LOG.info('UH-A1 final guarded stale probe rejected shared route: %s', exc)
        raise UnifiedNormalWakeSharedUnavailable(str(exc)) from exc
    except Exception as exc:
        # Zero-wait busy is pre-turn unavailability. Once shared stdin may have
        # started, never auto-render a second answer.
        if not acquired and '上一轮回复仍在生成中' in str(exc):
            _LOG.info('UH-A1 shared renderer busy; skipping Unified normal Wake')
            raise UnifiedNormalWakeSharedUnavailable(
                'generation_lock_busy'
            ) from exc
        if shared_started and delivery_fence is not None:
            # Retire the matching resident while the same generation condition
            # is held, then release the shared owner token. An old Wake stream
            # cannot release a newer Chat owner after TTL escape.
            delivery_fence.finish(
                False,
                cache_info={
                    'provider': 'claude_code',
                    'source': 'wake',
                    'b3_authority': True,
                },
                window_identity={
                    'context_id': watermark.context_id if watermark else None,
                    'context_epoch': watermark.context_epoch if watermark else None,
                    'resident_generation': (
                        watermark.resident_generation if watermark else None
                    ),
                },
            )
        raise
    finally:
        if acquired and delivery_fence is None:
            gateway._gen_release(None)


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
    """Invoke Renderer and return {text, provider, model_identity, ...}."""
    if invoke_fn is None:
        try:
            shared = _try_invoke_shared_renderer(renderer_input=renderer_input)
        except UnifiedNormalWakeSharedUnavailable as exc:
            return {
                'text': '',
                'provider': 'none',
                'model_identity': 'none',
                'shared_unavailable': True,
                'shared_unavailable_reason': str(exc) or 'unavailable',
            }
        if shared is not None:
            return shared
    invoker = invoke_fn or invoke_renderer_relay
    result = invoker(renderer_input=renderer_input, timeout_sec=timeout_sec)
    if isinstance(result, dict):
        return result
    return {'text': str(result or ''), 'provider': 'api_relay', 'model_identity': 'relay:test'}
