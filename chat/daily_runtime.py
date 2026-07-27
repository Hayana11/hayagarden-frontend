"""Daily Soft Window R1 — formal Claude Code resident orchestration.

Flag-gated via daily_context.enabled(). When disabled, this module must not be
imported on the production chat hot path except through a cheap enabled() guard.
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from chat import daily_context as dc
from chat import daily_history as dh
from chat.daily_context import (
    ConflictError,
    DEFAULT_CHAT_ID,
    DeferredError,
    HotTurnCursorError,
    chat_day_for_timestamp,
)

logger = logging.getLogger(__name__)

NL = chr(10)
CONTEXT_PROFILE = 'daily_window'
DEFAULT_LEASE_TTL = 360


class DailyRuntimeError(Exception):
    """Base error for daily runtime orchestration."""

    def __init__(self, message: str, *, error_code: str = 'daily_runtime_error', retryable: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class LeaseConflictError(DailyRuntimeError):
    def __init__(self, message: str = 'resident turn lease conflict'):
        super().__init__(message, error_code='resident_turn_lease_conflict', retryable=True)


class EpochMismatchError(DailyRuntimeError):
    def __init__(self, message: str = 'epoch token mismatch'):
        super().__init__(message, error_code='epoch_mismatch', retryable=False)


class DailyWindowToolFencePending(DailyRuntimeError):
    def __init__(self, message: str = 'tool fencing not implemented for daily window'):
        super().__init__(message, error_code='DailyWindowToolFencePending', retryable=False)


@dataclass
class DailyTurnPlan:
    request_id: str
    chat_id: str
    local_day: str
    context_id: int
    context_epoch: int
    resident_generation: int
    resident_key: str
    user_message_id: int
    epoch_token: dict[str, Any]
    lease_owner: str
    is_cold: bool
    is_respawn: bool
    cursor_before: Optional[int]
    assembly: dict[str, Any]
    manifest: dict[str, Any]
    user_content: str = ''
    lease_acquired: bool = False
    lease_released: bool = False
    db_path: Optional[str] = None
    _resident_close_fn: Optional[Callable[[], None]] = field(default=None, repr=False)


_BINDING_KEY: Optional[str] = None


def reset_bindings_for_tests() -> None:
    global _BINDING_KEY
    _BINDING_KEY = None


def make_resident_key(
    *,
    chat_id: str,
    context_epoch: int,
    resident_generation: int,
) -> str:
    return 'daily:%s:%s:%s' % (chat_id, int(context_epoch), int(resident_generation))


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _sha256_owner(owner: str) -> str:
    return _sha256_text(str(owner or ''))


def verify_epoch_token(plan: DailyTurnPlan) -> None:
    token = plan.epoch_token
    if int(token.get('context_epoch') or -1) != int(plan.context_epoch):
        raise EpochMismatchError('context_epoch mismatch')
    if int(token.get('resident_generation') or -1) != int(plan.resident_generation):
        raise EpochMismatchError('resident_generation mismatch')
    if str(token.get('chat_id') or '') != str(plan.chat_id):
        raise EpochMismatchError('chat_id mismatch')
    if not dc.is_epoch_current(token, db_path=plan.db_path):
        raise EpochMismatchError('epoch no longer current')


def _fetch_user_message(
    message_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    conn = dc._connect(db_path)
    try:
        row = conn.execute(
            'SELECT id, author, content, image_url FROM chat_messages WHERE id=?',
            (int(message_id),),
        ).fetchone()
        if row is None:
            raise DailyRuntimeError('user message not found: %s' % message_id, error_code='user_message_missing')
        content = str(row['content'] or '').strip()
        if not content and str(row['image_url'] or '').strip():
            content = '[image]'
        return {'id': int(row['id']), 'content': content}
    finally:
        conn.close()


def _format_history_messages(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get('role') or 'user'
        label = '用户' if role == 'user' else '费佳'
        lines.append('[%s] %s' % (label, msg.get('content') or ''))
    return NL.join(lines)


def _format_carryover_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ''
    lines = ['【昨日延续对话】']
    for msg in messages:
        role = msg.get('role') or 'user'
        label = '用户' if role == 'user' else '费佳'
        lines.append('[%s] %s' % (label, msg.get('content') or ''))
    return NL.join(lines)


def format_resident_turn_content(
    *,
    assembly: dict[str, Any],
    user_content: str,
    is_cold: bool,
    is_respawn: bool,
) -> str:
    cold_like = bool(is_cold or is_respawn)
    prefix_parts: list[str] = []
    if cold_like:
        handoff = str(assembly.get('day_handoff') or '').strip()
        if handoff:
            prefix_parts.append(handoff)
        carryover = assembly.get('carryover_messages') or []
        carry_text = _format_carryover_messages(carryover)
        if carry_text:
            prefix_parts.append(carry_text)
    state_text = str(assembly.get('state') or '').strip()
    if state_text:
        prefix_parts.append(state_text)
    history = assembly.get('current_day_history') or []
    prefix = NL.join(p for p in prefix_parts if p)
    if cold_like and history:
        history_text = _format_history_messages(history)
        body = (
            '以下是本聊天日内的正式对话记录：' + NL + NL
            + history_text + NL + NL
            + '请回复最后一条用户消息。'
        )
        if prefix:
            return prefix + NL + NL + body + NL + NL + str(user_content or '')
        return body + NL + NL + str(user_content or '')
    if history:
        history_text = _format_history_messages(history)
        replay = '【新增正式对话】' + NL + history_text + NL + NL
        if prefix:
            return prefix + NL + NL + replay + str(user_content or '')
        return replay + str(user_content or '')
    if prefix:
        return prefix + NL + NL + str(user_content or '')
    return str(user_content or '')


def _determine_turn_kind(
    *,
    resident_key: str,
    context_id: int,
    resident_generation: int,
    resident_alive_fn: Callable[[str], bool],
    db_path: Optional[str],
    force_respawn: bool,
) -> tuple[str, bool, bool]:
    if force_respawn:
        return 'respawn', False, True
    cursor = dc.get_resident_history_cursor(context_id, resident_generation, db_path=db_path)
    bound = resident_alive_fn(resident_key)
    if bound and cursor is not None:
        return 'hot', False, False
    if bound and cursor is None:
        return 'cold', True, False
    return 'cold', True, False


def _build_manifest_base(
    *,
    plan_fields: dict[str, Any],
    assembly_manifest: dict[str, Any],
    turn_kind: str,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
) -> dict[str, Any]:
    manifest = dict(assembly_manifest)
    manifest.update({
        'context_profile': CONTEXT_PROFILE,
        'request_id': plan_fields['request_id'],
        'chat_id': plan_fields['chat_id'],
        'local_day': plan_fields['local_day'],
        'context_id': plan_fields['context_id'],
        'context_epoch': plan_fields['context_epoch'],
        'resident_generation': plan_fields['resident_generation'],
        'resident_key_hash': _sha256_text(plan_fields['resident_key']),
        'turn_kind': turn_kind,
        'lease_owner_hash': _sha256_owner(plan_fields['lease_owner']),
        'lease_acquired': False,
        'lease_released': False,
        'assistant_message_id': None,
        'cursor_after': None,
        'cursor_cas_success': None,
        'legacy_cold_once_injected': False,
        'auto_recall_injected': False,
        'relationship_context_injected': False,
        'pre_boundary_unselected_history_injected': False,
        'provider': provider,
        'model': model,
        'static_system_sha256': static_system_sha256,
        'persona_sha256': persona_sha256,
        'stop_reason': None,
        'input_tokens': None,
        'output_tokens': None,
        'error_code': None,
    })
    return manifest


def prepare_daily_turn(
    *,
    user_message_id: int,
    chat_id: str = DEFAULT_CHAT_ID,
    request_id: Optional[str] = None,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    lease_owner: Optional[str] = None,
    resident_alive_fn: Optional[Callable[[str], bool]] = None,
    force_respawn: bool = False,
    resident: Optional[Any] = None,
    static_system: str = '',
    static_system_sha256: str = '',
    persona_sha256: str = '',
    provider: str = 'claude_code',
    model: str = '',
) -> DailyTurnPlan:
    if not dc.enabled():
        raise DailyRuntimeError('DAILY_SOFT_WINDOW_ENABLED=0', error_code='daily_disabled')

    req_id = str(request_id or uuid.uuid4())
    owner = str(lease_owner or req_id)
    now = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS))
    local_day = chat_day_for_timestamp(now)
    alive_fn = resident_alive_fn or (lambda key: _BINDING_KEY == key)

    # Rollover: defer new epoch while previous provider turn lease is active.
    if dc.has_active_provider_turn_lease(chat_id, db_path=db_path, now=now):
        prior = dc.get_latest_active_context(chat_id, db_path=db_path)
        if prior and str(prior.get('local_day') or '') != local_day:
            raise DeferredError('provider request in flight; rollover deferred')

    ctx = dc.get_or_create_daily_context(
        chat_id=chat_id,
        local_day=local_day,
        now=now,
        db_path=db_path,
        provider_busy=dc.has_active_provider_turn_lease(chat_id, db_path=db_path, now=now),
    )
    context_id = int(ctx['id'])
    context_epoch = int(ctx['context_epoch'])
    resident_generation = int(ctx.get('resident_generation') or 1)

    dc.ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)
    refreshed = dc.get_daily_context_by_id(context_id, db_path=db_path) or ctx
    context_epoch = int(refreshed['context_epoch'])
    resident_generation = int(refreshed.get('resident_generation') or 1)

    resident_key = make_resident_key(
        chat_id=chat_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
    )
    turn_kind, is_cold, is_respawn = _determine_turn_kind(
        resident_key=resident_key,
        context_id=context_id,
        resident_generation=resident_generation,
        resident_alive_fn=alive_fn,
        db_path=db_path,
        force_respawn=force_respawn,
    )

    user_row = _fetch_user_message(user_message_id, db_path=db_path)
    try:
        lease_row = dc.acquire_resident_turn_lease(
            context_id,
            resident_generation,
            lease_owner=owner,
            request_message_id=int(user_message_id),
            db_path=db_path,
            now=now,
        )
    except ConflictError as exc:
        raise LeaseConflictError(str(exc)) from exc
    _ = lease_row

    epoch_token = dc.make_epoch_token(
        chat_id=chat_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
    )
    verify_epoch_token(DailyTurnPlan(
        request_id=req_id,
        chat_id=chat_id,
        local_day=local_day,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
        resident_key=resident_key,
        user_message_id=int(user_message_id),
        epoch_token=epoch_token,
        lease_owner=owner,
        is_cold=is_cold,
        is_respawn=is_respawn,
        cursor_before=dc.get_resident_history_cursor(
            context_id, resident_generation, db_path=db_path,
        ),
        assembly={},
        manifest={},
        db_path=db_path,
    ))

    last_state: Optional[dict[str, str]] = None
    cold_like = bool(is_cold or is_respawn)
    if turn_kind == 'hot' and resident is not None:
        raw = getattr(resident, 'last_state_snapshot', None) or {}
        if isinstance(raw, dict):
            last_state = {str(k): str(v) for k, v in raw.items()}

    assembly = dh.build_daily_window_context(
        chat_id=chat_id,
        daily_context=refreshed,
        current_user_message_id=int(user_message_id),
        static_system=static_system,
        is_cold=is_cold,
        is_respawn=is_respawn,
        last_state_snapshot=last_state,
        inject_handoff=cold_like,
        inject_carryover=cold_like,
        db_path=db_path,
    )

    manifest = _build_manifest_base(
        plan_fields={
            'request_id': req_id,
            'chat_id': chat_id,
            'local_day': local_day,
            'context_id': context_id,
            'context_epoch': context_epoch,
            'resident_generation': resident_generation,
            'resident_key': resident_key,
            'lease_owner': owner,
        },
        assembly_manifest=dict(assembly.get('manifest') or {}),
        turn_kind=turn_kind,
        static_system_sha256=static_system_sha256 or _sha256_text(static_system),
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
    )
    manifest['lease_acquired'] = True

    return DailyTurnPlan(
        request_id=req_id,
        chat_id=chat_id,
        local_day=local_day,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
        resident_key=resident_key,
        user_message_id=int(user_message_id),
        epoch_token=epoch_token,
        lease_owner=owner,
        is_cold=is_cold,
        is_respawn=is_respawn,
        cursor_before=manifest.get('cursor_before'),
        assembly=assembly,
        manifest=manifest,
        user_content=str(user_row.get('content') or ''),
        lease_acquired=True,
        db_path=db_path,
    )


def bind_resident_key(resident_key: str) -> None:
    global _BINDING_KEY
    _BINDING_KEY = resident_key


def unbind_resident_key() -> None:
    global _BINDING_KEY
    _BINDING_KEY = None


def close_resident(resident: Any, *, reason: str = 'daily_abort') -> None:
    kill = getattr(resident, '_kill', None)
    if callable(kill):
        kill(quiet=True)
    unbind_resident_key()


def stream_daily_resident_turn(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    env: dict[str, str],
    static_system: str,
) -> Iterator[tuple[str, Any]]:
    """Yield resident events for one daily-window turn. No DB writes."""
    verify_epoch_token(plan)
    content = format_resident_turn_content(
        assembly=plan.assembly,
        user_content=plan.user_content,
        is_cold=plan.is_cold,
        is_respawn=plan.is_respawn,
    )
    is_cold = bool(resident.ensure_alive(static_system, env))
    bind_resident_key(plan.resident_key)
    state_snapshot = dict(plan.assembly.get('state_snapshot') or {})
    commit_meta = {'state_snapshot': state_snapshot}
    if plan.assembly.get('manifest', {}).get('state_mode') == 'delta':
        commit_meta['lean_state_active'] = True
    saw_tool = False
    for evt, payload in resident.send_turn(content, commit_meta=commit_meta):
        if evt == 'tool_use':
            saw_tool = True
            raise DailyWindowToolFencePending()
        yield evt, payload
    if saw_tool:
        raise DailyWindowToolFencePending()


def complete_daily_turn(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
    stop_reason: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
) -> dict[str, Any]:
    verify_epoch_token(plan)
    aid = int(assistant_message_id)
    if aid <= 0:
        raise DailyRuntimeError('assistant_message_id required', error_code='assistant_missing')

    cursor_result: dict[str, Any]
    cas_success = False
    try:
        cursor_result = dc.advance_resident_history_cursor(
            plan.context_id,
            plan.resident_generation,
            aid,
            expected_cursor=plan.cursor_before,
            db_path=plan.db_path,
        )
        cas_success = bool(cursor_result.get('advanced', True))
    except ConflictError as exc:
        logger.warning(
            'daily cursor CAS conflict context=%s gen=%s: %s',
            plan.context_id, plan.resident_generation, exc,
        )
        close_resident_for_plan(plan)
        dc.respawn_daily_resident(plan.context_id, db_path=plan.db_path)
        plan.manifest['cursor_cas_success'] = False
        plan.manifest['error_code'] = 'cursor_cas_conflict'
        _release_lease(plan)
        plan.manifest['integrity_warning'] = 'cursor_cas_conflict'
        return dict(plan.manifest)

    _release_lease(plan)
    plan.manifest.update({
        'assistant_message_id': aid,
        'cursor_after': int(cursor_result.get('history_cursor_message_id') or aid),
        'cursor_cas_success': cas_success,
        'stop_reason': stop_reason,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'error_code': None,
    })
    return dict(plan.manifest)


def abort_daily_turn(
    plan: DailyTurnPlan,
    *,
    error_code: str,
    close_resident_obj: Optional[Any] = None,
    respawn: bool = True,
) -> dict[str, Any]:
    if close_resident_obj is not None:
        close_resident(close_resident_obj, reason=error_code)
    else:
        close_resident_for_plan(plan)
    if respawn:
        try:
            dc.respawn_daily_resident(plan.context_id, db_path=plan.db_path)
        except Exception:
            logger.exception('respawn_daily_resident failed for context %s', plan.context_id)
    _release_lease(plan)
    plan.manifest['error_code'] = error_code
    return dict(plan.manifest)


def close_resident_for_plan(plan: DailyTurnPlan) -> None:
    if plan._resident_close_fn:
        plan._resident_close_fn()
    unbind_resident_key()


def _release_lease(plan: DailyTurnPlan) -> None:
    if plan.lease_released:
        return
    try:
        released = dc.release_resident_turn_lease(
            plan.context_id,
            plan.resident_generation,
            lease_owner=plan.lease_owner,
            db_path=plan.db_path,
        )
        plan.lease_released = True
        plan.manifest['lease_released'] = bool(released)
    except Exception:
        logger.exception('release_resident_turn_lease failed')
        plan.manifest['lease_released'] = False


def handle_provider_success(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
    raw_text: str,
    usage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if not str(raw_text or '').strip():
        abort_daily_turn(plan, error_code='empty_provider_response')
        raise DailyRuntimeError('empty provider response', error_code='empty_provider_response')
    usage = usage or {}
    return complete_daily_turn(
        plan,
        assistant_message_id=int(assistant_message_id),
        stop_reason=str(usage.get('stop_reason') or 'end_turn'),
        input_tokens=usage.get('input_tokens'),
        output_tokens=usage.get('output_tokens'),
    )


def handle_provider_failure(
    plan: DailyTurnPlan,
    *,
    error_code: str,
    resident: Optional[Any] = None,
) -> dict[str, Any]:
    return abort_daily_turn(
        plan,
        error_code=error_code,
        close_resident_obj=resident,
        respawn=True,
    )
