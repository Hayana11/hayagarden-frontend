"""Daily Soft Window R1 — formal Claude Code resident orchestration."""
from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

import cc_resident

from chat import daily_context as dc
from chat import daily_history as dh
from chat.daily_context import (
    ConflictError,
    DEFAULT_CHAT_ID,
    DeferredError,
    StaleOriginDayError,
    chat_day_for_timestamp,
    make_resident_key,
)

logger = logging.getLogger(__name__)

NL = chr(10)
CONTEXT_PROFILE = 'daily_window'
DEFAULT_LEASE_TTL = 480
LEASE_HEARTBEAT_INTERVAL = 50
WORKER_ID = '%s:%s' % (socket.gethostname(), os.getpid())
SAVE_RE = re.compile(r'\[\[SAVE(?::[^\]]+)?\]\]', re.IGNORECASE)
DAILY_TOOL_PROFILE = cc_resident.TOOL_PROFILE_TEXT_ONLY


class DailyRuntimeError(Exception):
    def __init__(self, message: str, *, error_code: str = 'daily_runtime_error', retryable: bool = False):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class LeaseConflictError(DailyRuntimeError):
    def __init__(self, message: str = 'resident turn lease conflict'):
        super().__init__(message, error_code='resident_turn_lease_conflict', retryable=True)


class LeaseHeartbeatTerminalFailure(DailyRuntimeError):
    def __init__(self, message: str = 'lease heartbeat failed terminally'):
        super().__init__(message, error_code='lease_heartbeat_terminal_failure', retryable=False)


class EpochMismatchError(DailyRuntimeError):
    def __init__(self, message: str = 'epoch token mismatch'):
        super().__init__(message, error_code='epoch_mismatch', retryable=False)


class DailyWindowToolFencePending(DailyRuntimeError):
    def __init__(self, message: str = 'tool fencing not implemented for daily window'):
        super().__init__(message, error_code='DailyWindowToolFencePending', retryable=False)


class DuplicateTurnInProgress(DailyRuntimeError):
    def __init__(self, message: str = 'duplicate turn already in progress'):
        super().__init__(message, error_code='duplicate_turn_in_progress', retryable=True)


class CursorCASConflictAfterPersist(DailyRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        assistant_message_id: int,
        manifest: dict[str, Any],
    ):
        super().__init__(message, error_code='cursor_cas_conflict', retryable=False)
        self.assistant_message_id = int(assistant_message_id)
        self.manifest = dict(manifest)


@dataclass
class LocalResidentBinding:
    resident_key: str
    context_id: int
    context_epoch: int
    resident_generation: int
    bound_cursor_message_id: Optional[int]
    process_generation: int
    tool_profile: str


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
    worker_id: str = WORKER_ID
    tool_profile: str = DAILY_TOOL_PROFILE
    user_created_at: Optional[datetime.datetime] = None
    origin_local_day: str = ''
    turn_started_at: Optional[datetime.datetime] = None
    _resident_close_fn: Optional[Callable[[], None]] = field(default=None, repr=False)


_LOCAL_BINDING: Optional[LocalResidentBinding] = None

_CONTEXT_SWITCH_RESIDENT_CLOSER: Optional[Callable[[dict[str, Any]], None]] = None


def register_context_switch_resident_closer(
    fn: Optional[Callable[[dict[str, Any]], None]],
) -> None:
    """Gateway registers _CC_RESIDENT close handler for manual context switch."""
    global _CONTEXT_SWITCH_RESIDENT_CLOSER
    _CONTEXT_SWITCH_RESIDENT_CLOSER = fn


def notify_context_window_switched(result: dict[str, Any]) -> None:
    closer = _CONTEXT_SWITCH_RESIDENT_CLOSER
    if closer is not None:
        closer(result)


def reset_bindings_for_tests() -> None:
    global _LOCAL_BINDING
    _LOCAL_BINDING = None


def get_local_binding() -> Optional[LocalResidentBinding]:
    return _LOCAL_BINDING


def set_local_binding(binding: Optional[LocalResidentBinding]) -> None:
    global _LOCAL_BINDING
    _LOCAL_BINDING = binding


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def strip_daily_save_markers(text: str) -> tuple[str, bool]:
    raw = str(text or '')
    had = bool(SAVE_RE.search(raw))
    cleaned = SAVE_RE.sub('', raw).strip()
    return cleaned, had


def verify_epoch_token(plan: DailyTurnPlan) -> None:
    token = plan.epoch_token
    if int(token.get('context_id') or -1) != int(plan.context_id):
        raise EpochMismatchError('context_id mismatch')
    if int(token.get('context_epoch') or -1) != int(plan.context_epoch):
        raise EpochMismatchError('context_epoch mismatch')
    if int(token.get('resident_generation') or -1) != int(plan.resident_generation):
        raise EpochMismatchError('resident_generation mismatch')
    if str(token.get('chat_id') or '') != str(plan.chat_id):
        raise EpochMismatchError('chat_id mismatch')
    if not dc.is_epoch_current(token, db_path=plan.db_path):
        raise EpochMismatchError('epoch no longer current')


def is_epoch_token_current(plan: DailyTurnPlan) -> bool:
    try:
        verify_epoch_token(plan)
        return True
    except EpochMismatchError:
        return False


def _parse_message_created_at(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(str(value or '').strip(), '%Y-%m-%d %H:%M:%S')


def _fetch_user_message(message_id: int, *, db_path: Optional[str] = None) -> dict[str, Any]:
    conn = dc._connect(db_path)
    try:
        row = conn.execute(
            'SELECT id, author, content, image_url, created_at FROM chat_messages WHERE id=?',
            (int(message_id),),
        ).fetchone()
        if row is None:
            raise DailyRuntimeError('user message not found: %s' % message_id, error_code='user_message_missing')
        content = str(row['content'] or '').strip()
        if not content and str(row['image_url'] or '').strip():
            content = '[image]'
        created_at = str(row['created_at'] or '').strip()
        return {
            'id': int(row['id']),
            'content': content,
            'created_at': created_at,
        }
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


def _binding_matches_plan(binding: Optional[LocalResidentBinding], plan: DailyTurnPlan) -> bool:
    if binding is None:
        return False
    return (
        binding.resident_key == plan.resident_key
        and int(binding.context_epoch) == int(plan.context_epoch)
        and int(binding.resident_generation) == int(plan.resident_generation)
        and binding.tool_profile == plan.tool_profile
    )


def _resident_is_alive(resident: Any) -> bool:
    alive = getattr(resident, '_alive', None)
    if callable(alive):
        return bool(alive())
    return bool(alive)


def _can_hot_turn(
    *,
    plan: DailyTurnPlan,
    resident: Any,
    db_cursor: Optional[int],
) -> bool:
    binding = get_local_binding()
    if not _binding_matches_plan(binding, plan):
        return False
    if not _resident_is_alive(resident):
        return False
    if db_cursor is None:
        return False
    if binding is not None and binding.bound_cursor_message_id != db_cursor:
        return False
    if int(binding.process_generation) != int(getattr(resident, 'generation', 0) or 0):
        return False
    owner = dc.get_resident_owner(plan.context_id, plan.resident_generation, db_path=plan.db_path)
    if owner is None:
        return False
    if str(owner.get('worker_id') or '') != str(plan.worker_id):
        return False
    if str(owner.get('resident_key') or '') != str(plan.resident_key):
        return False
    if owner.get('bound_cursor_message_id') is not None and int(owner['bound_cursor_message_id']) != int(db_cursor):
        return False
    if int(owner.get('process_generation') or 0) != int(getattr(resident, 'generation', 0) or 0):
        return False
    if str(getattr(resident, 'tool_profile', '')) != str(plan.tool_profile):
        return False
    return True


def close_local_resident_if_bound(
    resident: Any,
    *,
    expected_key: Optional[str],
    clear_binding: bool = True,
) -> bool:
    binding = get_local_binding()
    if expected_key is not None:
        if binding is None or binding.resident_key != expected_key:
            return False
    kill = getattr(resident, '_kill', None)
    if callable(kill):
        kill(quiet=True)
    if clear_binding:
        set_local_binding(None)
    return True


def close_local_resident_for_context_switch(
    resident: Any,
    *,
    source_context_id: int,
    source_context_epoch: int,
    source_resident_generation: int,
    chat_id: str = DEFAULT_CHAT_ID,
) -> bool:
    """Close CC resident only when local binding matches captured switch source."""
    binding = get_local_binding()
    if binding is None:
        return False
    expected_key = make_resident_key(
        chat_id=chat_id,
        context_epoch=int(source_context_epoch),
        resident_generation=int(source_resident_generation),
    )
    if (
        int(binding.context_id) != int(source_context_id)
        or int(binding.context_epoch) != int(source_context_epoch)
        or int(binding.resident_generation) != int(source_resident_generation)
        or str(binding.resident_key) != str(expected_key)
    ):
        logger.warning(
            'context switch resident close skipped: binding mismatch '
            '(have ctx=%s/%s/%s key=%s want ctx=%s/%s/%s key=%s)',
            binding.context_id,
            binding.context_epoch,
            binding.resident_generation,
            binding.resident_key,
            source_context_id,
            source_context_epoch,
            source_resident_generation,
            expected_key,
        )
        return False
    return close_local_resident_if_bound(resident, expected_key=expected_key)


def _close_stale_local_resident(resident: Any, *, expected_key: str) -> None:
    binding = get_local_binding()
    if binding is not None and binding.resident_key != expected_key:
        close_local_resident_if_bound(resident, expected_key=binding.resident_key)


def _release_lease_for(
    *,
    context_id: int,
    resident_generation: int,
    lease_owner: str,
    db_path: Optional[str],
    manifest: Optional[dict[str, Any]] = None,
) -> bool:
    try:
        released = dc.release_resident_turn_lease(
            context_id, resident_generation, lease_owner=lease_owner, db_path=db_path,
        )
        if manifest is not None:
            manifest['lease_released'] = bool(released)
        return bool(released)
    except Exception:
        logger.exception('release_resident_turn_lease failed')
        if manifest is not None:
            manifest['lease_released'] = False
        return False


def _release_lease(plan: DailyTurnPlan) -> None:
    if plan.lease_released:
        return
    _release_lease_for(
        context_id=plan.context_id,
        resident_generation=plan.resident_generation,
        lease_owner=plan.lease_owner,
        db_path=plan.db_path,
        manifest=plan.manifest,
    )
    plan.lease_released = True


class LeaseHeartbeat:
    def __init__(self, plan: DailyTurnPlan, *, on_failure: Optional[Callable[[], None]] = None):
        self._plan = plan
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failed = False
        self._on_failure = on_failure

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name='daily-lease-heartbeat', daemon=True)
        self._thread.start()

    def _fail(self) -> None:
        if self._failed:
            return
        self._failed = True
        if self._on_failure is not None:
            try:
                self._on_failure()
            except Exception:
                logger.exception('lease heartbeat on_failure callback failed')
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(LEASE_HEARTBEAT_INTERVAL):
            try:
                dc.renew_resident_turn_lease(
                    self._plan.context_id,
                    self._plan.resident_generation,
                    lease_owner=self._plan.lease_owner,
                    chat_id=self._plan.chat_id,
                    worker_id=self._plan.worker_id,
                    resident_key=self._plan.resident_key,
                    db_path=self._plan.db_path,
                    epoch_token=self._plan.epoch_token,
                )
            except Exception:
                logger.exception('lease heartbeat renew failed')
                self._fail()
                return

    def stop(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        return self._failed

    @property
    def failed(self) -> bool:
        return self._failed


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
        'lease_owner_hash': _sha256_text(plan_fields['lease_owner']),
        'worker_id_hash': _sha256_text(plan_fields.get('worker_id') or WORKER_ID),
        'tool_profile': plan_fields.get('tool_profile') or DAILY_TOOL_PROFILE,
        'lease_acquired': False,
        'lease_released': False,
        'assistant_message_id': None,
        'cursor_after': None,
        'cursor_cas_success': None,
        'unexpected_save_marker': False,
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


def _assemble_plan(
    *,
    req_id: str,
    owner: str,
    chat_id: str,
    local_day: str,
    refreshed: dict[str, Any],
    user_message_id: int,
    user_content: str,
    is_cold: bool,
    is_respawn: bool,
    turn_kind: str,
    cursor_before: Optional[int],
    resident: Optional[Any],
    static_system: str,
    static_system_sha256: str,
    persona_sha256: str,
    provider: str,
    model: str,
    db_path: Optional[str],
    lease_acquired: bool,
    user_created_at: Optional[datetime.datetime] = None,
    origin_local_day: str = '',
    turn_started_at: Optional[datetime.datetime] = None,
) -> DailyTurnPlan:
    context_id = int(refreshed['id'])
    context_epoch = int(refreshed['context_epoch'])
    resident_generation = int(refreshed['resident_generation'])
    resident_key = make_resident_key(
        chat_id=chat_id, context_epoch=context_epoch, resident_generation=resident_generation,
    )
    epoch_token = dc.make_epoch_token(
        chat_id=chat_id,
        context_id=context_id,
        context_epoch=context_epoch,
        resident_generation=resident_generation,
    )
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
            'worker_id': WORKER_ID,
            'tool_profile': DAILY_TOOL_PROFILE,
        },
        assembly_manifest=dict(assembly.get('manifest') or {}),
        turn_kind=turn_kind,
        static_system_sha256=static_system_sha256 or _sha256_text(static_system),
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
    )
    manifest['lease_acquired'] = lease_acquired
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
        cursor_before=cursor_before,
        assembly=assembly,
        manifest=manifest,
        user_content=user_content,
        lease_acquired=lease_acquired,
        db_path=db_path,
        user_created_at=user_created_at,
        origin_local_day=origin_local_day or local_day,
        turn_started_at=turn_started_at or user_created_at,
    )


def prepare_daily_turn(
    *,
    user_message_id: int,
    chat_id: str = DEFAULT_CHAT_ID,
    request_id: Optional[str] = None,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    wall_now: Optional[datetime.datetime] = None,
    origin_local_day: Optional[str] = None,
    lease_owner: Optional[str] = None,
    resident: Optional[Any] = None,
    static_system: str = '',
    static_system_sha256: str = '',
    persona_sha256: str = '',
    provider: str = 'claude_code',
    model: str = '',
    _cold_reprepare: bool = False,
) -> DailyTurnPlan:
    if not dc.enabled():
        raise DailyRuntimeError('DAILY_SOFT_WINDOW_ENABLED=0', error_code='daily_disabled')

    req_id = str(request_id or uuid.uuid4())
    owner = str(lease_owner or req_id)
    user_row = _fetch_user_message(user_message_id, db_path=db_path)
    user_created_at = _parse_message_created_at(user_row.get('created_at') or '')
    if now is not None:
        user_created_at = now
    origin_day = str(origin_local_day or chat_day_for_timestamp(user_created_at))
    turn_started_at = user_created_at
    lease_now = datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
    context_wall_now = wall_now or lease_now

    lease_context_id: Optional[int] = None
    lease_generation: Optional[int] = None
    lease_acquired = False

    try:
        if dc.has_active_provider_turn_lease(chat_id, db_path=db_path, now=lease_now):
            prior = dc.get_latest_active_context(chat_id, db_path=db_path)
            if prior and str(prior.get('local_day') or '') != origin_day:
                raise DeferredError('provider request in flight; rollover deferred')

        prior_ctx = dc.get_latest_active_context(chat_id, db_path=db_path)
        try:
            ctx = dc.resolve_or_create_daily_context_for_origin(
                chat_id=chat_id,
                origin_local_day=origin_day,
                actual_wall_now=context_wall_now,
                db_path=db_path,
                provider_busy=False,
            )
        except StaleOriginDayError as exc:
            raise DailyRuntimeError(
                str(exc), error_code='stale_origin_day', retryable=False,
            ) from exc
        context_id = int(ctx['id'])

        if (
            prior_ctx is not None
            and int(prior_ctx['id']) != context_id
            and resident is not None
        ):
            old_key = make_resident_key(
                chat_id=chat_id,
                context_epoch=int(prior_ctx['context_epoch']),
                resident_generation=int(prior_ctx['resident_generation']),
            )
            close_local_resident_if_bound(resident, expected_key=old_key)
            try:
                dc.retire_resident_for_rollover(int(prior_ctx['id']), db_path=db_path)
            except Exception:
                logger.exception('retire_resident_for_rollover failed')

        dc.ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)
        refreshed = dc.get_daily_context_by_id(context_id, db_path=db_path) or ctx
        context_epoch = int(refreshed['context_epoch'])
        resident_generation = int(refreshed['resident_generation'])
        resident_key = make_resident_key(
            chat_id=chat_id, context_epoch=context_epoch, resident_generation=resident_generation,
        )

        if resident is not None:
            _close_stale_local_resident(resident, expected_key=resident_key)

        db_cursor = dc.get_resident_history_cursor(
            context_id, resident_generation, db_path=db_path,
        )
        try:
            claim = dc.claim_daily_resident_turn(
                chat_id=chat_id,
                context_id=context_id,
                expected_context_epoch=context_epoch,
                worker_id=WORKER_ID,
                request_message_id=int(user_message_id),
                lease_owner=owner,
                resident_key=resident_key,
                bound_cursor_message_id=db_cursor,
                process_generation=int(getattr(resident, 'generation', 0) or 0) if resident else None,
                db_path=db_path,
                now=lease_now,
            )
        except ConflictError as exc:
            raise LeaseConflictError(str(exc)) from exc
        if claim.get('status') == 'idempotent':
            raise DuplicateTurnInProgress(
                'resident turn lease already held for this request',
            )
        lease_acquired = True
        lease_context_id = context_id
        if claim.get('status') == 'takeover':
            refreshed = dc.get_daily_context_by_id(context_id, db_path=db_path) or refreshed
            context_epoch = int(refreshed['context_epoch'])
            resident_generation = int(claim['resident_generation'])
            resident_key = str(claim['resident_key'])
            db_cursor = None
            if resident is not None:
                close_local_resident_if_bound(
                    resident,
                    expected_key=get_local_binding().resident_key if get_local_binding() else None,
                )
        else:
            resident_generation = int(claim['resident_generation'])
            resident_key = str(claim['resident_key'])
        lease_generation = resident_generation

        dc.record_daily_message_context(
            int(user_message_id),
            context_id=context_id,
            context_epoch=context_epoch,
            resident_generation=resident_generation,
            role='user',
            db_path=db_path,
        )

        is_cold = not _can_hot_turn(
            plan=DailyTurnPlan(
                request_id=req_id, chat_id=chat_id, local_day=origin_day,
                context_id=context_id, context_epoch=context_epoch,
                resident_generation=resident_generation, resident_key=resident_key,
                user_message_id=int(user_message_id), epoch_token={},
                lease_owner=owner, is_cold=True, is_respawn=False,
                cursor_before=db_cursor, assembly={}, manifest={},
                db_path=db_path, tool_profile=DAILY_TOOL_PROFILE,
                user_created_at=user_created_at, origin_local_day=origin_day,
                turn_started_at=turn_started_at,
            ),
            resident=resident or object(),
            db_cursor=db_cursor,
        )
        is_respawn = _cold_reprepare
        turn_kind = 'respawn' if is_respawn else ('hot' if not is_cold else 'cold')

        plan = _assemble_plan(
            req_id=req_id,
            owner=owner,
            chat_id=chat_id,
            local_day=origin_day,
            refreshed=refreshed,
            user_message_id=int(user_message_id),
            user_content=str(user_row.get('content') or ''),
            is_cold=is_cold,
            is_respawn=is_respawn,
            turn_kind=turn_kind,
            cursor_before=db_cursor,
            resident=resident,
            static_system=static_system,
            static_system_sha256=static_system_sha256,
            persona_sha256=persona_sha256,
            provider=provider,
            model=model,
            db_path=db_path,
            lease_acquired=lease_acquired,
            user_created_at=user_created_at,
            origin_local_day=origin_day,
            turn_started_at=turn_started_at,
        )
        verify_epoch_token(plan)
        return plan
    except Exception:
        if lease_acquired and lease_context_id is not None and lease_generation is not None:
            _release_lease_for(
                context_id=lease_context_id,
                resident_generation=lease_generation,
                lease_owner=owner,
                db_path=db_path,
            )
        raise


def ensure_resident_and_stream(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    env: dict[str, str],
    static_system: str,
    _reprep_depth: int = 0,
) -> Iterator[tuple[str, Any]]:
    """Prepare resident process, handle hot→cold mismatch, stream one turn."""
    verify_epoch_token(plan)
    if (plan.is_cold or plan.is_respawn) and resident is not None:
        binding = get_local_binding()
        if not _binding_matches_plan(binding, plan) and _resident_is_alive(resident):
            kill = getattr(resident, '_kill', None)
            if callable(kill):
                kill(quiet=True)
            set_local_binding(None)

    def _heartbeat_failure() -> None:
        close_local_resident_if_bound(resident, expected_key=plan.resident_key)

    heartbeat = LeaseHeartbeat(plan, on_failure=_heartbeat_failure)
    heartbeat.start()
    try:
        binding = get_local_binding()
        if binding is not None and not _binding_matches_plan(binding, plan):
            close_local_resident_if_bound(resident, expected_key=binding.resident_key)

        actual_cold = bool(
            resident.ensure_alive(static_system, env, tool_profile=plan.tool_profile)
        )
        if not plan.is_cold and not plan.is_respawn and actual_cold:
            heartbeat.stop()
            _release_lease(plan)
            close_local_resident_if_bound(resident, expected_key=plan.resident_key)
            if _reprep_depth >= 1:
                raise DailyRuntimeError(
                    'resident cold respawn during hot plan',
                    error_code='hot_to_cold_mismatch',
                )
            new_plan = reprepare_after_hot_cold_mismatch(
                plan,
                resident=resident,
                static_system=static_system,
                static_system_sha256=plan.manifest.get('static_system_sha256') or _sha256_text(static_system),
                persona_sha256=plan.manifest.get('persona_sha256') or '',
                provider=str(plan.manifest.get('provider') or 'claude_code'),
                model=str(plan.manifest.get('model') or ''),
            )
            yield from ensure_resident_and_stream(
                new_plan,
                resident=resident,
                env=env,
                static_system=static_system,
                _reprep_depth=_reprep_depth + 1,
            )
            return

        content = format_resident_turn_content(
            assembly=plan.assembly,
            user_content=plan.user_content,
            is_cold=plan.is_cold or actual_cold,
            is_respawn=plan.is_respawn,
        )
        db_cursor = dc.get_resident_history_cursor(
            plan.context_id, plan.resident_generation, db_path=plan.db_path,
        )
        set_local_binding(LocalResidentBinding(
            resident_key=plan.resident_key,
            context_id=plan.context_id,
            context_epoch=plan.context_epoch,
            resident_generation=plan.resident_generation,
            bound_cursor_message_id=db_cursor,
            process_generation=int(getattr(resident, 'generation', 0) or 0),
            tool_profile=plan.tool_profile,
        ))
        dc.upsert_resident_owner(
            plan.context_id,
            plan.resident_generation,
            worker_id=plan.worker_id,
            resident_key=plan.resident_key,
            bound_cursor_message_id=db_cursor,
            process_generation=int(getattr(resident, 'generation', 0) or 0),
            db_path=plan.db_path,
        )

        state_snapshot = dict(plan.assembly.get('state_snapshot') or {})
        commit_meta = {'state_snapshot': state_snapshot}
        if plan.assembly.get('manifest', {}).get('state_mode') == 'delta':
            commit_meta['lean_state_active'] = True

        if heartbeat.failed:
            raise LeaseConflictError('lease heartbeat failed before send')

        for evt, payload in resident.send_turn(content, commit_meta=commit_meta):
            if heartbeat.failed:
                close_local_resident_if_bound(resident, expected_key=plan.resident_key)
                raise LeaseHeartbeatTerminalFailure('lease heartbeat failed during stream')
            if evt == 'tool_use':
                raise DailyWindowToolFencePending()
            yield evt, payload

        if heartbeat.stop():
            close_local_resident_if_bound(resident, expected_key=plan.resident_key)
            raise LeaseHeartbeatTerminalFailure('lease heartbeat failed after stream')
    finally:
        heartbeat.stop()
        if heartbeat.failed:
            close_local_resident_if_bound(resident, expected_key=plan.resident_key)


def stream_daily_resident_turn(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    env: dict[str, str],
    static_system: str,
) -> Iterator[tuple[str, Any]]:
    yield from ensure_resident_and_stream(
        plan, resident=resident, env=env, static_system=static_system,
    )


def complete_daily_turn(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
    stop_reason: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    unexpected_save_marker: bool = False,
) -> dict[str, Any]:
    verify_epoch_token(plan)
    aid = int(assistant_message_id)
    if aid <= 0:
        raise DailyRuntimeError('assistant_message_id required', error_code='assistant_missing')

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
        logger.warning('daily cursor CAS conflict: %s', exc)
        abort_daily_turn(plan, error_code='cursor_cas_conflict', respawn=is_epoch_token_current(plan))
        plan.manifest['cursor_cas_success'] = False
        plan.manifest['error_code'] = 'cursor_cas_conflict'
        plan.manifest['integrity_warning'] = 'cursor_cas_conflict'
        raise CursorCASConflictAfterPersist(
            'cursor CAS conflict after assistant persist',
            assistant_message_id=aid,
            manifest=dict(plan.manifest),
        ) from exc

    _release_lease(plan)
    plan.manifest.update({
        'assistant_message_id': aid,
        'cursor_after': int(cursor_result.get('history_cursor_message_id') or aid),
        'cursor_cas_success': cas_success,
        'unexpected_save_marker': bool(unexpected_save_marker),
        'stop_reason': stop_reason,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'error_code': None,
    })
    binding = get_local_binding()
    if binding and binding.resident_key == plan.resident_key:
        binding.bound_cursor_message_id = aid
        set_local_binding(binding)
    return dict(plan.manifest)


def abort_daily_turn(
    plan: DailyTurnPlan,
    *,
    error_code: str,
    resident: Optional[Any] = None,
    respawn: bool = True,
) -> dict[str, Any]:
    current = is_epoch_token_current(plan)
    if resident is not None:
        close_local_resident_if_bound(resident, expected_key=plan.resident_key)
    elif plan._resident_close_fn:
        plan._resident_close_fn()
    if respawn and current:
        try:
            dc.respawn_daily_resident(plan.context_id, db_path=plan.db_path)
        except Exception:
            logger.exception('respawn_daily_resident failed')
    _release_lease(plan)
    plan.manifest['error_code'] = error_code
    plan.manifest['abort_epoch_current'] = current
    return dict(plan.manifest)


def persist_daily_assistant_for_plan(
    plan: DailyTurnPlan,
    *,
    content: str,
    thinking: str = '',
    tool_calls: str = '',
    cache_info: str = '',
    choices: str = '',
) -> int:
    return dc.persist_daily_assistant_if_current(
        chat_id=plan.chat_id,
        context_id=plan.context_id,
        context_epoch=plan.context_epoch,
        resident_generation=plan.resident_generation,
        lease_owner=plan.lease_owner,
        content=content,
        thinking=thinking,
        tool_calls=tool_calls,
        cache_info=cache_info,
        choices=choices,
        db_path=plan.db_path,
    )


def handle_provider_success(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
    raw_text: str,
    usage: Optional[dict[str, Any]] = None,
    unexpected_save_marker: bool = False,
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
        unexpected_save_marker=unexpected_save_marker,
    )


def handle_provider_failure(
    plan: DailyTurnPlan,
    *,
    error_code: str,
    resident: Optional[Any] = None,
) -> dict[str, Any]:
    return abort_daily_turn(plan, error_code=error_code, resident=resident, respawn=True)


def reprepare_after_hot_cold_mismatch(
    plan: DailyTurnPlan,
    *,
    resident: Optional[Any],
    static_system: str = '',
    static_system_sha256: str = '',
    persona_sha256: str = '',
    provider: str = 'claude_code',
    model: str = '',
) -> DailyTurnPlan:
    """Single allowed reprepare after ensure_alive cold surprise."""
    _release_lease(plan)
    if resident is not None:
        close_local_resident_if_bound(resident, expected_key=plan.resident_key)
    if is_epoch_token_current(plan):
        dc.respawn_daily_resident(plan.context_id, db_path=plan.db_path)
    return prepare_daily_turn(
        user_message_id=plan.user_message_id,
        chat_id=plan.chat_id,
        request_id=plan.request_id,
        db_path=plan.db_path,
        now=plan.user_created_at,
        origin_local_day=plan.origin_local_day or plan.local_day,
        lease_owner=plan.lease_owner,
        resident=resident,
        static_system=static_system,
        static_system_sha256=static_system_sha256,
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
        _cold_reprepare=True,
    )
