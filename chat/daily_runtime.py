"""Daily Soft Window R1 — formal Claude Code resident orchestration."""
from __future__ import annotations

import datetime
import copy
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import cc_resident

from chat import daily_context as dc
from chat import daily_history as dh
from chat import context_window as cw
from chat.attachment_contract import (
    provider_current_turn_attachment_parts,
    provider_current_turn_attachments,
)
from chat.claude_event_mapping import MappingPassRequest, MappingPassResult, run_mapping_pass
from chat.daily_context import (
    ConflictError,
    DEFAULT_CHAT_ID,
    DeferredError,
    StaleOriginDayError,
    chat_day_for_timestamp,
    make_resident_key,
)
from chat.session_registry import (
    SCAN_STATUS_BLOCKED,
    SessionRegistryError,
    get_context_claude_session,
    register_context_claude_session,
)
from chat.capacity_swap_runtime import (
    CAPACITY_BOUNDARY_REPRESENTATION,
    CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1,
    CapacitySwapStagedHooks,
    effective_static_system_for_registry,
    is_capacity_swap_reason,
    note_same_context_last_good,
    register_capacity_swap_generation,
    resolve_finalize_registry_source,
    run_capacity_swap_handoff,
    try_restore_same_context_last_good,
    with_capacity_boundary_suffix,
)
from tools.cc_jsonl_usage import snapshot_session_jsonl
from tools.lease_signer import issue_turn_lease

logger = logging.getLogger(__name__)

NL = chr(10)
CONTEXT_PROFILE = 'daily_window'
DEFAULT_LEASE_TTL = 480
LEASE_HEARTBEAT_INTERVAL = 50
WORKER_ID = '%s:%s' % (socket.gethostname(), os.getpid())
SAVE_RE = re.compile(r'\[\[SAVE(?::[^\]]+)?\]\]', re.IGNORECASE)
DAILY_TOOL_PROFILE = cc_resident.TOOL_PROFILE_UH_A0


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
    """Legacy Gateway catch target; Daily no longer raises this for tool_use."""

    def __init__(self, message: str = 'legacy daily tool fence pending'):
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


class SwitchInProgressRuntimeError(DailyRuntimeError):
    def __init__(self, message: str = 'switch_in_progress'):
        super().__init__(message, error_code='switch_in_progress', retryable=True)


class FirstTurnFinalizePendingError(DailyRuntimeError):
    def __init__(self, message: str = 'first-turn finalize pending'):
        super().__init__(
            message,
            error_code='FIRST_TURN_FINALIZE_PENDING',
            retryable=True,
        )


@dataclass
class LocalResidentBinding:
    resident_key: str
    context_id: int
    context_epoch: int
    resident_generation: int
    bound_cursor_message_id: Optional[int]
    process_generation: int
    tool_profile: str
    claude_session_id: Optional[str] = None


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
    feedback_lines: tuple[str, ...] = ()
    feedback_ids: tuple[int, ...] = ()
    user_content: str = ''
    user_image_url: str = ''
    user_attachments: tuple[dict[str, str], ...] = ()
    provider_display_thinking_suffix: str = field(default='', repr=False)
    lease_acquired: bool = False
    lease_released: bool = False
    db_path: Optional[str] = None
    worker_id: str = WORKER_ID
    tool_profile: str = DAILY_TOOL_PROFILE
    turn_lease: dict[str, Any] = field(default_factory=dict)
    user_created_at: Optional[datetime.datetime] = None
    origin_local_day: str = ''
    turn_started_at: Optional[datetime.datetime] = None
    transcript_cwd: str = ''
    transcript_path: Optional[str] = None
    transcript_start_offset: Optional[int] = None
    transcript_end_offset: Optional[int] = None
    transcript_claude_session_id: Optional[str] = None
    transcript_process_generation: Optional[int] = None
    transcript_observation_error_code: Optional[str] = None
    _resident_close_fn: Optional[Callable[[], None]] = field(default=None, repr=False)


_LOCAL_BINDING: Optional[LocalResidentBinding] = None


def reset_bindings_for_tests() -> None:
    global _LOCAL_BINDING
    _LOCAL_BINDING = None
    set_owner_cursor_write_hook_for_tests(None)
    try:
        from chat.context_window import clear_pending_old_resident_close_for_tests
        clear_pending_old_resident_close_for_tests()
    except Exception:
        pass


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
        cols = {str(r[1]) for r in conn.execute('PRAGMA table_info(chat_messages)').fetchall()}
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('image_url', 'file_url', 'file_name', 'attachments'):
            if optional in cols:
                select_cols.append(optional)
        row = conn.execute(
            'SELECT %s FROM chat_messages WHERE id=?' % ', '.join(select_cols),
            (int(message_id),),
        ).fetchone()
        if row is None:
            raise DailyRuntimeError('user message not found: %s' % message_id, error_code='user_message_missing')
        content = str(row['content'] or '').strip()
        image_url = str(row['image_url'] or '').strip() if 'image_url' in row.keys() else ''
        attachments = provider_current_turn_attachments(
            row['attachments'] if 'attachments' in row.keys() else [],
            legacy_file_url=row['file_url'] if 'file_url' in row.keys() else '',
            legacy_file_name=row['file_name'] if 'file_name' in row.keys() else '',
            legacy_image_url=image_url,
        )
        if not content and any(item['type'] == 'image' for item in attachments):
            content = '[image]'
        created_at = str(row['created_at'] or '').strip()
        return {
            'id': int(row['id']),
            'content': content,
            'image_url': image_url,
            'attachments': attachments,
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


def _normalize_feedback_snapshot(lines: Any, ids: Any) -> tuple[tuple[str, ...], tuple[int, ...]]:
    clean_lines = tuple(
        str(line).strip() for line in (lines or ()) if str(line).strip()
    )
    clean_ids = []
    for value in ids or ():
        try:
            value = int(value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in clean_ids:
            clean_ids.append(value)
    return clean_lines, tuple(clean_ids)


def _peek_feedback_snapshot() -> tuple[tuple[str, ...], tuple[int, ...]]:
    try:
        import command_store
        return _normalize_feedback_snapshot(*command_store.peek_feedback())
    except Exception:
        logger.warning('daily feedback peek failed; continuing without feedback', exc_info=True)
        return (), ()


def _format_task_feedback(lines: tuple[str, ...]) -> str:
    if not lines:
        return ''
    return (
        '\n## 任务完成反馈\n'
        + '\n'.join('- ' + line for line in lines)
        + '\n（这是浮窗自己记录回传的，不是她手动告诉你的。她这次开口了，'
          '你可以顺嘴提一句——用时、快慢、有没有取消，按你的性子说，别像报数据。）'
    )


def format_resident_turn_content(
    *,
    assembly: dict[str, Any],
    user_content: str,
    is_cold: bool,
    is_respawn: bool,
    user_image_url: str = '',
    user_attachments: Optional[list[dict[str, str]]] = None,
    provider_display_thinking_suffix: str = '',
    reality_time_anchor: str = '',
) -> Any:
    """Assemble resident turn content.

    text-only → str (unchanged contract)
    with image → multimodal list accepted by Claude Code stream-json
    """
    cold_like = bool(is_cold or is_respawn)
    attachment_value = list(user_attachments or [])
    legacy_image_url = ''
    if not attachment_value and str(user_image_url or '').strip():
        legacy_image_url = str(user_image_url or '').strip()
    attachment_parts = provider_current_turn_attachment_parts(
        attachment_value,
        legacy_image_url=legacy_image_url,
    )
    has_image = any(part.get('type') == 'image' for part in attachment_parts)
    # Real vision input replaces the textual [image] placeholder.
    turn_user_text = str(user_content or '')
    if has_image and turn_user_text.strip() == '[image]':
        turn_user_text = ''

    time_anchor = str(reality_time_anchor or '').strip()
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
    task_feedback = str(assembly.get('task_feedback') or '').strip()
    if task_feedback:
        prefix_parts.append(task_feedback)
    history = assembly.get('current_day_history') or []
    prefix = NL.join(p for p in prefix_parts if p)
    if cold_like and history:
        history_text = _format_history_messages(history)
        body = (
            '以下是本聊天日内的正式对话记录：' + NL + NL
            + history_text
        )
        if time_anchor:
            body += NL + NL + time_anchor
        body += NL + NL + '请回复最后一条用户消息。' + NL + NL + turn_user_text
        if prefix:
            text = prefix + NL + NL + body
        else:
            text = body
    elif history:
        history_text = _format_history_messages(history)
        replay = '【新增正式对话】' + NL + history_text + NL + NL
        if prefix:
            text = prefix + NL + NL + replay + turn_user_text
        else:
            text = replay + turn_user_text
    elif prefix:
        if cold_like and time_anchor:
            text = prefix + NL + NL + time_anchor + NL + NL + turn_user_text
        else:
            text = prefix + NL + NL + turn_user_text
    elif cold_like and time_anchor:
        text = time_anchor + NL + NL + turn_user_text
    else:
        text = turn_user_text

    if not attachment_parts:
        content = text
    else:
        from chat.cc_vision_bridge import build_claude_user_content
        content = build_claude_user_content(
            text=text,
            attachment_parts=attachment_parts,
        )
    from chat.display_thinking import append_display_thinking_suffix
    return append_display_thinking_suffix(
        content, provider_display_thinking_suffix,
    )


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


_HANDOFF_LOCK = threading.Lock()

# Empty source (boundary=0, no selected carryover): no positive DB watermark exists.
# Bind leaves cursor unset; hot is impossible until a positive watermark is written.
# Never claim hot with cursor=None.
EMPTY_WINDOW_CURSOR_BOOTSTRAP = 'empty_window_cursor_bootstrap'

# Test-only hook: called inside owner+cursor txn after owner upsert ('after_owner').
_OWNER_CURSOR_WRITE_HOOK = None


def handoff_lock():
    return _HANDOFF_LOCK


def forged_history_watermark(result):
    """Cursor must match forged history watermark.

    - selected_message_ids present → last selected id
    - count=0 with positive source boundary → that boundary id
    - boundary=0 empty window → None (bootstrap / fail-closed for hot)
    """
    selected = result.get('selected_message_ids') or []
    if selected:
        return int(selected[-1])
    boundary = int(result.get('boundary_message_id') or 0)
    if boundary > 0:
        return boundary
    return None


def set_owner_cursor_write_hook_for_tests(hook):
    global _OWNER_CURSOR_WRITE_HOOK
    _OWNER_CURSOR_WRITE_HOOK = hook


def build_target_resident_binding(
    *,
    staged_resident,
    result,
    chat_id=DEFAULT_CHAT_ID,
    tool_profile=None,
):
    """Compute target LocalResidentBinding payload (no side effects)."""
    profile = str(tool_profile or DAILY_TOOL_PROFILE)
    target_id = int(result['target_context_id'])
    target_epoch = int(result['target_context_epoch'])
    target_gen = int(result.get('resident_generation') or 1)
    key = make_resident_key(
        chat_id=chat_id,
        context_epoch=target_epoch,
        resident_generation=target_gen,
    )
    process_generation = int(getattr(staged_resident, 'generation', 1) or 1)
    cursor = forged_history_watermark(result)
    session_id = result.get('claude_session_id') or getattr(
        staged_resident, 'session_id', None,
    )
    if hasattr(staged_resident, '_tool_profile'):
        staged_resident._tool_profile = profile
    return LocalResidentBinding(
        resident_key=key,
        context_id=target_id,
        context_epoch=target_epoch,
        resident_generation=target_gen,
        bound_cursor_message_id=cursor,
        process_generation=process_generation,
        tool_profile=profile,
        claude_session_id=str(session_id) if session_id else None,
    )


def write_target_resident_db_metadata(binding, *, db_path=None):
    """Write owner + cursor in one transaction (no LocalResidentBinding change)."""
    conn = dc._connect(db_path)
    try:
        now_s = (
            datetime.datetime.utcnow() + datetime.timedelta(hours=dc.TZ_OFFSET_HOURS)
        ).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'INSERT INTO daily_resident_owners '
            '(context_id, resident_generation, worker_id, resident_key, '
            'bound_cursor_message_id, process_generation, updated_at) '
            'VALUES (?,?,?,?,?,?,?) '
            'ON CONFLICT(context_id, resident_generation) DO UPDATE SET '
            'worker_id=excluded.worker_id, resident_key=excluded.resident_key, '
            'bound_cursor_message_id=excluded.bound_cursor_message_id, '
            'process_generation=excluded.process_generation, '
            'updated_at=excluded.updated_at',
            (
                int(binding.context_id), int(binding.resident_generation),
                WORKER_ID, binding.resident_key,
                binding.bound_cursor_message_id,
                int(binding.process_generation), now_s,
            ),
        )
        hook = _OWNER_CURSOR_WRITE_HOOK
        if hook is not None:
            hook('after_owner')
        if binding.bound_cursor_message_id is not None:
            cursor_id = int(binding.bound_cursor_message_id)
            if cursor_id <= 0:
                raise ValueError('forged watermark cursor must be positive')
            conn.execute(
                'INSERT INTO daily_resident_cursors '
                '(context_id, resident_generation, history_cursor_message_id) '
                'VALUES (?,?,?) '
                'ON CONFLICT(context_id, resident_generation) DO UPDATE SET '
                'history_cursor_message_id=excluded.history_cursor_message_id, '
                "updated_at=datetime('now','+8 hours')",
                (
                    int(binding.context_id), int(binding.resident_generation),
                    cursor_id,
                ),
            )
        else:
            conn.execute(
                'DELETE FROM daily_resident_cursors '
                'WHERE context_id=? AND resident_generation=?',
                (int(binding.context_id), int(binding.resident_generation)),
            )
            logger.info(
                'context switch empty-window cursor bootstrap context_id=%s gen=%s state=%s',
                binding.context_id, binding.resident_generation,
                EMPTY_WINDOW_CURSOR_BOOTSTRAP,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_target_resident_db_metadata(
    *,
    context_id,
    resident_generation,
    db_path=None,
):
    """Best-effort clear target owner/cursor after a failed post-swap bind."""
    conn = dc._connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'DELETE FROM daily_resident_owners '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        )
        conn.execute(
            'DELETE FROM daily_resident_cursors '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception(
            'rollback_target_resident_db_metadata failed ctx=%s gen=%s',
            context_id, resident_generation,
        )
        raise
    finally:
        conn.close()


def target_resident_binding_matches(
    result,
    *,
    session_id=None,
    holder=None,
    expected_resident=None,
    db_path=None,
):
    """True only when handoff is fully installed (not merely context id overlap).

    Handoff-complete ≠ hot-ready: boundary=0 bootstrap leaves cursor unset and
    must not be treated as hot by ``_can_hot_turn``.
    """
    binding = get_local_binding()
    if binding is None:
        return False
    target_id = int(result['target_context_id'])
    target_epoch = int(result['target_context_epoch'])
    target_gen = int(result.get('resident_generation') or 1)
    if int(binding.context_id) != target_id:
        return False
    if int(binding.context_epoch) != target_epoch:
        return False
    if int(binding.resident_generation) != target_gen:
        return False
    if str(binding.tool_profile or '') != str(DAILY_TOOL_PROFILE):
        return False

    want_sid = str(
        session_id
        or result.get('claude_session_id')
        or '',
    ).strip()
    have_sid = str(binding.claude_session_id or '').strip()
    if not want_sid or not have_sid or want_sid != have_sid:
        return False

    watermark = forged_history_watermark(result)
    if watermark is not None:
        if binding.bound_cursor_message_id is None:
            return False
        if int(binding.bound_cursor_message_id) != int(watermark):
            return False
    else:
        if binding.bound_cursor_message_id is not None:
            return False

    current = None
    if holder is not None and hasattr(holder, 'get'):
        current = holder.get()
    elif expected_resident is not None:
        current = expected_resident
    if holder is not None or expected_resident is not None:
        if current is None:
            return False
        if expected_resident is not None and current is not expected_resident:
            return False
        if not _resident_is_alive(current):
            return False
        cur_sid = str(getattr(current, 'session_id', None) or '').strip()
        if cur_sid != want_sid:
            return False
        if int(getattr(current, 'generation', 0) or 0) != int(binding.process_generation):
            return False
        if str(getattr(current, 'tool_profile', '') or '') != str(DAILY_TOOL_PROFILE):
            return False

    owner = dc.get_resident_owner(target_id, target_gen, db_path=db_path)
    if owner is None:
        return False
    if str(owner.get('worker_id') or '') != str(WORKER_ID):
        return False
    if str(owner.get('resident_key') or '') != str(binding.resident_key):
        return False
    if int(owner.get('process_generation') or 0) != int(binding.process_generation):
        return False
    owner_cursor = owner.get('bound_cursor_message_id')
    if watermark is not None:
        if owner_cursor is None or int(owner_cursor) != int(watermark):
            return False
    elif owner_cursor is not None:
        return False

    db_cursor = dc.get_resident_history_cursor(
        target_id, target_gen, db_path=db_path,
    )
    if watermark is not None:
        if db_cursor is None or int(db_cursor) != int(watermark):
            return False
        if int(binding.bound_cursor_message_id) != int(db_cursor):
            return False
    else:
        if db_cursor is not None:
            return False
    return True


def install_target_resident_after_swap(
    *,
    holder,
    staged_resident,
    result,
    chat_id=DEFAULT_CHAT_ID,
    tool_profile=None,
    db_path=None,
):
    """Swap formal holder to staged, then write metadata.

    On any post-swap failure: restore holder + previous binding and roll back
    target owner/cursor inside the caller's handoff lock.
    Returns the previous (old) resident handle; caller closes it only after
    ``mark_intent_committed``.
    """
    sid = str(
        getattr(staged_resident, 'session_id', None)
        or result.get('claude_session_id')
        or '',
    )
    if target_resident_binding_matches(
        result,
        session_id=sid,
        holder=holder,
        expected_resident=holder.get() if hasattr(holder, 'get') else None,
        db_path=db_path,
    ):
        current = holder.get() if hasattr(holder, 'get') else staged_resident
        binding = build_target_resident_binding(
            staged_resident=current,
            result=result,
            chat_id=chat_id,
            tool_profile=tool_profile,
        )
        write_target_resident_db_metadata(binding, db_path=db_path)
        set_local_binding(binding)
        return None

    prev_binding = get_local_binding()
    binding = build_target_resident_binding(
        staged_resident=staged_resident,
        result=result,
        chat_id=chat_id,
        tool_profile=tool_profile,
    )
    old = holder.swap(staged_resident)
    try:
        write_target_resident_db_metadata(binding, db_path=db_path)
        set_local_binding(binding)
    except Exception:
        try:
            holder.swap(old)
        except Exception:
            logger.exception('failed to restore old resident after partial swap')
        set_local_binding(prev_binding)
        try:
            rollback_target_resident_db_metadata(
                context_id=int(binding.context_id),
                resident_generation=int(binding.resident_generation),
                db_path=db_path,
            )
        except Exception:
            logger.exception('failed to rollback target owner/cursor after partial swap')
        raise
    return old


def bind_target_resident_after_switch(
    *,
    staged_resident,
    result,
    chat_id=DEFAULT_CHAT_ID,
    tool_profile=None,
    db_path=None,
):
    """Write owner+cursor then LocalResidentBinding (offline / no-holder path)."""
    binding = build_target_resident_binding(
        staged_resident=staged_resident,
        result=result,
        chat_id=chat_id,
        tool_profile=tool_profile,
    )
    write_target_resident_db_metadata(binding, db_path=db_path)
    set_local_binding(binding)
    return binding


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
        'transcript_mapping_status': 'NOT_ATTEMPTED',
        'transcript_mapping_error_code': None,
        'transcript_mapping_event_count': 0,
        'transcript_mapping_scan_offset': None,
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
    turn_lease: dict[str, Any],
    user_created_at: Optional[datetime.datetime] = None,
    origin_local_day: str = '',
    turn_started_at: Optional[datetime.datetime] = None,
    history_token_budget: Optional[int] = None,
    user_image_url: str = '',
    user_attachments: Optional[list[dict[str, str]]] = None,
    feedback_snapshot: Optional[tuple[tuple[str, ...], tuple[int, ...]]] = None,
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
    provider_sid = None
    if resident is not None and _resident_is_alive(resident):
        provider_sid = str(getattr(resident, 'session_id', None) or '').strip() or None
    if feedback_snapshot is None:
        feedback_snapshot = _peek_feedback_snapshot()
    feedback_lines, feedback_ids = feedback_snapshot
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
        history_token_budget=history_token_budget,
        provider_claude_session_id=provider_sid,
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
    manifest['feedback_ids'] = list(feedback_ids)
    task_feedback = _format_task_feedback(feedback_lines)
    if task_feedback:
        assembly['task_feedback'] = task_feedback
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
        feedback_lines=tuple(feedback_lines),
        feedback_ids=tuple(feedback_ids),
        user_content=user_content,
        user_image_url=str(user_image_url or ''),
        user_attachments=tuple(dict(item) for item in (user_attachments or [])),
        lease_acquired=lease_acquired,
        db_path=db_path,
        turn_lease=copy.deepcopy(turn_lease),
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
    _turn_lease: Optional[dict[str, Any]] = None,
    _feedback_snapshot: Optional[tuple[tuple[str, ...], tuple[int, ...]]] = None,
    _cold_reprepare: bool = False,
    _capacity_swap_reprepare: bool = False,
) -> DailyTurnPlan:
    if not dc.enabled():
        raise DailyRuntimeError('DAILY_SOFT_WINDOW_ENABLED=0', error_code='daily_disabled')

    feedback_snapshot = (
        _feedback_snapshot
        if _feedback_snapshot is not None
        else _peek_feedback_snapshot()
    )
    req_id = str(request_id or uuid.uuid4())
    owner = str(lease_owner or req_id)
    turn_lease = (
        copy.deepcopy(_turn_lease)
        if _turn_lease is not None
        else issue_turn_lease(
            turn_id=req_id,
            turn_mode='chat',
            issued_from='default_policy',
        )
    )
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
        manual_mode = cw.enabled()
        if dc.has_active_provider_turn_lease(chat_id, db_path=db_path, now=lease_now):
            if not manual_mode:
                prior = dc.get_latest_active_context(chat_id, db_path=db_path)
                if prior and str(prior.get('local_day') or '') != origin_day:
                    raise DeferredError('provider request in flight; rollover deferred')

        if manual_mode:
            if cw.has_active_switch_intent(chat_id, db_path=db_path):
                raise SwitchInProgressRuntimeError('switch_in_progress')
            # Fail-closed: committed first-turn with assistant persisted but
            # checkpoint incomplete must block ordinary daily turns even after
            # the temporary first-turn lease TTL (480s) expires.
            pending = cw.get_first_turn_finalize_pending(chat_id, db_path=db_path)
            if pending is not None:
                raise FirstTurnFinalizePendingError(
                    'first-turn finalize pending; explicit finalize retry required',
                )
            try:
                ctx = cw.get_current_context_window(
                    chat_id=chat_id,
                    db_path=db_path,
                    now=context_wall_now,
                )
            except cw.NoOpenContextWindowError as exc:
                raise DailyRuntimeError(
                    'no open context window',
                    error_code='no_open_context_window',
                    retryable=False,
                ) from exc
        else:
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

        context_id = int(ctx['id'])

        if not manual_mode:
            dc.ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)
        refreshed = dc.get_daily_context_by_id(context_id, db_path=db_path) or ctx
        context_local_day = str(refreshed.get('local_day') or origin_day)
        context_epoch = int(refreshed['context_epoch'])
        resident_generation = int(refreshed['resident_generation'])
        resident_key = make_resident_key(
            chat_id=chat_id, context_epoch=context_epoch, resident_generation=resident_generation,
        )

        if resident is not None:
            # Capacity Swap reprepare is a known brief handoff state: the live
            # process may already be target gen N+1 while LocalResidentBinding
            # is still source gen N. Do not apply the ordinary stale-resident
            # kill here — target binding is committed only after Registry publish.
            if not _capacity_swap_reprepare:
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
        if _capacity_swap_reprepare:
            # Same-context Capacity Swap: forged transcript already carries history.
            # Force hot-like assembly (no cold history / carryover / handoff replay).
            is_cold = False
            is_respawn = False
            turn_kind = 'capacity_swap'
        else:
            is_respawn = _cold_reprepare
            turn_kind = 'respawn' if is_respawn else ('hot' if not is_cold else 'cold')

        plan = _assemble_plan(
            req_id=req_id,
            owner=owner,
            chat_id=chat_id,
            local_day=context_local_day,
            refreshed=refreshed,
            user_message_id=int(user_message_id),
            user_content=str(user_row.get('content') or ''),
            user_image_url=str(user_row.get('image_url') or ''),
            user_attachments=list(user_row.get('attachments') or []),
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
            turn_lease=turn_lease,
            user_created_at=user_created_at,
            origin_local_day=origin_day,
            turn_started_at=turn_started_at,
            feedback_snapshot=feedback_snapshot,
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


def _registered_generation_requires_respawn(
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
) -> bool:
    """True when stdin must not proceed on the current resident_generation.

    Registry absent → allow first session registration for this generation.
    Registry present + upcoming/new Claude session → bump generation first.
    """
    decision = peek_registered_respawn_decision(plan, resident, static_system)
    return bool(decision.get('requires_respawn'))


def peek_registered_respawn_decision(
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
) -> dict[str, Any]:
    """Read-only door-lock decision with concrete reason (capacity vs generic).

    Uses effective_system for capacity_swap generations so suffix persistence
    does not spuriously trip ``system_changed``.
    """
    registry = get_context_claude_session(
        int(plan.context_id),
        int(plan.resident_generation),
        db_path=plan.db_path,
    )
    if registry is None:
        return {
            'requires_respawn': False,
            'reason': None,
            'registry': None,
            'effective_system': str(static_system or ''),
            'capacity_swap': False,
        }

    effective = effective_static_system_for_registry(static_system, registry)
    peek = getattr(resident, 'peek_respawn_reason', None)
    reason = None
    if callable(peek):
        reason = peek(effective, tool_profile=plan.tool_profile)

    if reason:
        return {
            'requires_respawn': True,
            'reason': reason,
            'registry': registry,
            'effective_system': effective,
            'capacity_swap': is_capacity_swap_reason(reason),
        }

    live_sid = str(getattr(resident, 'session_id', None) or '').strip()
    reg_sid = str(registry.get('claude_session_id') or '').strip()
    if not live_sid or live_sid != reg_sid:
        return {
            'requires_respawn': True,
            'reason': 'session_identity_mismatch',
            'registry': registry,
            'effective_system': effective,
            'capacity_swap': False,
        }
    return {
        'requires_respawn': False,
        'reason': None,
        'registry': registry,
        'effective_system': effective,
        'capacity_swap': False,
    }


def reprepare_after_capacity_swap(
    plan: DailyTurnPlan,
    *,
    resident: Optional[Any],
    static_system: str = '',
    static_system_sha256: str = '',
    persona_sha256: str = '',
    provider: str = 'claude_code',
    model: str = '',
    cursor_watermark: Optional[int] = None,
) -> DailyTurnPlan:
    """Same-context gen bump already done; re-claim lease without cold history replay.

    Cursor/owner seed is mandatory. Any seed failure fail-closes — callers must
    treat this as Capacity Swap failure and fall back (last-good / cold). Never
    continue into ``_capacity_swap_reprepare`` hot-like assembly without a seed.
    """
    _release_lease(plan)
    # Seed cursor for the new generation so hot assembly does not replay forged rounds.
    watermark = cursor_watermark if cursor_watermark is not None else plan.cursor_before
    if watermark is None:
        # No prior cursor (rare): pin just before CURRENT user so hot assembly is legal.
        watermark = max(0, int(plan.user_message_id) - 1)

    refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=plan.db_path) or {}
    gen = int(refreshed.get('resident_generation') or 0)
    if gen <= 0:
        raise DailyRuntimeError(
            'capacity swap cursor seed: resident_generation missing',
            error_code='capacity_swap_cursor_seed_failed',
        )
    try:
        dc.upsert_resident_owner(
            plan.context_id,
            gen,
            worker_id=WORKER_ID,
            resident_key=make_resident_key(
                chat_id=plan.chat_id,
                context_epoch=int(refreshed.get('context_epoch') or plan.context_epoch),
                resident_generation=gen,
            ),
            bound_cursor_message_id=int(watermark),
            process_generation=int(getattr(resident, 'generation', 0) or 0) if resident else None,
            db_path=plan.db_path,
        )
        conn = dc._connect(plan.db_path)
        try:
            conn.execute(
                'INSERT INTO daily_resident_cursors '
                '(context_id, resident_generation, history_cursor_message_id) '
                'VALUES (?,?,?) '
                'ON CONFLICT(context_id, resident_generation) DO UPDATE SET '
                'history_cursor_message_id=excluded.history_cursor_message_id, '
                "updated_at=datetime('now','+8 hours')",
                (int(plan.context_id), gen, int(watermark)),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except DailyRuntimeError:
        raise
    except Exception as exc:
        logger.exception('capacity swap cursor seed failed')
        raise DailyRuntimeError(
            'capacity swap cursor seed failed',
            error_code='capacity_swap_cursor_seed_failed',
        ) from exc

    return prepare_daily_turn(
        user_message_id=plan.user_message_id,
        chat_id=plan.chat_id,
        request_id=plan.request_id,
        db_path=plan.db_path,
        now=plan.user_created_at,
        origin_local_day=plan.origin_local_day or plan.local_day,
        lease_owner=plan.lease_owner,
        resident=resident,
        _turn_lease=plan.turn_lease,
        _feedback_snapshot=(tuple(plan.feedback_lines), tuple(plan.feedback_ids)),
        static_system=static_system,
        static_system_sha256=static_system_sha256,
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
        _capacity_swap_reprepare=True,
    )


# Process/session identity transferred from staged (the new Claude process).
_CAPACITY_SWAP_PROCESS_IDENTITY = (
    '_proc', '_system_text', '_session_id', '_cold', '_generation',
    '_tool_profile', '_model_identity', '_history_rewrite_epoch', '_last_used',
)
_CAPACITY_SWAP_PUBLIC_MIRRORS = (
    'session_id', 'generation', 'tool_profile', 'cwd', '_peek_reason',
)
# Generation-scoped runtime metadata owned by ResidentSession._reset_session_meta.
# Adoption must NOT keep the old live values of these across CapSwap.
_CAPACITY_SWAP_GENERATION_META = (
    '_resident_turn_count',
    '_last_round_context',
    '_max_round_context',
    '_pending_respawn_reason',
    '_turns_since_respawn',
    '_last_state_snapshot',
    '_last_state_send_snapshot',
    '_last_successful_lean_state',
    '_last_state_anchor_generation',
    '_last_state_schema_version',
    '_turns_since_state_anchor',
    '_state_delta_chars_since_anchor',
    '_last_state_anchor_version',
    '_committed_file_hashes',
    '_pending_file_hashes',
    '_last_group_message_id',
    '_group_cursor_initialized',
    '_last_rel_fingerprint',
    '_turns_since_rel_sent',
    '_last_rel_mood',
    '_keepwarm_lease_expires_at',
    '_tool_surface_snapshot',
)


def _snapshot_resident_attr(value: Any) -> Any:
    if isinstance(value, (dict, list, set)):
        return copy.deepcopy(value)
    return value


def _snapshot_capacity_swap_live_resident(live_resident: Any) -> dict[str, Any]:
    attrs = (
        _CAPACITY_SWAP_PROCESS_IDENTITY
        + _CAPACITY_SWAP_PUBLIC_MIRRORS
        + _CAPACITY_SWAP_GENERATION_META
    )
    out: dict[str, Any] = {}
    for attr in attrs:
        if hasattr(live_resident, attr):
            try:
                out[attr] = _snapshot_resident_attr(getattr(live_resident, attr))
            except Exception:
                pass
    return out


def install_capacity_swap_into_live_resident(
    *,
    live_resident: Any,
    staged_resident: Any,
) -> dict[str, Any]:
    """Adopt staged Claude process into the live holder without killing old proc.

    Single adoption semantic:
      1) transfer process/session identity from staged
      2) reset generation-scoped runtime meta via ``_reset_session_meta``
         (same helper staged spawn uses) so old-generation state/file/group/
         relationship dedupe cannot leak into the new generation
      3) copy staged tool-surface after reset

    Returns ``{old_proc, old_attrs, old_binding}`` for deferred close /
    pre-flush rollback (process + LocalResidentBinding restored together).
    """
    old_attrs = _snapshot_capacity_swap_live_resident(live_resident)
    old_proc = old_attrs.get('_proc', getattr(live_resident, '_proc', None))
    old_binding = get_local_binding()

    for attr in _CAPACITY_SWAP_PROCESS_IDENTITY:
        if hasattr(staged_resident, attr) and hasattr(live_resident, attr):
            try:
                setattr(live_resident, attr, getattr(staged_resident, attr))
            except Exception:
                pass
    for attr in _CAPACITY_SWAP_PUBLIC_MIRRORS:
        if hasattr(staged_resident, attr):
            try:
                setattr(live_resident, attr, getattr(staged_resident, attr))
            except Exception:
                pass

    reset = getattr(live_resident, '_reset_session_meta', None)
    if callable(reset):
        reason = getattr(staged_resident, '_pending_respawn_reason', None) or 'capacity_swap'
        reset(respawn_reason=reason)
        if hasattr(staged_resident, '_tool_surface_snapshot'):
            try:
                live_resident._tool_surface_snapshot = copy.deepcopy(
                    getattr(staged_resident, '_tool_surface_snapshot') or {},
                )
            except Exception:
                live_resident._tool_surface_snapshot = {}
    else:
        # Test fakes: prefer staged generation meta; otherwise clear old-gen values.
        for attr in _CAPACITY_SWAP_GENERATION_META:
            if hasattr(staged_resident, attr):
                try:
                    setattr(
                        live_resident,
                        attr,
                        _snapshot_resident_attr(getattr(staged_resident, attr)),
                    )
                except Exception:
                    pass
            elif hasattr(live_resident, attr):
                try:
                    if attr.endswith('_hashes'):
                        setattr(live_resident, attr, set())
                    elif attr.endswith('_snapshot') or attr == '_tool_surface_snapshot':
                        setattr(live_resident, attr, {})
                    elif attr == '_last_state_anchor_generation':
                        setattr(live_resident, attr, -1)
                    elif attr in {
                        '_last_group_message_id',
                        '_turns_since_respawn',
                        '_turns_since_state_anchor',
                        '_state_delta_chars_since_anchor',
                        '_turns_since_rel_sent',
                        '_resident_turn_count',
                        '_last_round_context',
                        '_max_round_context',
                    }:
                        setattr(live_resident, attr, 0)
                    elif attr in {'_group_cursor_initialized', '_last_successful_lean_state'}:
                        setattr(live_resident, attr, False)
                    else:
                        setattr(live_resident, attr, None)
                except Exception:
                    pass

    try:
        staged_resident._proc = None
    except Exception:
        pass
    return {
        'old_proc': old_proc,
        'old_attrs': old_attrs,
        'old_binding': old_binding,
    }


def rollback_capacity_swap_install(
    *,
    live_resident: Any,
    install_state: Optional[dict[str, Any]],
) -> None:
    """Restore pre-install live resident + binding; kill the staged/new process."""
    if not install_state:
        return
    new_proc = getattr(live_resident, '_proc', None)
    old_proc = install_state.get('old_proc')
    old_attrs = dict(install_state.get('old_attrs') or {})
    for attr, value in old_attrs.items():
        try:
            setattr(live_resident, attr, _snapshot_resident_attr(value))
        except Exception:
            pass
    if old_proc is not None:
        try:
            live_resident._proc = old_proc
        except Exception:
            pass
    if new_proc is not None and new_proc is not old_proc:
        try:
            if getattr(new_proc, 'poll', lambda: None)() is None:
                new_proc.kill()
        except Exception:
            pass
    # Restore LocalResidentBinding with the old resident so we never leave
    # gen1 process paired with a gen2 binding (or the reverse) after rollback.
    if 'old_binding' in install_state:
        set_local_binding(install_state.get('old_binding'))


def close_deferred_capacity_swap_old_proc(old_proc: Any) -> None:
    """Kill superseded last-good process after stdin flush (exactly-once locked)."""
    if old_proc is None:
        return
    try:
        poll = getattr(old_proc, 'poll', None)
        if callable(poll) and poll() is None:
            old_proc.kill()
    except Exception:
        pass


def _default_capacity_swap_staged_hooks(live_resident: Any) -> CapacitySwapStagedHooks:
    """Production hooks: spawn a sibling ResidentSession for staged --resume."""
    import cc_resident as _cc

    def spawn_and_health(
        system_text,
        env,
        *,
        resume_session_id,
        jsonl_path,
        expected_sha256,
    ):
        cwd = str(getattr(live_resident, 'cwd', '') or '')
        allowed = str(getattr(live_resident, '_allowed_tools', '') or '')
        mcp = str(getattr(live_resident, '_mcp_config_path', '') or (cwd + '/cc-tools.json'))
        staged = _cc.ResidentSession(cwd, allowed, mcp)
        try:
            staged.spawn_resumable(
                system_text,
                env,
                resume_session_id=str(resume_session_id),
                tool_profile=DAILY_TOOL_PROFILE,
                reason='capacity_swap',
            )
            staged.wait_staged_health(
                jsonl_path=jsonl_path,
                expected_sha256=expected_sha256,
            )
        except Exception:
            try:
                staged._kill(quiet=True)
            except Exception:
                pass
            raise
        return staged

    return CapacitySwapStagedHooks(spawn_and_health=spawn_and_health)


def reprepare_after_registered_session_change(
    plan: DailyTurnPlan,
    *,
    resident: Optional[Any],
    static_system: str = '',
    static_system_sha256: str = '',
    persona_sha256: str = '',
    provider: str = 'claude_code',
    model: str = '',
) -> DailyTurnPlan:
    """Bump resident_generation before a new Claude session under a registered gen."""
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
        _turn_lease=plan.turn_lease,
        _feedback_snapshot=(tuple(plan.feedback_lines), tuple(plan.feedback_ids)),
        static_system=static_system,
        static_system_sha256=static_system_sha256,
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
        _cold_reprepare=True,
    )


def _adopt_reprepared_plan_in_place(
    current: DailyTurnPlan,
    replacement: DailyTurnPlan,
    *,
    resident: Optional[Any],
) -> DailyTurnPlan:
    """Copy replacement state into ``current`` without changing object identity.

    Gateway keeps the original DailyTurnPlan reference across stream → persist →
    cursor CAS → Mapping. Reprepare must therefore mutate that same object.
    """
    provider_suffix = getattr(current, 'provider_display_thinking_suffix', '')
    for f in fields(DailyTurnPlan):
        setattr(current, f.name, getattr(replacement, f.name))
    current.provider_display_thinking_suffix = provider_suffix
    if resident is not None:
        key = current.resident_key
        current._resident_close_fn = (
            lambda k=key: close_local_resident_if_bound(
                resident,
                expected_key=k,
            )
        )
    else:
        current._resident_close_fn = None
    return current


def _capture_transcript_start(plan: DailyTurnPlan, resident: Any) -> None:
    """Capture JSONL start offset after ensure_alive, before send_turn."""
    try:
        plan.transcript_cwd = str(getattr(resident, 'cwd', '') or '')
        plan.transcript_process_generation = int(getattr(resident, 'generation', 0) or 0)
        sid = str(getattr(resident, 'session_id', None) or '').strip() or None
        plan.transcript_claude_session_id = sid
        if sid:
            snap = snapshot_session_jsonl(plan.transcript_cwd, sid)
            if snap is None:
                plan.transcript_observation_error_code = 'transcript_snapshot_failed'
                return
            plan.transcript_path = str(snap.get('path') or '') or None
            plan.transcript_start_offset = int(snap.get('offset') or 0)
        else:
            # Cold session: session id arrives at done; start at byte 0.
            plan.transcript_path = None
            plan.transcript_start_offset = 0
    except Exception:
        logger.warning('transcript start capture failed', exc_info=True)
        plan.transcript_observation_error_code = 'transcript_start_capture_failed'


def _capture_transcript_end(plan: DailyTurnPlan, resident: Any) -> None:
    """Capture JSONL end offset on done, before yielding done upstream."""
    if plan.transcript_observation_error_code:
        return
    try:
        sid = str(getattr(resident, 'session_id', None) or '').strip() or None
        if not sid:
            plan.transcript_observation_error_code = 'transcript_session_id_missing'
            return
        live_gen = int(getattr(resident, 'generation', 0) or 0)
        if (
            plan.transcript_process_generation is not None
            and live_gen != int(plan.transcript_process_generation)
        ):
            plan.transcript_observation_error_code = 'transcript_process_generation_changed'
            return
        start_sid = str(plan.transcript_claude_session_id or '').strip() or None
        if start_sid and start_sid != sid:
            plan.transcript_observation_error_code = 'transcript_session_id_changed'
            return
        plan.transcript_claude_session_id = sid
        snap = snapshot_session_jsonl(plan.transcript_cwd, sid)
        if snap is None:
            plan.transcript_observation_error_code = 'transcript_snapshot_failed'
            return
        end_path = str(snap.get('path') or '') or None
        end_offset = int(snap.get('offset') or 0)
        if plan.transcript_path and end_path and plan.transcript_path != end_path:
            plan.transcript_observation_error_code = 'transcript_path_changed'
            return
        if plan.transcript_start_offset is None:
            plan.transcript_observation_error_code = 'transcript_start_offset_missing'
            return
        if end_offset < int(plan.transcript_start_offset):
            plan.transcript_observation_error_code = 'transcript_offset_invalid'
            return
        plan.transcript_path = end_path
        plan.transcript_end_offset = end_offset
    except Exception:
        logger.warning('transcript end capture failed', exc_info=True)
        plan.transcript_observation_error_code = 'transcript_end_capture_failed'


def _set_transcript_mapping_manifest(
    plan: DailyTurnPlan,
    *,
    status: str,
    error_code: Optional[str] = None,
    event_count: int = 0,
    scan_offset: Optional[int] = None,
) -> None:
    plan.manifest['transcript_mapping_status'] = status
    plan.manifest['transcript_mapping_error_code'] = error_code
    plan.manifest['transcript_mapping_event_count'] = int(event_count)
    plan.manifest['transcript_mapping_scan_offset'] = scan_offset


def finalize_transcript_mapping_after_success(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
) -> dict[str, Any]:
    """Register session + map events after assistant persist and cursor CAS.

    Failures only update mapping manifest fields (BLOCKED). Never raises to
    Gateway, never aborts the turn, never respawns or rolls back chat state.
    """
    try:
        existing = get_context_claude_session(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
        # BLOCKED at/after current turn start stays fail-closed. BLOCKED with a
        # true backlog (scan_offset behind this turn start) must still attempt
        # synchronous catch-up — otherwise one missed map permanently poisons
        # every later successful turn.
        if existing is not None and str(existing.get('scan_status') or '') == SCAN_STATUS_BLOCKED:
            turn_start = plan.transcript_start_offset
            reg_off = existing.get('scan_offset')
            has_backlog = (
                turn_start is not None
                and reg_off is not None
                and int(reg_off) < int(turn_start)
            )
            if not has_backlog:
                _set_transcript_mapping_manifest(
                    plan,
                    status='BLOCKED',
                    error_code=str(existing.get('scan_error_code') or 'registry_blocked'),
                    event_count=0,
                    scan_offset=(
                        int(existing['scan_offset'])
                        if existing.get('scan_offset') is not None
                        else None
                    ),
                )
                return dict(plan.manifest)

        if plan.transcript_observation_error_code:
            _set_transcript_mapping_manifest(
                plan,
                status='BLOCKED',
                error_code=str(plan.transcript_observation_error_code),
                event_count=0,
                scan_offset=None,
            )
            return dict(plan.manifest)

        sid = str(plan.transcript_claude_session_id or '').strip()
        if (
            not sid
            or not str(plan.transcript_cwd or '').strip()
            or plan.transcript_start_offset is None
            or plan.transcript_end_offset is None
            or plan.transcript_process_generation is None
        ):
            _set_transcript_mapping_manifest(
                plan,
                status='BLOCKED',
                error_code='transcript_observation_incomplete',
                event_count=0,
                scan_offset=None,
            )
            return dict(plan.manifest)

        register_context_claude_session(
            context_id=int(plan.context_id),
            context_epoch=int(plan.context_epoch),
            resident_generation=int(plan.resident_generation),
            chat_id=str(plan.chat_id),
            claude_session_id=sid,
            cwd=str(plan.transcript_cwd),
            source=resolve_finalize_registry_source(existing, default='daily_runtime'),
            scan_offset=int(plan.transcript_start_offset),
            process_generation=int(plan.transcript_process_generation),
            transcript_path=plan.transcript_path,
            db_path=plan.db_path,
        )

        result: MappingPassResult = run_mapping_pass(
            MappingPassRequest(
                context_id=int(plan.context_id),
                context_epoch=int(plan.context_epoch),
                resident_generation=int(plan.resident_generation),
                chat_id=str(plan.chat_id),
                user_message_id=int(plan.user_message_id),
                assistant_message_id=int(assistant_message_id),
                expected_start_offset=int(plan.transcript_start_offset),
                observed_end_offset=int(plan.transcript_end_offset),
            ),
            db_path=plan.db_path,
        )
        if result.ok:
            _set_transcript_mapping_manifest(
                plan,
                status='MAPPED',
                error_code=None,
                event_count=len(result.mapped_event_uuids or []),
                scan_offset=result.scan_offset,
            )
        else:
            _set_transcript_mapping_manifest(
                plan,
                status='BLOCKED',
                error_code=str(result.error_code or 'mapping_blocked'),
                event_count=0,
                scan_offset=(
                    int(result.registry['scan_offset'])
                    if result.registry and result.registry.get('scan_offset') is not None
                    else None
                ),
            )
    except SessionRegistryError as exc:
        logger.warning(
            'transcript registry/mapping blocked: %s',
            getattr(exc, 'error_code', exc),
            exc_info=True,
        )
        _set_transcript_mapping_manifest(
            plan,
            status='BLOCKED',
            error_code=str(getattr(exc, 'error_code', None) or 'registry_error'),
            event_count=0,
            scan_offset=None,
        )
    except Exception as exc:
        logger.warning('transcript mapping finalize failed: %s', exc, exc_info=True)
        _set_transcript_mapping_manifest(
            plan,
            status='BLOCKED',
            error_code='mapping_finalize_exception',
            event_count=0,
            scan_offset=None,
        )
    return dict(plan.manifest)


def _rebuild_daily_assembly_with_history_budget(
    plan: DailyTurnPlan,
    *,
    static_system: str,
    history_token_budget: int,
    resident: Optional[Any],
    is_cold: bool,
    is_respawn: bool,
) -> dict[str, Any]:
    """Re-run cold assembly with a smaller history budget (Fence B rebuild)."""
    refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=plan.db_path)
    if not refreshed:
        raise DailyRuntimeError(
            'daily context missing during cold rebuild',
            error_code='daily_context_missing',
        )
    last_state: Optional[dict[str, str]] = None
    cold_like = bool(is_cold or is_respawn)
    provider_sid = None
    if resident is not None and _resident_is_alive(resident):
        provider_sid = str(getattr(resident, 'session_id', None) or '').strip() or None
    assembly = dh.build_daily_window_context(
        chat_id=plan.chat_id,
        daily_context=refreshed,
        current_user_message_id=int(plan.user_message_id),
        static_system=static_system,
        is_cold=is_cold,
        is_respawn=is_respawn,
        last_state_snapshot=last_state,
        inject_handoff=cold_like,
        inject_carryover=cold_like,
        db_path=plan.db_path,
        history_token_budget=history_token_budget,
        provider_claude_session_id=provider_sid,
    )
    # Fence B must reuse the plan's frozen snapshot.  The rebuild is allowed to
    # trim history, but it must not re-peek or silently drop feedback that was
    # already selected for this logical turn.
    task_feedback = _format_task_feedback(tuple(plan.feedback_lines))
    if task_feedback:
        assembly['task_feedback'] = task_feedback
    else:
        assembly.pop('task_feedback', None)
    return assembly


def _apply_daily_cold_prompt_fence(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    static_system: str,
    content: Any,
    is_cold: bool,
    is_respawn: bool,
    reality_time_anchor: str = '',
) -> Any:
    """Whole-prompt Fence B/C for Daily cold/respawn — reuses #218 helpers."""
    from chat.cold_bootstrap_budget import (
        ColdBootstrapOverflow,
        NoBenefitRespawnError,
        cold_prompt_target,
        effective_history_budget,
        estimate_text_tokens,
        estimate_whole_prompt,
        should_refuse_no_benefit_hard_context_respawn,
    )
    from chat.context_lean import cc_history_token_budget

    cold_prompt_target_val = cold_prompt_target()
    cold_history_budget_val = int(
        (plan.assembly.get('manifest') or {}).get('cold_history_budget')
        or cc_history_token_budget()
    )
    cold_history_trimmed_flag = bool(
        (plan.assembly.get('manifest') or {}).get('cold_history_trimmed')
    )
    cold_prompt_estimate = estimate_whole_prompt(static_system, content)

    if cold_prompt_estimate > cold_prompt_target_val:
        history = plan.assembly.get('current_day_history') or []
        history_tokens_est = estimate_text_tokens(_format_history_messages(history))
        non_history_est = max(0, cold_prompt_estimate - history_tokens_est)
        new_budget = effective_history_budget(
            default_history_budget=cold_history_budget_val,
            non_history_estimate=non_history_est,
            cold_target=cold_prompt_target_val,
        )
        rebuilt = _rebuild_daily_assembly_with_history_budget(
            plan,
            static_system=static_system,
            history_token_budget=new_budget,
            resident=resident,
            is_cold=is_cold,
            is_respawn=is_respawn,
        )
        plan.assembly = rebuilt
        content = format_resident_turn_content(
            assembly=plan.assembly,
            user_content=plan.user_content,
            is_cold=is_cold,
            is_respawn=is_respawn,
            user_image_url=plan.user_image_url,
            user_attachments=list(plan.user_attachments),
            provider_display_thinking_suffix=(
                plan.provider_display_thinking_suffix
            ),
            reality_time_anchor=reality_time_anchor,
        )
        cold_prompt_estimate = estimate_whole_prompt(static_system, content)
        cold_history_budget_val = new_budget
        cold_history_trimmed_flag = bool(
            (plan.assembly.get('manifest') or {}).get('cold_history_trimmed')
        )
        plan.manifest.update(dict(plan.assembly.get('manifest') or {}))

    if cold_prompt_estimate > cold_prompt_target_val:
        plan.manifest['cold_budget_overflow'] = True
        plan.manifest['cold_prompt_estimate'] = int(cold_prompt_estimate)
        plan.manifest['cold_prompt_target'] = int(cold_prompt_target_val)
        plan.manifest['cold_history_budget'] = int(cold_history_budget_val)
        plan.manifest['cold_budget_mode'] = 'token_budget'
        raise ColdBootstrapOverflow(
            estimate=cold_prompt_estimate,
            target=cold_prompt_target_val,
            history_budget=cold_history_budget_val,
            cold_history_trimmed=cold_history_trimmed_flag,
        )

    pending_respawn_reason = getattr(resident, 'pending_respawn_reason', None)
    if pending_respawn_reason == 'hard_context':
        pre_spawn_turns = getattr(resident, 'hard_context_pre_spawn_turns', None)
        if should_refuse_no_benefit_hard_context_respawn(
            pre_spawn_turns=pre_spawn_turns,
            cold_prompt_estimate=cold_prompt_estimate,
            last_cold_bootstrap_estimate=int(
                getattr(resident, 'last_cold_bootstrap_estimate', 0) or 0
            ),
            last_cold_bootstrap_generation=int(
                getattr(resident, 'last_cold_bootstrap_generation', 0) or 0
            ),
            current_generation=int(getattr(resident, 'generation', 0) or 0),
        ):
            baseline = int(getattr(resident, 'last_cold_bootstrap_estimate', 0) or 0)
            raise NoBenefitRespawnError(
                new_estimate=cold_prompt_estimate, baseline=baseline,
            )
        clear_storm = getattr(resident, 'clear_hard_context_pre_spawn_turns', None)
        if callable(clear_storm):
            clear_storm()

    note_cold_estimate = getattr(resident, 'note_cold_bootstrap_estimate', None)
    if callable(note_cold_estimate):
        note_cold_estimate(cold_prompt_estimate)

    plan.manifest['cold_prompt_estimate'] = int(cold_prompt_estimate)
    plan.manifest['cold_prompt_target'] = int(cold_prompt_target_val)
    plan.manifest['cold_history_budget'] = int(cold_history_budget_val)
    plan.manifest['cold_history_trimmed'] = bool(cold_history_trimmed_flag)
    plan.manifest['cold_budget_mode'] = 'token_budget'
    plan.manifest['cold_budget_overflow'] = False
    return content


def _attempt_capacity_swap_before_stdin(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    static_system: str,
    env: dict[str, str],
    trigger_reason: str,
    staged_hooks: Optional[CapacitySwapStagedHooks] = None,
    claude_home: Optional[str] = None,
) -> dict[str, Any]:
    """Run Capacity Swap handoff before CURRENT user stdin. Never sends the user.

    Commit order (frozen for failure safety):
      generation +1 → install staged (keep old) → cursor/reprepare → identity
      → register Registry source=capacity_swap → success
    Registry is the pre-stdin commit flag; failures before it leave no target
    Registry row. Old last-good proc stays until CURRENT user stdin flush.
    """
    if not is_capacity_swap_reason(trigger_reason):
        return {'ok': False, 'error_code': 'trigger_not_capacity'}

    hooks = staged_hooks or _default_capacity_swap_staged_hooks(resident)
    handoff = run_capacity_swap_handoff(
        plan=plan,
        trigger_reason=trigger_reason,
        static_system=static_system,
        env=env,
        live_resident=resident,
        staged_hooks=hooks,
        claude_home=claude_home,
    )
    if not handoff.ok or handoff.candidate is None:
        return {
            'ok': False,
            'error_code': handoff.error_code or 'capacity_swap_failed',
            'warnings': list(handoff.warnings),
        }

    staged = getattr(handoff, 'staged_resident', None)
    if staged is None:
        return {'ok': False, 'error_code': 'staged_missing'}

    source_ctx = int(plan.context_id)
    source_epoch = int(plan.context_epoch)
    source_gen = int(plan.resident_generation)
    watermark = plan.cursor_before
    install_state: Optional[dict[str, Any]] = None

    def _fail_kill_staged(code: str, **extra: Any) -> dict[str, Any]:
        try:
            if hasattr(staged, '_kill'):
                staged._kill(quiet=True)
        except Exception:
            pass
        out = {'ok': False, 'error_code': code}
        out.update(extra)
        return out

    # Generation bump (same context_id / epoch). Registry not yet published.
    if is_epoch_token_current(plan):
        dc.respawn_daily_resident(plan.context_id, db_path=plan.db_path)
    refreshed = dc.get_daily_context_by_id(plan.context_id, db_path=plan.db_path) or {}
    target_gen = int(refreshed.get('resident_generation') or 0)
    if target_gen != int(handoff.target_resident_generation):
        return _fail_kill_staged(
            'generation_cas_mismatch',
            source_generation=source_gen,
            target_generation=target_gen,
        )
    if int(refreshed.get('id') or 0) != source_ctx:
        return _fail_kill_staged('context_id_drift')
    if int(refreshed.get('context_epoch') or 0) != source_epoch:
        return _fail_kill_staged('context_epoch_drift')

    cwd = str(getattr(resident, 'cwd', '') or plan.transcript_cwd or '')

    # Transfer staged → live WITHOUT killing old last-good process.
    install_state = install_capacity_swap_into_live_resident(
        live_resident=resident, staged_resident=staged,
    )

    try:
        effective = str(handoff.effective_system or with_capacity_boundary_suffix(static_system))
        replacement = reprepare_after_capacity_swap(
            plan,
            resident=resident,
            static_system=effective,
            static_system_sha256=_sha256_text(effective),
            persona_sha256=plan.manifest.get('persona_sha256') or '',
            provider=str(plan.manifest.get('provider') or 'claude_code'),
            model=str(plan.manifest.get('model') or ''),
            cursor_watermark=watermark,
        )
        _adopt_reprepared_plan_in_place(plan, replacement, resident=resident)
        plan.manifest['capacity_swap'] = True
        plan.manifest['capacity_swap_reason'] = trigger_reason
        plan.manifest['capacity_swap_source_generation'] = source_gen
        plan.manifest['capacity_boundary_representation'] = CAPACITY_BOUNDARY_REPRESENTATION

        if int(plan.context_id) != source_ctx or int(plan.context_epoch) != source_epoch:
            raise DailyRuntimeError(
                'identity drift after capacity swap reprepare',
                error_code='identity_drift_after_reprepare',
            )
        if int(plan.resident_generation) != target_gen:
            raise DailyRuntimeError(
                'generation drift after capacity swap reprepare',
                error_code='generation_drift_after_reprepare',
            )

        # Registry publication is the pre-stdin commit point (after seed/identity).
        register_capacity_swap_generation(
            plan=plan,
            candidate=handoff.candidate,
            process_generation=int(getattr(resident, 'generation', 0) or 0),
            scan_offset=(
                int(Path(str(handoff.jsonl_path)).stat().st_size)
                if handoff.jsonl_path else 0
            ),
            cwd=cwd,
            claude_home=claude_home,
        )
        # Commit target LocalResidentBinding before returning ok so the recursive
        # door never sees gen1 binding against a gen2 plan/process.
        set_local_binding(LocalResidentBinding(
            resident_key=str(plan.resident_key),
            context_id=int(plan.context_id),
            context_epoch=int(plan.context_epoch),
            resident_generation=int(plan.resident_generation),
            bound_cursor_message_id=plan.cursor_before,
            process_generation=int(getattr(resident, 'generation', 0) or 0),
            tool_profile=str(plan.tool_profile),
            claude_session_id=str(getattr(resident, 'session_id', None) or '') or None,
        ))
    except Exception as exc:
        logger.info(
            'capacity swap post-install failed code=%s; rolling back to old resident',
            getattr(exc, 'error_code', type(exc).__name__),
        )
        rollback_capacity_swap_install(
            live_resident=resident, install_state=install_state,
        )
        err = str(getattr(exc, 'error_code', None) or '')
        if not err and 'registry' in type(exc).__name__.lower():
            err = 'registry_register_failed'
        if not err:
            err = 'capacity_swap_post_install_failed'
        # SessionRegistryError carries error_code.
        if hasattr(exc, 'error_code') and getattr(exc, 'error_code', None):
            # Prefer capacity_swap_cursor_seed_failed / identity_* over generic.
            err = str(exc.error_code)
            if err in {'registry_identity_conflict', 'session_bound_elsewhere',
                       'registry_integrity', 'source_required', 'cwd_required',
                       'session_id_required', 'path_derive_failed',
                       'invalid_identity', 'scan_offset_required',
                       'scan_offset_invalid', 'transcript_path_mismatch',
                       'chat_id_required'}:
                err = 'registry_register_failed'
        return {
            'ok': False,
            'error_code': err,
            'detail': str(exc),
            'rolled_back_to_last_good': True,
            'target_generation_unregistered': True,
            'target_generation': target_gen,
        }

    # Success: retain full install_state until CURRENT user stdin flush.
    plan._capacity_swap_install_state = install_state  # type: ignore[attr-defined]
    plan._capacity_swap_deferred_old_proc = install_state.get('old_proc')  # type: ignore[attr-defined]
    return {
        'ok': True,
        'effective_system': effective,
        'candidate_session_id': handoff.candidate_session_id,
        'target_generation': target_gen,
        'source_generation': source_gen,
        'deferred_old_proc': install_state.get('old_proc'),
        'install_state': install_state,
    }


def _rollback_capacity_swap_if_unflushed(
    plan: DailyTurnPlan,
    *,
    resident: Any,
) -> bool:
    """If CapSwap installed but CURRENT user not yet flushed, restore old live.

    Returns True when a rollback was performed.
    """
    if bool(getattr(plan, '_current_user_stdin_flushed', False)):
        return False
    install_state = getattr(plan, '_capacity_swap_install_state', None)
    if not install_state:
        return False
    logger.info('capacity swap pre-flush failure; rolling back install to old resident')
    rollback_capacity_swap_install(
        live_resident=resident, install_state=install_state,
    )
    plan._capacity_swap_install_state = None  # type: ignore[attr-defined]
    plan._capacity_swap_deferred_old_proc = None  # type: ignore[attr-defined]
    plan.manifest['capacity_swap_pre_flush_rollback'] = True
    return True


def _commit_capacity_swap_after_stdin_flush(plan: DailyTurnPlan) -> None:
    """After successful stdin flush: lock exactly-once and close deferred old proc."""
    plan._current_user_stdin_flushed = True  # type: ignore[attr-defined]
    install_state = getattr(plan, '_capacity_swap_install_state', None)
    old_proc = None
    if install_state:
        old_proc = install_state.get('old_proc')
    if old_proc is None:
        old_proc = getattr(plan, '_capacity_swap_deferred_old_proc', None)
    close_deferred_capacity_swap_old_proc(old_proc)
    plan._capacity_swap_install_state = None  # type: ignore[attr-defined]
    plan._capacity_swap_deferred_old_proc = None  # type: ignore[attr-defined]


def ensure_resident_and_stream(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    env: dict[str, str],
    static_system: str,
    _reprep_depth: int = 0,
    _registry_reprep_depth: int = 0,
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

        # Generation/session door-lock: before ensure_alive (spawn) and stdin.
        door = peek_registered_respawn_decision(plan, resident, static_system)
        effective_system = str(door.get('effective_system') or static_system)
        if door.get('requires_respawn'):
            heartbeat.stop()
            if _registry_reprep_depth >= 1:
                # Second mismatch: drop the current (already-reprepared) lease.
                _release_lease(plan)
                raise DailyRuntimeError(
                    'registered Claude session conflicts with resident_generation',
                    error_code='registered_session_generation_mismatch',
                )

            if door.get('capacity_swap'):
                # Capacity-only path: same context_id/epoch, generation +1, SYSTEM_SUFFIX_V1.
                swap = _attempt_capacity_swap_before_stdin(
                    plan,
                    resident=resident,
                    static_system=static_system,
                    env=env,
                    trigger_reason=str(door.get('reason') or ''),
                )
                if swap.get('ok'):
                    effective_system = str(swap.get('effective_system') or effective_system)
                    yield from ensure_resident_and_stream(
                        plan,
                        resident=resident,
                        env=env,
                        static_system=effective_system,
                        _reprep_depth=_reprep_depth,
                        _registry_reprep_depth=_registry_reprep_depth + 1,
                    )
                    return
                # Pre-stdin failure → same-context last-good → existing cold (order frozen).
                logger.info(
                    'capacity swap failed pre-stdin code=%s; trying same-context last-good',
                    swap.get('error_code'),
                )
                flushed = bool(getattr(plan, '_current_user_stdin_flushed', False))
                try:
                    lg = try_restore_same_context_last_good(
                        plan=plan,
                        live_resident=resident,
                        current_user_stdin_flushed=flushed,
                    )
                except Exception as exc:
                    # After stdin flush: fail closed (no cold resend).
                    from chat.capacity_swap_runtime import CapacitySwapRuntimeError
                    if isinstance(exc, CapacitySwapRuntimeError) and exc.error_code == 'current_user_already_sent':
                        raise DailyRuntimeError(
                            'capacity swap failed after stdin flush; refuse resend',
                            error_code='current_user_already_sent',
                        ) from exc
                    raise
                plan.manifest['capacity_swap_failed'] = True
                plan.manifest['capacity_swap_error_code'] = swap.get('error_code')
                plan.manifest['capacity_swap_last_good'] = dict(lg)
                if lg.get('ok'):
                    yield from ensure_resident_and_stream(
                        plan,
                        resident=resident,
                        env=env,
                        static_system=static_system,
                        _reprep_depth=_reprep_depth,
                        _registry_reprep_depth=_registry_reprep_depth + 1,
                    )
                    return
                logger.info(
                    'same-context last-good not sendable code=%s; falling back to cold reprepare',
                    lg.get('error_code'),
                )

            replacement = reprepare_after_registered_session_change(
                plan,
                resident=resident,
                static_system=static_system,
                static_system_sha256=plan.manifest.get('static_system_sha256') or _sha256_text(static_system),
                persona_sha256=plan.manifest.get('persona_sha256') or '',
                provider=str(plan.manifest.get('provider') or 'claude_code'),
                model=str(plan.manifest.get('model') or ''),
            )
            _adopt_reprepared_plan_in_place(plan, replacement, resident=resident)
            yield from ensure_resident_and_stream(
                plan,
                resident=resident,
                env=env,
                static_system=static_system,
                _reprep_depth=_reprep_depth,
                _registry_reprep_depth=_registry_reprep_depth + 1,
            )
            return

        actual_cold = bool(
            resident.ensure_alive(effective_system, env, tool_profile=plan.tool_profile)
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
            replacement = reprepare_after_hot_cold_mismatch(
                plan,
                resident=resident,
                static_system=static_system,
                static_system_sha256=plan.manifest.get('static_system_sha256') or _sha256_text(static_system),
                persona_sha256=plan.manifest.get('persona_sha256') or '',
                provider=str(plan.manifest.get('provider') or 'claude_code'),
                model=str(plan.manifest.get('model') or ''),
            )
            _adopt_reprepared_plan_in_place(plan, replacement, resident=resident)
            yield from ensure_resident_and_stream(
                plan,
                resident=resident,
                env=env,
                static_system=static_system,
                _reprep_depth=_reprep_depth + 1,
                _registry_reprep_depth=_registry_reprep_depth,
            )
            return

        cold_like = bool(plan.is_cold or plan.is_respawn or actual_cold)
        reality: Optional[dict[str, Any]] = None
        reality_time_anchor = ''
        try:
            from chat.reality_context import build_reality_context
            reality = build_reality_context(
                current_user_message_id=int(plan.user_message_id),
                is_new_model_context=bool(cold_like),
                db_path=plan.db_path,
            )
            reality_time_anchor = str(reality.get('time_anchor') or '').strip()
            plan.manifest['reality_context'] = {
                'time_anchor_reason': reality.get('time_anchor_reason'),
                'weather_anchor_reason': reality.get('weather_anchor_reason'),
                'weather_status': reality.get('weather_status'),
                'has_time_anchor': bool(reality_time_anchor),
                'has_weather_anchor': bool(str(reality.get('weather_anchor') or '').strip()),
            }
        except Exception:
            logger.warning('reality_context build failed; continuing without', exc_info=True)

        try:
            content = format_resident_turn_content(
                assembly=plan.assembly,
                user_content=plan.user_content,
                is_cold=plan.is_cold or actual_cold,
                is_respawn=plan.is_respawn,
                user_image_url=plan.user_image_url,
            user_attachments=list(plan.user_attachments),
                provider_display_thinking_suffix=(
                    plan.provider_display_thinking_suffix
                ),
                reality_time_anchor=reality_time_anchor if cold_like else '',
            )
        except Exception as exc:
            from chat.cc_vision_bridge import VisionBridgeError
            if isinstance(exc, VisionBridgeError):
                # Fail closed before stdin — do not leave a broken vision turn
                # in the resident session.
                raise DailyRuntimeError(
                    str(exc), error_code=getattr(exc, 'code', 'vision_bridge_error'),
                ) from exc
            raise
        if cold_like:
            content = _apply_daily_cold_prompt_fence(
                plan,
                resident=resident,
                static_system=static_system,
                content=content,
                is_cold=plan.is_cold or actual_cold,
                is_respawn=plan.is_respawn,
                reality_time_anchor=reality_time_anchor,
            )

        # Weather remains a prefix; the cold time anchor was inserted above
        # while history and current user were still separate assembly fields.
        if reality is not None:
            try:
                from chat.reality_context import prepend_reality_to_provider_content
                prefix_reality = dict(reality)
                if cold_like:
                    prefix_reality['time_anchor'] = ''
                content = prepend_reality_to_provider_content(content, prefix_reality)
            except Exception:
                logger.warning('reality_context prefix injection failed; continuing without', exc_info=True)

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
            claude_session_id=str(getattr(resident, 'session_id', None) or '') or None,
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

        _capture_transcript_start(plan, resident)

        def _mark_stdin_flushed() -> None:
            _commit_capacity_swap_after_stdin_flush(plan)

        send_kwargs: dict[str, Any] = {'commit_meta': commit_meta}
        try:
            import inspect
            params = inspect.signature(resident.send_turn).parameters
            accepts_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if 'on_stdin_flushed' in params or accepts_kwargs:
                send_kwargs['on_stdin_flushed'] = _mark_stdin_flushed
            if plan.tool_profile == cc_resident.TOOL_PROFILE_UH_A0:
                if not isinstance(plan.turn_lease, dict) or not plan.turn_lease:
                    raise DailyRuntimeError(
                        'UH-A0 daily turn lease missing',
                        error_code='uh_a0_turn_lease_missing',
                    )
                if 'turn_lease' not in params and not accepts_kwargs:
                    raise DailyRuntimeError(
                        'resident send_turn cannot accept UH-A0 turn lease',
                        error_code='uh_a0_turn_lease_unsupported',
                    )
                send_kwargs['turn_lease'] = copy.deepcopy(plan.turn_lease)
        except (TypeError, ValueError):
            if plan.tool_profile == cc_resident.TOOL_PROFILE_UH_A0:
                raise DailyRuntimeError(
                    'resident send_turn signature unavailable for UH-A0',
                    error_code='uh_a0_turn_lease_unsupported',
                )

        try:
            for evt, payload in resident.send_turn(content, **send_kwargs):
                if heartbeat.failed:
                    close_local_resident_if_bound(resident, expected_key=plan.resident_key)
                    raise LeaseHeartbeatTerminalFailure('lease heartbeat failed during stream')
                if evt == 'done':
                    _capture_transcript_end(plan, resident)
                    # Safety net: stream completed without on_stdin_flushed hook.
                    if getattr(plan, '_capacity_swap_install_state', None) is not None:
                        _commit_capacity_swap_after_stdin_flush(plan)
                yield evt, payload
        except Exception as exc:
            # Post-flush: CURRENT user already sent → fail closed, never resend.
            if bool(getattr(plan, '_current_user_stdin_flushed', False)):
                raise
            # Pre-flush CapSwap failure: restore old live, then continue frozen
            # fallback (last-good → cold) so CURRENT user can still send once.
            rolled = _rollback_capacity_swap_if_unflushed(plan, resident=resident)
            if not rolled:
                raise
            logger.info(
                'capacity swap pre-flush send failed (%s); continuing last-good→cold fallback',
                type(exc).__name__,
            )
            pre_flush_meta = {
                'capacity_swap_pre_flush_rollback': True,
                'capacity_swap_pre_flush_send_error': type(exc).__name__,
            }
            heartbeat.stop()

            flushed = bool(getattr(plan, '_current_user_stdin_flushed', False))
            lg = try_restore_same_context_last_good(
                plan=plan,
                live_resident=resident,
                current_user_stdin_flushed=flushed,
            )
            pre_flush_meta['capacity_swap_last_good'] = dict(lg)
            if lg.get('ok'):
                plan.manifest.update(pre_flush_meta)
                yield from ensure_resident_and_stream(
                    plan,
                    resident=resident,
                    env=env,
                    static_system=static_system,
                    _reprep_depth=_reprep_depth,
                    _registry_reprep_depth=_registry_reprep_depth + 1,
                )
                return

            # Demote CapSwap target generation to non-current history via cold
            # reprepare (existing path). No second CapSwap; CURRENT user once.
            # Strip capacity suffix — cold is a new non-capacity_swap generation.
            cold_system = str(static_system or '')
            if CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1 in cold_system:
                cold_system = cold_system.replace(CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1, '')
            replacement = reprepare_after_registered_session_change(
                plan,
                resident=resident,
                static_system=cold_system,
                static_system_sha256=_sha256_text(cold_system),
                persona_sha256=plan.manifest.get('persona_sha256') or '',
                provider=str(plan.manifest.get('provider') or 'claude_code'),
                model=str(plan.manifest.get('model') or ''),
            )
            _adopt_reprepared_plan_in_place(plan, replacement, resident=resident)
            # Re-apply after adopt — replacement.manifest would otherwise wipe flags.
            plan.manifest.update(pre_flush_meta)
            plan.manifest['capacity_swap_pre_flush_cold_fallback'] = True
            yield from ensure_resident_and_stream(
                plan,
                resident=resident,
                env=env,
                static_system=cold_system,
                _reprep_depth=_reprep_depth,
                _registry_reprep_depth=_registry_reprep_depth + 1,
            )
            return

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


def persist_partial_daily_stream_rescue(
    plan: DailyTurnPlan,
    *,
    content: str,
    thinking: str = '',
    tool_calls: str = '',
) -> int:
    """Classic-aligned interrupt rescue: persist visible assistant text only.

    Does not complete the turn, advance cursor, map transcript, or note
    same-context last-good. Caller must still abort the interrupted turn.
    """
    text = str(content or '').strip()
    if not text:
        raise DailyRuntimeError(
            'empty partial rescue content',
            error_code='empty_partial_rescue',
        )
    cache_info = json.dumps(
        {
            'stream_interrupted': True,
            'turn_incomplete': True,
            'partial_rescue': True,
        },
        ensure_ascii=False,
    )
    aid = persist_daily_assistant_for_plan(
        plan,
        content=text,
        thinking=thinking or '',
        tool_calls=tool_calls or '',
        cache_info=cache_info,
        choices='',
    )
    plan.manifest['partial_rescue'] = True
    plan.manifest['partial_rescue_assistant_id'] = int(aid)
    return int(aid)


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


def _consume_feedback_after_success(plan: DailyTurnPlan) -> None:
    ids = list(plan.feedback_ids)
    if not ids:
        return
    try:
        import command_store
        count = command_store.consume_feedback(ids)
    except Exception as exc:
        plan.manifest['feedback_consume_status'] = 'FAILED'
        plan.manifest['feedback_consume_error'] = type(exc).__name__
        logger.warning(
            'daily feedback consume failed; preserving at-least-once feedback ids=%s',
            ids,
            exc_info=True,
        )
        return
    plan.manifest['feedback_consume_status'] = 'CONSUMED'
    plan.manifest['feedback_consumed_count'] = int(count or 0)


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
    complete_daily_turn(
        plan,
        assistant_message_id=int(assistant_message_id),
        stop_reason=str(usage.get('stop_reason') or 'end_turn'),
        input_tokens=usage.get('input_tokens'),
        output_tokens=usage.get('output_tokens'),
        unexpected_save_marker=unexpected_save_marker,
    )
    # Mapping only after assistant persist (Gateway) + cursor CAS success above.
    manifest = finalize_transcript_mapping_after_success(
        plan, assistant_message_id=int(assistant_message_id),
    )
    # Same-context last-good: only after full success (result + persist + JSONL + cursor).
    end_off = plan.transcript_end_offset
    start_off = plan.transcript_start_offset
    sid = str(plan.transcript_claude_session_id or '').strip()
    jsonl_grew = (
        start_off is not None
        and end_off is not None
        and int(end_off) > int(start_off)
        and bool(sid)
    )
    if jsonl_grew:
        existing = get_context_claude_session(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
        note_same_context_last_good(
            context_id=int(plan.context_id),
            context_epoch=int(plan.context_epoch),
            resident_generation=int(plan.resident_generation),
            claude_session_id=sid,
            source=resolve_finalize_registry_source(existing, default='daily_runtime'),
            transcript_end_offset=int(end_off) if end_off is not None else None,
        )
    _consume_feedback_after_success(plan)
    return dict(plan.manifest)


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
        _turn_lease=plan.turn_lease,
        _feedback_snapshot=(tuple(plan.feedback_lines), tuple(plan.feedback_ids)),
        static_system=static_system,
        static_system_sha256=static_system_sha256,
        persona_sha256=persona_sha256,
        provider=provider,
        model=model,
        _cold_reprepare=True,
    )
