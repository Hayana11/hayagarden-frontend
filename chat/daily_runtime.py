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
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import cc_resident

from chat import daily_context as dc
from chat.display_segments import has_save_markers, strip_save_markers
from chat import daily_history as dh
from chat import context_window as cw
from chat.attachment_contract import (
    AttachmentValidationError,
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
    get_same_context_last_good,
    is_capacity_swap_reason,
    note_same_context_last_good,
    register_capacity_swap_generation,
    resolve_finalize_registry_source,
    run_capacity_swap_handoff,
    prepare_capacity_swap_for_context_plan,
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
DAILY_TOOL_PROFILE = cc_resident.TOOL_PROFILE_UH_A0
_HOT_FIXED_SECTION_KINDS = frozenset({
    'invariant_system',
    'accepted_state',
    'accepted_open_loops',
})
_HOT_HISTORICAL_REPRESENTATION_KINDS = frozenset({'raw', 'chunk'})


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
    provider_display_thinking_prompt: str = field(default='', repr=False)
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
    terminal_receipt: Optional[cc_resident.ProviderTerminalReceipt] = field(
        default=None, repr=False,
    )
    continuity_plan: Any = field(default=None, repr=False)
    continuity_chunk_bodies: dict[str, str] = field(default_factory=dict, repr=False)
    hot_desired_plan: Any = field(default=None, repr=False)
    hot_desired_chunk_bodies: dict[str, str] = field(default_factory=dict, repr=False)
    hot_receipt_frozen: Optional[dict[str, Any]] = field(default=None, repr=False)
    hot_decision: Optional[str] = field(default=None, repr=False)
    hot_decision_reason: Optional[str] = field(default=None, repr=False)
    capacity_context_plan: Any = field(default=None, repr=False)
    capacity_context_chunk_bodies: dict[str, str] = field(default_factory=dict, repr=False)
    capacity_context_bootstrap: Optional[dict[str, Any]] = field(default=None, repr=False)
    capacity_source_receipt_frozen: Optional[dict[str, Any]] = field(default=None, repr=False)
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
    try:
        from chat import daily_continuity_shadow_receipt as receipt_store
        receipt_store.clear_for_tests()
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
    had = has_save_markers(raw)
    cleaned = strip_save_markers(raw).strip()
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


DAILY_HISTORY_ATTACHMENT_POLICY = 'explicit_metadata_degrade_v1'


def _history_attachment_marker(msg: dict[str, Any]) -> str:
    """Make prior-turn attachment downgrade explicit and bounded.

    Daily history is a text reconstruction surface. Prior attachments are
    represented by names only; actual image blocks and file bodies belong to
    the current turn or the selected Forge carryover surface.
    """
    try:
        items = provider_current_turn_attachments(
            msg.get('attachments') or [],
            legacy_file_url=msg.get('file_url') or '',
            legacy_file_name=msg.get('file_name') or '',
            legacy_image_url=msg.get('image_url') or '',
        )
    except AttachmentValidationError:
        return '[历史附件已显式降级：附件元数据不可用]'
    if not items:
        return ''
    names = [str(item.get('name') or '图片') for item in items]
    return (
        '[历史附件已显式降级为元数据标记：%s；'
        '本轮不重读历史图片或文件正文]' % '、'.join(names)
    )


def _format_history_messages(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        role = msg.get('role') or 'user'
        label = '用户' if role == 'user' else '费佳'
        line = '[%s] %s' % (label, msg.get('content') or '')
        marker = _history_attachment_marker(msg)
        if marker:
            line += NL + marker
        lines.append(line)
    return NL.join(lines)


def _format_context_plan_representation_blocks(
    blocks: list[dict[str, Any]],
) -> str:
    """Render canonical ContextPlan blocks without redefining raw turns."""
    rendered = []
    for block in blocks:
        body = str(block.get('body') or '').strip()
        if body:
            rendered.append(body)
    return NL.join(rendered)


def _format_context_plan_open_loops(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        loops = [str(item).strip() for item in value if str(item).strip()]
    else:
        text = str(value or '').strip()
        loops = [text] if text else []
    if not loops:
        return ''
    return NL.join([
        '【已接受未完成事项】',
        *('  - ' + item for item in loops),
    ])


def _format_carryover_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ''
    lines = ['【昨日延续对话】']
    for msg in messages:
        role = msg.get('role') or 'user'
        label = '用户' if role == 'user' else '费佳'
        line = '[%s] %s' % (label, msg.get('content') or '')
        marker = _history_attachment_marker(msg)
        if marker:
            line += NL + marker
        lines.append(line)
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
    attachment_static_dir: str = '/opt/frontend/static',
    provider_display_thinking_suffix: str = '',
    reality_time_anchor: str = '',
) -> Any:
    """Assemble resident turn content.

    text-only → str (unchanged contract)
    with image → multimodal list accepted by Claude Code stream-json
    """
    cold_like = bool(is_cold or is_respawn)
    capacity_bootstrap = bool(
        isinstance(assembly, dict)
        and assembly.get('capacity_context_bootstrap')
    )
    attachment_value = list(user_attachments or [])
    legacy_image_url = ''
    if not attachment_value and str(user_image_url or '').strip():
        legacy_image_url = str(user_image_url or '').strip()
    attachment_parts = provider_current_turn_attachment_parts(
        attachment_value,
        legacy_image_url=legacy_image_url,
        static_dir=attachment_static_dir,
    )
    has_image = any(part.get('type') == 'image' for part in attachment_parts)
    # Real vision input replaces the textual [image] placeholder.
    turn_user_text = str(user_content or '')
    if has_image and turn_user_text.strip() == '[image]':
        turn_user_text = ''

    time_anchor = str(reality_time_anchor or '').strip()
    prefix_parts: list[str] = []
    fixed_carrier_values: list[tuple[str, str]] = []
    open_loops = ''
    if cold_like or capacity_bootstrap:
        if cold_like:
            handoff = str(assembly.get('day_handoff') or '').strip()
            if handoff:
                prefix_parts.append(handoff)
            carryover = assembly.get('carryover_messages') or []
            carry_text = _format_carryover_messages(carryover)
            if carry_text:
                prefix_parts.append(carry_text)
        open_loops = _format_context_plan_open_loops(
            assembly.get('context_plan_accepted_open_loops'),
        )
        if open_loops:
            prefix_parts.append(open_loops)
            fixed_carrier_values.append(('accepted_open_loops', open_loops))
    state_text = str(assembly.get('state') or '').strip()
    if state_text:
        prefix_parts.append(state_text)
        fixed_carrier_values.append(('accepted_state', state_text))
    task_feedback = str(assembly.get('task_feedback') or '').strip()
    if task_feedback:
        prefix_parts.append(task_feedback)
    history = assembly.get('current_day_history') or []
    context_plan_blocks = assembly.get('context_plan_representation_blocks') or []
    canonical_history = _format_context_plan_representation_blocks(
        context_plan_blocks,
    )
    render_receipt = {
        'representation_ids': [],
        'representation_body_hashes': [],
        'fixed_section_kinds': [],
        'fixed_section_body_hashes': [],
        'current_request_slots': 0,
        'current_request_carrier': '',
    }

    def _render_context_plan_history() -> str:
        rendered = []
        for block in context_plan_blocks:
            body = str(block.get('body') or '').strip()
            if not body:
                continue
            rendered.append(body)
            render_receipt['representation_ids'].append(
                str(block.get('representation_id') or '')
            )
            render_receipt['representation_body_hashes'].append(
                _sha256_text(body)
            )
        return NL.join(rendered)

    def _render_fixed_prefix() -> str:
        for kind, value in fixed_carrier_values:
            render_receipt['fixed_section_kinds'].append(str(kind))
            render_receipt['fixed_section_body_hashes'].append(
                _sha256_text(str(value))
            )
        return prefix

    def _render_current_request() -> str:
        render_receipt['current_request_slots'] += 1
        render_receipt['current_request_carrier'] = 'tail'
        return turn_user_text

    prefix = NL.join(p for p in prefix_parts if p)
    assembly['_context_install_render_receipt'] = render_receipt
    if (cold_like or capacity_bootstrap) and canonical_history:
        body = (
            '以下是本聊天日内的正式对话记录：' + NL + NL
            + _render_context_plan_history()
        )
        if time_anchor:
            body += NL + NL + time_anchor
        body += NL + NL + '请回复最后一条用户消息。' + NL + NL + _render_current_request()
        if prefix:
            text = _render_fixed_prefix() + NL + NL + body
        else:
            text = body
    elif cold_like and history:
        history_text = _format_history_messages(history)
        body = (
            '以下是本聊天日内的正式对话记录：' + NL + NL
            + history_text
        )
        if time_anchor:
            body += NL + NL + time_anchor
        body += NL + NL + '请回复最后一条用户消息。' + NL + NL + _render_current_request()
        if prefix:
            text = _render_fixed_prefix() + NL + NL + body
        else:
            text = body
    elif history:
        history_text = _format_history_messages(history)
        replay = '【新增正式对话】' + NL + history_text + NL + NL
        if prefix:
            text = _render_fixed_prefix() + NL + NL + replay + _render_current_request()
        else:
            text = replay + _render_current_request()
    elif prefix:
        if cold_like and time_anchor:
            text = _render_fixed_prefix() + NL + NL + time_anchor + NL + NL + _render_current_request()
        else:
            text = _render_fixed_prefix() + NL + NL + _render_current_request()
    elif cold_like and time_anchor:
        text = time_anchor + NL + NL + _render_current_request()
    else:
        text = _render_current_request()

    if not attachment_parts:
        content = text
    else:
        from chat.cc_vision_bridge import build_claude_user_content
        content = build_claude_user_content(
            text=text,
            attachment_parts=attachment_parts,
        )
    from chat.display_thinking import append_display_thinking_suffix
    content = append_display_thinking_suffix(
        content, provider_display_thinking_suffix,
    )
    _bind_context_install_render_receipt(assembly, content)
    return content


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




def _context_plan_consumer_enabled() -> bool:
    """Read the existing gate; any read/config error fails closed to OFF."""
    try:
        import config_store
        return str(config_store.get(
            'CONTEXT_PLAN_CONSUMER_ENABLED',
            '0',
        ) or '').strip() == '1'
    except Exception:
        return False


def _context_plan_policy(mode: str = 'hot') -> tuple[Any, str]:
    from chat.cold_bootstrap_budget import (
        capacity_swap_prompt_target,
        cold_prompt_target,
        resident_rebuild_prompt_target,
        cold_safety_margin,
    )
    from chat.context_lean import cc_history_token_budget
    from continuity.context_plan import (
        CONTINUITY_CONTEXT_BUDGET_POLICY_VERSION,
        ContextBudgetPolicy,
    )

    policy_mode = str(mode or 'hot').strip().lower()
    if policy_mode in ('cold', 'respawn'):
        target = int(resident_rebuild_prompt_target())
    elif policy_mode == 'capacity':
        target = int(capacity_swap_prompt_target())
    elif policy_mode == 'hot':
        target = int(cold_prompt_target())
    else:
        raise ValueError('unknown_context_plan_mode:%s' % policy_mode)
    reserve = int(cold_safety_margin())
    token_budget = target + reserve
    recent_raw_target = int(cc_history_token_budget())
    policy = ContextBudgetPolicy(
        token_budget=token_budget,
        reserve_budget=reserve,
        recent_raw_target=recent_raw_target,
    )
    return policy, CONTINUITY_CONTEXT_BUDGET_POLICY_VERSION


def _production_continuity_store_path(plan: DailyTurnPlan) -> str:
    path = str(plan.db_path or dc.DEFAULT_DB_PATH or '').strip()
    if not path:
        raise DailyRuntimeError(
            'canonical production continuity store path is unavailable',
            error_code='context_plan_store_unavailable',
        )
    return path


def _build_production_context_plan(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    static_system: str,
    mode: str = 'hot',
) -> tuple[Any, dict[str, str]]:
    """Build exactly one canonical plan from the explicit production DB."""
    from continuity.store import read_ready_surface
    policy, policy_version = _context_plan_policy(mode=mode)
    store_path = _production_continuity_store_path(plan)
    surface = read_ready_surface(store_path)
    if surface.status == 'unavailable':
        raise DailyRuntimeError(
            'continuity store unavailable',
            error_code='context_plan_store_unavailable',
            retryable=True,
        )
    if surface.status == 'corrupt':
        raise DailyRuntimeError(
            'continuity store corrupt',
            error_code='context_plan_store_corrupt',
            retryable=False,
        )
    validated_ready_artifacts = tuple(
        {
            'chunk_id': str(chunk.chunk_id),
            'artifact_revision': str(chunk.artifact_revision),
            'body_hash': str(chunk.body_hash),
        }
        for chunk in surface.artifacts
    )
    from chat.daily_continuity_shadow import build_daily_continuity_shadow_plan
    fixed_sections = _build_continuity_shadow_fixed_sections(
        plan=plan,
        resident=resident,
        static_system=static_system,
        require_full_state_snapshot=True,
    )
    result = build_daily_continuity_shadow_plan(
        source_db_path=store_path,
        shadow_store_path=store_path,
        continuity_store_path=store_path,
        current_user_message_id=int(plan.user_message_id),
        budget_policy=policy,
        accepted_fixed_sections=fixed_sections,
        budget_policy_version=policy_version,
        validated_ready_artifacts=validated_ready_artifacts,
    )
    if result.plan is None:
        raise DailyRuntimeError(
            str(result.error_code or 'context_plan_unavailable'),
            error_code=str(result.error_code or 'context_plan_unavailable'),
            retryable=True,
        )
    if (
        not str(result.plan.plan_id or '').startswith('plan:')
        or not re.fullmatch(r'[0-9a-f]{64}', str(result.plan.plan_hash or ''))
        or str(result.plan.plan_id) != 'plan:' + str(result.plan.plan_hash)[:32]
        or not re.fullmatch(r'[0-9a-f]{64}', str(result.plan.source_hash or ''))
    ):
        raise DailyRuntimeError(
            'canonical ContextPlan identity is invalid',
            error_code='context_plan_identity_invalid',
            retryable=False,
        )
    if not result.plan.valid:
        raise DailyRuntimeError(
            'canonical ContextPlan is invalid',
            error_code='context_plan_invalid',
            retryable=False,
        )
    validated_by_id = {
        str(chunk.chunk_id): (str(chunk.artifact_revision), str(chunk.body_hash))
        for chunk in surface.artifacts
    }
    for representation in result.plan.representations:
        if representation.kind != 'chunk':
            continue
        chunk_id = str(representation.chunk_id or '')
        if chunk_id not in validated_by_id:
            raise DailyRuntimeError(
                'ContextPlan selected an artifact outside the strict READY surface',
                error_code='context_plan_ready_artifact_outside_surface',
                retryable=False,
            )
        provenance = dict(representation.provenance)
        if (
            provenance.get('artifact_revision') != validated_by_id[chunk_id][0]
            or provenance.get('body_hash') != validated_by_id[chunk_id][1]
        ):
            raise DailyRuntimeError(
                'ContextPlan selected a changed READY artifact',
                error_code='context_plan_ready_artifact_identity_changed',
                retryable=False,
            )
    bodies = {
        str(chunk.chunk_id): str(chunk.body)
        for chunk in (surface.artifacts or ())
    }
    return result.plan, bodies


def _context_source_message_ids(source_ref: str) -> tuple[int, ...]:
    value = str(source_ref or '')
    parts = value.split(':')
    if len(parts) == 3 and parts[0] == 'turn':
        try:
            return (int(parts[1]), int(parts[2]))
        except (TypeError, ValueError):
            return ()
    if len(parts) == 2 and parts[0] == 'wake':
        try:
            return (int(parts[1]),)
        except (TypeError, ValueError):
            return ()
    return ()


def _load_context_source_rows(
    message_ids: set[int],
    *,
    db_path: str,
) -> dict[int, dict[str, Any]]:
    if not message_ids:
        return {}
    from continuity.store import open_continuity_read_only
    conn = open_continuity_read_only(db_path)
    try:
        columns = {
            str(row[1])
            for row in conn.execute('PRAGMA table_info(chat_messages)').fetchall()
        }
        selected = ['id', 'author', 'content', 'created_at']
        for optional in (
            'thinking', 'tool_calls', 'branches', 'branch_idx', 'cache_info',
            'source_kind', 'attachments', 'image_url', 'file_url', 'file_name',
        ):
            if optional in columns:
                selected.append(optional)
        placeholders = ','.join('?' for _ in message_ids)
        rows = conn.execute(
            'SELECT %s FROM chat_messages WHERE id IN (%s)' % (
                ', '.join(selected), placeholders,
            ),
            tuple(sorted(int(value) for value in message_ids)),
        ).fetchall()
        return {int(row['id']): dict(row) for row in rows}
    finally:
        conn.close()


def _project_context_plan_history(
    plan: DailyTurnPlan,
    *,
    context_plan: Any,
    chunk_bodies: dict[str, str],
) -> dict[str, Any]:
    """Project canonical representations into the existing formatter fields."""
    from continuity.materialization import materialize_source_members
    from continuity.sources import is_formal_user_source_row
    store_path = _production_continuity_store_path(plan)
    message_ids: set[int] = set()
    for representation in context_plan.representations:
        if representation.kind == 'raw':
            for member in representation.source_members:
                ids = _context_source_message_ids(member.source_ref)
                if not ids:
                    raise DailyRuntimeError(
                        'selected continuity source reference is invalid',
                        error_code='context_plan_source_unavailable',
                    )
                message_ids.update(ids)
    rows = _load_context_source_rows(message_ids, db_path=store_path)
    history: list[dict[str, Any]] = []
    representation_blocks: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for representation in context_plan.representations:
        if representation.kind == 'chunk':
            body = str(chunk_bodies.get(str(representation.chunk_id)) or '').strip()
            if not body:
                raise DailyRuntimeError(
                    'selected continuity chunk body is unavailable',
                    error_code='context_plan_chunk_unavailable',
                )
            history.append({
                'role': 'assistant',
                'content': body,
                'message_id': 0,
                'continuity_representation_id': str(
                    representation.representation_id
                ),
            })
            representation_blocks.append({
                'kind': 'chunk',
                'representation_id': str(representation.representation_id),
                'body': body,
            })
            continue
        if representation.kind != 'raw':
            raise DailyRuntimeError(
                'selected continuity representation kind is invalid',
                error_code='context_plan_representation_invalid',
            )
        try:
            materialized = materialize_source_members(
                representation.source_members,
                rows.values(),
            )
        except Exception as exc:
            raise DailyRuntimeError(
                'selected continuity raw representation is unavailable',
                error_code='context_plan_source_unavailable',
            ) from exc
        if not str(materialized.body or '').strip():
            raise DailyRuntimeError(
                'selected continuity raw representation is empty',
                error_code='context_plan_source_unavailable',
            )
        representation_blocks.append({
            'kind': 'raw',
            'representation_id': str(representation.representation_id),
            'body': str(materialized.body),
            'source_fingerprint': str(materialized.source_fingerprint),
            'source_refs': tuple(materialized.source_refs),
        })
        for member in representation.source_members:
            ids = _context_source_message_ids(member.source_ref)
            if not ids:
                raise DailyRuntimeError(
                    'selected continuity source reference is invalid',
                    error_code='context_plan_source_unavailable',
                )
            for message_id in ids:
                if message_id == int(plan.user_message_id) or message_id in seen_ids:
                    continue
                row = rows.get(message_id)
                if row is None:
                    raise DailyRuntimeError(
                        'selected continuity source row is unavailable',
                        error_code='context_plan_source_unavailable',
                    )
                seen_ids.add(message_id)
                history.append({
                    'role': (
                        'user' if is_formal_user_source_row(row)
                        else 'assistant'
                    ),
                    'content': str(row.get('content') or ''),
                    'message_id': message_id,
                    'attachments': row.get('attachments') or [],
                    'image_url': row.get('image_url') or '',
                    'file_url': row.get('file_url') or '',
                    'file_name': row.get('file_name') or '',
                })
    assembly = dict(plan.assembly)
    assembly['current_day_history'] = history
    assembly['context_plan_representation_blocks'] = representation_blocks
    if any(
        str(section.kind or '') == 'accepted_open_loops'
        for section in context_plan.ordered_sections
    ):
        handoff = assembly.get('day_handoff_content')
        if isinstance(handoff, dict):
            loops = handoff.get('open_loops')
            if loops:
                assembly['context_plan_accepted_open_loops'] = loops
            else:
                assembly.pop('context_plan_accepted_open_loops', None)
        else:
            assembly.pop('context_plan_accepted_open_loops', None)
    layers = [
        layer for layer in (assembly.get('layers') or ())
        if str(layer.get('kind') or '') != 'current_day_history'
    ]
    if history:
        layers.append({
            'kind': 'current_day_history',
            'messages': [dict(item) for item in history],
        })
    assembly['layers'] = layers
    return assembly


def _is_capacity_context_plan(plan: DailyTurnPlan) -> bool:
    return getattr(plan, 'capacity_context_plan', None) is not None



def _freeze_capacity_context_bootstrap(
    plan: DailyTurnPlan,
    *,
    context_plan: Any,
) -> dict[str, Any]:
    """Freeze the first-target-stdin carrier beside the exact ContextPlan."""
    assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
    fixed_kinds = {
        str(getattr(section, 'kind', '') or '')
        for section in tuple(getattr(context_plan, 'ordered_sections', ()) or ())
    }
    handoff = assembly.get('day_handoff_content')
    open_loops = (
        copy.deepcopy(handoff.get('open_loops'))
        if isinstance(handoff, dict)
        else None
    )
    return {
        'plan_id': str(getattr(context_plan, 'plan_id', '') or ''),
        'plan_hash': str(getattr(context_plan, 'plan_hash', '') or ''),
        'accepted_state_text': (
            _accepted_state_text(assembly)
            if 'accepted_state' in fixed_kinds else ''
        ),
        'accepted_open_loops': (
            open_loops if 'accepted_open_loops' in fixed_kinds else None
        ),
        'handoff_content': copy.deepcopy(handoff) if isinstance(handoff, dict) else None,
        'fixed_section_identities': tuple(
            _section_install_identity(section)
            for section in tuple(getattr(context_plan, 'ordered_sections', ()) or ())
            if str(getattr(section, 'kind', '') or '') in _HOT_FIXED_SECTION_KINDS
        ),
    }


def _project_capacity_context_bootstrap(plan: DailyTurnPlan) -> dict[str, Any]:
    """Project only chunk blocks plus fixed first-stdin content."""
    context_plan = getattr(plan, 'capacity_context_plan', None)
    if context_plan is None:
        raise DailyRuntimeError(
            'capacity ContextPlan is missing',
            error_code='context_plan_capacity_missing',
        )
    assembly = _project_context_plan_history(
        plan,
        context_plan=context_plan,
        chunk_bodies=dict(getattr(plan, 'capacity_context_chunk_bodies', {}) or {}),
    )
    bootstrap = dict(getattr(plan, 'capacity_context_bootstrap', None) or {})
    chunk_blocks = [
        dict(block)
        for block in (assembly.get('context_plan_representation_blocks') or [])
        if isinstance(block, dict) and str(block.get('kind') or '') == 'chunk'
    ]
    assembly['capacity_context_bootstrap'] = True
    assembly['context_plan_representation_blocks'] = chunk_blocks
    assembly['current_day_history'] = []
    assembly['day_handoff'] = ''
    assembly['carryover_messages'] = []
    assembly['day_handoff_content'] = copy.deepcopy(
        bootstrap.get('handoff_content')
    )
    accepted_state = str(bootstrap.get('accepted_state_text') or '')
    assembly['state'] = accepted_state
    if bootstrap.get('accepted_open_loops') is not None:
        assembly['context_plan_accepted_open_loops'] = copy.deepcopy(
            bootstrap.get('accepted_open_loops')
        )
    else:
        assembly.pop('context_plan_accepted_open_loops', None)
    assembly['layers'] = [
        layer for layer in (assembly.get('layers') or ())
        if str(layer.get('kind') or '') not in {
            'day_handoff',
            'carryover',
            'state',
            'current_day_history',
        }
    ]
    if accepted_state:
        assembly['layers'].append({
            'kind': 'state',
            'text': accepted_state,
            'mode': 'snapshot',
        })
    manifest = dict(assembly.get('manifest') or {})
    manifest.update({
        'context_plan_consumer': 'canonical_capacity',
        'context_plan_id': str(getattr(context_plan, 'plan_id', '') or ''),
        'context_plan_hash': str(getattr(context_plan, 'plan_hash', '') or ''),
        'context_plan_capacity_chunk_count': len(chunk_blocks),
        'context_plan_capacity_raw_in_first_stdin': False,
    })
    assembly['manifest'] = manifest
    return assembly


def _context_plan_for_receipt(plan: DailyTurnPlan) -> Any:
    # A Capacity Swap plan is the consumed production authority for this
    # transition; do not let an older cold/hot plan win receipt construction.
    context_plan = getattr(plan, 'capacity_context_plan', None)
    if context_plan is not None:
        return context_plan
    context_plan = getattr(plan, 'continuity_plan', None)
    if context_plan is not None:
        return context_plan
    return getattr(plan, 'hot_desired_plan', None)


def _context_plan_chunk_bodies(plan: DailyTurnPlan) -> dict[str, str]:
    capacity_bodies = getattr(plan, 'capacity_context_chunk_bodies', None)
    if isinstance(capacity_bodies, dict) and capacity_bodies:
        return dict(capacity_bodies)
    return dict(getattr(plan, 'continuity_chunk_bodies', None) or {})


def _context_receipt_members(plan: DailyTurnPlan) -> tuple[Any, ...]:
    from chat.context_receipt import ContextReceiptMember
    context_plan = _context_plan_for_receipt(plan)
    if context_plan is None:
        return ()
    members: list[Any] = []
    order = 0
    for representation in context_plan.representations:
        for source_member in representation.source_members:
            members.append(ContextReceiptMember(
                installed_order=order,
                representation_id=str(representation.representation_id),
                representation_kind=str(representation.kind),
                source_ref=str(source_member.source_ref),
                source_revision=str(source_member.source_revision),
                source_kind=str(source_member.source_kind),
                content_hash=str(source_member.content_hash),
                span_start=source_member.span_start,
                span_end=source_member.span_end,
                branch_id=str(source_member.branch_id),
            ))
            order += 1
    for section in context_plan.ordered_sections:
        kind = str(getattr(section, 'kind', '') or '')
        if kind not in _HOT_FIXED_SECTION_KINDS:
            continue
        members.append(ContextReceiptMember(
            installed_order=order,
            representation_id='fixed:%s' % kind,
            representation_kind='fixed',
            source_ref=str(section.source_ref),
            source_revision=str(section.content_hash),
            source_kind=kind,
            content_hash=str(section.content_hash),
            branch_id='',
        ))
        order += 1
    for section in context_plan.ordered_sections:
        if str(getattr(section, 'kind', '') or '') == 'current_request':
            members.append(ContextReceiptMember(
                installed_order=order,
                representation_id='current_request',
                representation_kind='fixed',
                source_ref=str(section.source_ref),
                source_revision=str(section.content_hash),
                source_kind='current_request',
                content_hash=str(section.content_hash),
                branch_id='',
            ))
            break
    return tuple(members)


def _receipt_member_source_identity(member: Any) -> tuple[Any, ...]:
    return (
        str(getattr(member, 'source_ref', '') or ''),
        str(getattr(member, 'source_revision', '') or ''),
        str(getattr(member, 'source_kind', '') or ''),
        str(getattr(member, 'content_hash', '') or ''),
        getattr(member, 'span_start', None),
        getattr(member, 'span_end', None),
        str(getattr(member, 'branch_id', '') or ''),
    )


def _receipt_member_identity(member: Any) -> tuple[Any, ...]:
    return (
        str(getattr(member, 'representation_id', '') or ''),
        str(getattr(member, 'representation_kind', '') or ''),
        *_receipt_member_source_identity(member),
    )


def _source_member_identity(member: Any) -> tuple[Any, ...]:
    return (
        str(getattr(member, 'source_ref', '') or ''),
        str(getattr(member, 'source_revision', '') or ''),
        str(getattr(member, 'source_kind', '') or ''),
        str(getattr(member, 'content_hash', '') or ''),
        getattr(member, 'span_start', None),
        getattr(member, 'span_end', None),
        str(getattr(member, 'branch_id', '') or ''),
    )


def _message_id_from_source_ref(source_ref: Any) -> Optional[int]:
    parts = str(source_ref or '').split(':')
    if len(parts) != 2 or parts[0] != 'message':
        return None
    try:
        value = int(parts[1])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _freeze_hot_receipt(
    plan: DailyTurnPlan,
    *,
    resident: Any,
) -> dict[str, Any]:
    """Read the durable receipt once and bind its revision to this turn."""
    from chat import context_receipt as receipt_store
    from continuity.store import open_continuity_read_only

    receipt = None
    receipt_members: tuple[Any, ...] = ()
    receipt_read_error: Optional[str] = None
    receipt_members_complete = True
    try:
        conn = open_continuity_read_only(_production_continuity_store_path(plan))
        try:
            try:
                receipt = receipt_store.get_receipt(
                    conn,
                    context_id=int(plan.context_id),
                    context_epoch=int(plan.context_epoch),
                    resident_generation=int(plan.resident_generation),
                )
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc).lower():
                    raise
                receipt = None
            if receipt is not None:
                try:
                    receipt_members = receipt_store.get_receipt_members(
                        conn,
                        context_id=int(plan.context_id),
                        context_epoch=int(plan.context_epoch),
                        resident_generation=int(plan.resident_generation),
                    )
                except sqlite3.OperationalError as exc:
                    if 'no such table' not in str(exc).lower():
                        raise
                    receipt_members_complete = False
                    receipt_members = ()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error) as exc:
        receipt_read_error = type(exc).__name__
        receipt = None
        receipt_members = ()
        receipt_members_complete = False

    membership_valid = bool(receipt is None)
    if receipt is not None and receipt_members_complete:
        try:
            membership_valid = (
                receipt_store.installed_membership_hash(receipt_members)
                == str(receipt.membership_hash)
            )
        except (TypeError, ValueError):
            membership_valid = False

    live_sid = str(getattr(resident, 'session_id', None) or '').strip()
    live_process_generation = int(
        getattr(resident, 'generation', 0) or 0
    )
    frozen = {
        'context_id': int(plan.context_id),
        'context_epoch': int(plan.context_epoch),
        'resident_generation': int(plan.resident_generation),
        'resident_key': str(plan.resident_key),
        'provider': str(plan.manifest.get('provider') or 'claude_code'),
        'model_identity': str(plan.manifest.get('model') or 'unknown'),
        'session_id': live_sid,
        'process_generation': live_process_generation,
        'receipt': receipt,
        'receipt_revision': (
            int(receipt.receipt_revision) if receipt is not None else None
        ),
        'plan_id': str(receipt.plan_id) if receipt is not None else None,
        'plan_hash': str(receipt.plan_hash) if receipt is not None else None,
        'membership_hash': (
            str(receipt.membership_hash) if receipt is not None else None
        ),
        'installed_source_watermark': (
            int(receipt.installed_source_watermark) if receipt is not None else None
        ),
        'members': tuple(receipt_members),
        'receipt_missing': receipt is None,
        'receipt_members_complete': bool(receipt_members_complete),
        'membership_valid': bool(membership_valid),
        'receipt_read_error': receipt_read_error,
        'expected_receipt_revision': (
            int(receipt.receipt_revision) if receipt is not None else None
        ),
    }
    plan.hot_receipt_frozen = frozen
    plan.manifest.update({
        'context_plan_receipt_revision_frozen': frozen['expected_receipt_revision'],
        'context_plan_receipt_present': not frozen['receipt_missing'],
        'context_plan_receipt_membership_valid': bool(membership_valid),
    })
    return frozen


def _hot_native_tail_proof(
    plan: DailyTurnPlan,
    *,
    frozen: dict[str, Any],
    registry: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Prove the previous receipt request+reply is the live native tail."""
    from continuity.sources import (
        build_source_members,
        derive_completed_turns,
        evidence_ref,
    )
    from continuity.store import open_continuity_read_only

    members = tuple(frozen.get('members') or ())
    request_members = tuple(
        member for member in members
        if str(getattr(member, 'source_kind', '') or '') == 'current_request'
    )
    if len(request_members) > 1:
        return {'status': 'ambiguous', 'reason': 'multiple_current_request_members'}
    if not request_members:
        return {'status': 'missing', 'reason': 'current_request_proof_missing'}
    request_source_ref = str(request_members[0].source_ref or '').strip()
    user_message_id = _message_id_from_source_ref(request_source_ref)
    if user_message_id is None:
        return {
            'status': 'ambiguous' if request_source_ref else 'missing',
            'reason': 'current_request_source_ambiguous' if request_source_ref
                else 'current_request_source_missing',
        }
    if int(user_message_id) >= int(plan.user_message_id):
        return {'status': 'ambiguous', 'reason': 'current_request_is_not_previous'}
    try:
        watermark = int(frozen['receipt'].installed_source_watermark)
    except (AttributeError, TypeError, ValueError):
        return {'status': 'missing', 'reason': 'receipt_watermark_invalid'}
    if watermark <= 0:
        return {'status': 'missing', 'reason': 'receipt_watermark_missing'}
    if registry is None:
        return {'status': 'missing', 'reason': 'session_registry_missing'}
    if str(registry.get('scan_status') or '') != 'READY':
        return {'status': 'missing', 'reason': 'session_registry_not_ready'}
    if registry.get('last_mapped_message_id') is None:
        return {'status': 'missing', 'reason': 'native_tail_mapping_missing'}
    try:
        last_mapped_message_id = int(registry['last_mapped_message_id'])
    except (TypeError, ValueError):
        return {'status': 'missing', 'reason': 'native_tail_mapping_watermark_invalid'}
    if last_mapped_message_id != watermark:
        return {'status': 'missing', 'reason': 'native_tail_mapping_watermark_mismatch'}

    last_good = get_same_context_last_good(
        int(plan.context_id), int(plan.context_epoch),
    )
    if not isinstance(last_good, dict):
        return {'status': 'missing', 'reason': 'same_context_last_good_missing'}
    try:
        last_good_identity_matches = (
            int(last_good.get('context_id') or 0) == int(plan.context_id)
            and int(last_good.get('context_epoch') or 0) == int(plan.context_epoch)
            and int(last_good.get('resident_generation') or 0)
                == int(plan.resident_generation)
            and str(last_good.get('claude_session_id') or '')
                == str(registry.get('claude_session_id') or '')
        )
    except (TypeError, ValueError):
        last_good_identity_matches = False
    if not last_good_identity_matches:
        return {'status': 'missing', 'reason': 'same_context_last_good_identity_mismatch'}
    try:
        last_good_end = int(last_good.get('transcript_end_offset'))
        registry_offset = int(registry.get('scan_offset'))
    except (TypeError, ValueError):
        return {'status': 'missing', 'reason': 'native_tail_offset_missing'}
    if last_good_end <= 0 or registry_offset != last_good_end:
        return {'status': 'missing', 'reason': 'native_tail_offset_mismatch'}

    try:
        conn = open_continuity_read_only(_production_continuity_store_path(plan))
        try:
            rows = conn.execute(
                'SELECT m.* FROM chat_messages m '
                'JOIN daily_message_contexts dmc ON dmc.message_id=m.id '
                'WHERE dmc.context_id=? AND dmc.context_epoch=? '
                'AND dmc.resident_generation=? AND m.id<=? '
                'ORDER BY m.id ASC',
                (
                    int(plan.context_id), int(plan.context_epoch),
                    int(plan.resident_generation), watermark,
                ),
            ).fetchall()
            mapping_rows = conn.execute(
                "SELECT event_uuid, message_id, role, claude_session_id, "
                "jsonl_byte_offset FROM chat_message_claude_events "
                "WHERE context_id=? AND context_epoch=? "
                "AND resident_generation=? AND message_id IN (?,?) "
                "AND role IN ('user','assistant') "
                "ORDER BY jsonl_byte_offset ASC",
                (
                    int(plan.context_id), int(plan.context_epoch),
                    int(plan.resident_generation), user_message_id, watermark,
                ),
            ).fetchall()
        finally:
            conn.close()
    except (OSError, ValueError, sqlite3.Error):
        return {'status': 'missing', 'reason': 'native_tail_rows_unavailable'}

    try:
        durable_rows = [dict(row) for row in rows]
    except Exception:
        return {'status': 'ambiguous', 'reason': 'native_tail_rows_corrupt'}
    try:
        turns = tuple(derive_completed_turns(durable_rows))
    except Exception:
        return {'status': 'ambiguous', 'reason': 'canonical_tail_derivation_failed'}
    matching = tuple(
        turn for turn in turns
        if str(turn.user_input_ref.source_ref) == 'message:%d' % user_message_id
    )
    if len(matching) > 1:
        return {'status': 'ambiguous', 'reason': 'multiple_canonical_tail_turns'}
    if not matching:
        return {'status': 'missing', 'reason': 'canonical_tail_turn_missing'}
    turn = matching[0]
    if len(turn.assistant_committed_output_refs) != 1:
        return {'status': 'ambiguous', 'reason': 'canonical_tail_assistant_count'}
    assistant_message_id = _message_id_from_source_ref(
        turn.assistant_committed_output_refs[0].source_ref,
    )
    if assistant_message_id is None or assistant_message_id != watermark:
        return {'status': 'missing', 'reason': 'canonical_tail_watermark_mismatch'}

    current_rows = tuple(
        row for row in durable_rows if int(row.get('id') or 0) == user_message_id
    )
    if len(current_rows) != 1:
        return {
            'status': 'ambiguous' if len(current_rows) > 1 else 'missing',
            'reason': 'current_request_row_ambiguous',
        }
    try:
        current_evidence = evidence_ref(current_rows[0], prefix='message')
    except Exception:
        return {'status': 'ambiguous', 'reason': 'current_request_evidence_corrupt'}
    request_member = request_members[0]
    if (
        str(request_member.source_ref) != str(current_evidence.source_ref)
        or str(request_member.source_revision) != str(current_evidence.source_revision)
        or str(request_member.content_hash) != str(current_evidence.content_hash)
    ):
        return {'status': 'missing', 'reason': 'current_request_proof_changed'}

    try:
        user_events = tuple(
            row for row in mapping_rows
            if int(row['message_id']) == user_message_id
            and str(row['role']) == 'user'
        )
        assistant_events = tuple(
            row for row in mapping_rows
            if int(row['message_id']) == assistant_message_id
            and str(row['role']) == 'assistant'
        )
    except Exception:
        return {'status': 'ambiguous', 'reason': 'native_tail_mapping_corrupt'}
    if len(user_events) > 1 or len(assistant_events) > 1:
        return {'status': 'ambiguous', 'reason': 'native_tail_mapping_ambiguous'}
    if len(user_events) != 1 or len(assistant_events) != 1:
        return {'status': 'missing', 'reason': 'native_tail_mapping_incomplete'}
    user_event = user_events[0]
    assistant_event = assistant_events[0]
    expected_sid = str(registry.get('claude_session_id') or '')
    try:
        sessions_match = (
            str(user_event['claude_session_id']) == expected_sid
            and str(assistant_event['claude_session_id']) == expected_sid
        )
    except Exception:
        return {'status': 'ambiguous', 'reason': 'native_tail_mapping_corrupt'}
    if not sessions_match:
        return {'status': 'missing', 'reason': 'native_tail_session_mismatch'}
    try:
        user_offset = int(user_event['jsonl_byte_offset'])
        assistant_offset = int(assistant_event['jsonl_byte_offset'])
    except Exception:
        return {'status': 'ambiguous', 'reason': 'native_tail_mapping_offset_corrupt'}
    if not (0 <= user_offset < assistant_offset < last_good_end):
        return {'status': 'missing', 'reason': 'native_tail_mapping_offset_invalid'}

    try:
        tail_members = tuple(build_source_members((turn,), ()))
    except Exception:
        return {'status': 'ambiguous', 'reason': 'canonical_tail_member_corrupt'}
    if len(tail_members) != 1:
        return {'status': 'ambiguous', 'reason': 'canonical_tail_member_ambiguous'}
    return {
        'status': 'pass',
        'reason': None,
        'source_member': tail_members[0],
        'user_message_id': user_message_id,
        'assistant_message_id': assistant_message_id,
        'transcript_end_offset': last_good_end,
    }


def _hot_live_identity_decision(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    frozen: dict[str, Any],
) -> tuple[str, str, Optional[dict[str, Any]]]:
    if not is_epoch_token_current(plan):
        return 'BLOCKED', 'epoch_invalid', None
    binding = get_local_binding()
    if binding is None or not _binding_matches_plan(binding, plan):
        return 'RESPAWN', 'live_binding_missing_or_mismatched', None
    try:
        if int(binding.context_id) != int(plan.context_id):
            return 'RESPAWN', 'live_binding_context_mismatch', None
        if int(binding.process_generation) <= 0:
            return 'RESPAWN', 'live_binding_process_generation_missing', None
    except (TypeError, ValueError):
        return 'BLOCKED', 'live_binding_identity_invalid', None
    if not _resident_is_alive(resident):
        return 'RESPAWN', 'resident_not_alive', None
    live_generation = int(getattr(resident, 'generation', 0) or 0)
    live_sid = str(getattr(resident, 'session_id', None) or '').strip()
    if live_generation <= 0 or not live_sid:
        return 'RESPAWN', 'live_session_identity_missing', None
    if int(binding.process_generation) != live_generation:
        return 'RESPAWN', 'binding_process_generation_mismatch', None
    if not str(binding.claude_session_id or '').strip():
        return 'RESPAWN', 'binding_session_identity_missing', None
    if str(binding.claude_session_id).strip() != live_sid:
        return 'RESPAWN', 'binding_session_identity_mismatch', None
    frozen_sid = str(frozen.get('session_id') or '').strip()
    try:
        frozen_process_generation = int(frozen.get('process_generation') or 0)
    except (TypeError, ValueError):
        return 'BLOCKED', 'frozen_process_generation_invalid', None
    if frozen_sid and frozen_sid != live_sid:
        return 'RESPAWN', 'frozen_session_identity_mismatch', None
    if frozen_process_generation and frozen_process_generation != live_generation:
        return 'RESPAWN', 'frozen_process_generation_mismatch', None
    expected_model = str(frozen.get('model_identity') or '').strip()
    live_model = str(
        getattr(resident, 'model_identity', None)
        or getattr(resident, '_model_identity', None)
        or ''
    ).strip()
    if expected_model and expected_model != 'unknown':
        if not live_model:
            return 'RESPAWN', 'live_model_identity_missing', None
        if live_model != expected_model:
            return 'RESPAWN', 'live_model_identity_mismatch', None
    expected_provider = str(frozen.get('provider') or '').strip()
    live_provider = str(
        getattr(resident, 'provider', None)
        or getattr(resident, 'provider_identity', None)
        or getattr(resident, '_provider', None)
        or ''
    ).strip()
    if live_provider and expected_provider and live_provider != expected_provider:
        return 'RESPAWN', 'live_provider_identity_mismatch', None
    expected_system_hash = str(
        plan.manifest.get('static_system_sha256') or ''
    ).strip()
    live_system = str(
        getattr(resident, 'system_text', None)
        or getattr(resident, '_system_text', None)
        or ''
    )
    if expected_system_hash:
        if not live_system:
            return 'RESPAWN', 'live_system_identity_missing', None
        if _sha256_text(live_system) != expected_system_hash:
            return 'RESPAWN', 'live_system_identity_mismatch', None
    live_persona_hash = str(
        getattr(resident, 'persona_sha256', None)
        or getattr(resident, '_persona_sha256', None)
        or ''
    ).strip()
    expected_persona_hash = str(
        plan.manifest.get('persona_sha256') or ''
    ).strip()
    if expected_persona_hash and live_persona_hash:
        if live_persona_hash != expected_persona_hash:
            return 'RESPAWN', 'live_persona_identity_mismatch', None

    peek = getattr(resident, 'peek_respawn_reason', None)
    if callable(peek):
        try:
            live_reason = peek(live_system, tool_profile=plan.tool_profile)
        except Exception:
            return 'BLOCKED', 'live_tool_surface_identity_unavailable', None
        if live_reason:
            return 'RESPAWN', 'live_provider_identity_mismatch', None
    if plan.tool_profile == DAILY_TOOL_PROFILE:
        tool_surface = str(
            getattr(resident, 'bound_tool_surface_fingerprint', None)
            or getattr(resident, '_bound_tool_surface_fingerprint', None)
            or ''
        ).strip()
        if not tool_surface:
            return 'RESPAWN', 'live_tool_surface_identity_missing', None

    try:
        registry = get_context_claude_session(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
    except (OSError, sqlite3.Error):
        return 'BLOCKED', 'session_registry_unavailable', None
    if registry is None:
        return 'RESPAWN', 'session_registry_missing', None
    try:
        if (
            int(registry.get('context_id')) != int(plan.context_id)
            or int(registry.get('resident_generation'))
                != int(plan.resident_generation)
            or int(registry.get('context_epoch')) != int(plan.context_epoch)
            or str(registry.get('chat_id') or '') != str(plan.chat_id)
        ):
            return 'BLOCKED', 'session_registry_identity_ambiguous', registry
    except (TypeError, ValueError):
        return 'BLOCKED', 'session_registry_identity_invalid', registry

    if str(registry.get('claude_session_id') or '') != live_sid:
        return 'RESPAWN', 'session_identity_mismatch', registry
    if registry.get('process_generation') is None:
        return 'RESPAWN', 'session_registry_process_generation_missing', registry
    try:
        registry_process_generation = int(registry['process_generation'])
    except (TypeError, ValueError):
        return 'BLOCKED', 'session_registry_process_generation_invalid', registry
    if registry_process_generation != live_generation:
        return 'RESPAWN', 'process_generation_mismatch', registry
    if binding.claude_session_id and binding.claude_session_id != live_sid:
        return 'RESPAWN', 'binding_session_identity_mismatch', registry

    try:
        owner = dc.get_resident_owner(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
    except (OSError, sqlite3.Error):
        return 'BLOCKED', 'resident_owner_unavailable', registry
    if owner is None:
        return 'RESPAWN', 'resident_owner_missing', registry
    try:
        if (
            int(owner.get('context_id')) != int(plan.context_id)
            or int(owner.get('resident_generation'))
                != int(plan.resident_generation)
            or str(owner.get('worker_id') or '') != str(plan.worker_id)
            or str(owner.get('resident_key') or '') != str(plan.resident_key)
            or int(owner.get('process_generation') or 0) != live_generation
        ):
            return 'RESPAWN', 'resident_owner_identity_mismatch', registry
    except (TypeError, ValueError):
        return 'RESPAWN', 'resident_owner_identity_invalid', registry

    receipt = frozen.get('receipt')
    if receipt is not None:
        try:
            if (
                int(receipt.context_id) != int(plan.context_id)
                or int(receipt.context_epoch) != int(plan.context_epoch)
                or int(receipt.resident_generation) != int(plan.resident_generation)
            ):
                return 'BLOCKED', 'receipt_window_identity_ambiguous', registry
            receipt_process_generation = int(receipt.process_generation)
            receipt_watermark = int(receipt.installed_source_watermark)
        except (TypeError, ValueError):
            return 'BLOCKED', 'receipt_window_identity_invalid', registry
        if str(receipt.resident_key) != str(plan.resident_key):
            return 'RESPAWN', 'receipt_resident_key_mismatch', registry
        if str(receipt.provider) != str(frozen['provider']):
            return 'RESPAWN', 'receipt_provider_mismatch', registry
        if str(receipt.model_identity) != str(frozen['model_identity']):
            return 'RESPAWN', 'receipt_model_mismatch', registry
        if str(receipt.session_id) != live_sid:
            return 'RESPAWN', 'receipt_session_identity_mismatch', registry
        if receipt_process_generation != live_generation:
            return 'RESPAWN', 'receipt_process_generation_mismatch', registry
        try:
            binding_cursor = binding.bound_cursor_message_id
            owner_cursor = owner.get('bound_cursor_message_id')
            db_cursor = dc.get_resident_history_cursor(
                int(plan.context_id),
                int(plan.resident_generation),
                db_path=plan.db_path,
            )
            if (
                binding_cursor is None
                or owner_cursor is None
                or db_cursor is None
                or int(binding_cursor) != receipt_watermark
                or int(owner_cursor) != receipt_watermark
                or int(db_cursor) != receipt_watermark
            ):
                return 'RESPAWN', 'resident_cursor_watermark_mismatch', registry
        except (TypeError, ValueError):
            return 'RESPAWN', 'resident_cursor_identity_invalid', registry
        except (OSError, sqlite3.Error):
            return 'BLOCKED', 'resident_cursor_unavailable', registry
    return 'PASS', '', registry


def _hot_historical_compatibility(
    *,
    receipt_members: tuple[Any, ...],
    desired_members: tuple[Any, ...],
    native_tail_member: Any,
) -> tuple[bool, str]:
    installed = tuple(
        member for member in receipt_members
        if str(getattr(member, 'representation_kind', '') or '')
            in _HOT_HISTORICAL_REPRESENTATION_KINDS
    )
    desired = tuple(
        member for member in desired_members
        if str(getattr(member, 'representation_kind', '') or '')
            in _HOT_HISTORICAL_REPRESENTATION_KINDS
    )
    installed_keys = tuple(_receipt_member_source_identity(member) for member in installed)
    desired_keys = tuple(_receipt_member_source_identity(member) for member in desired)
    if len(set(installed_keys)) != len(installed_keys):
        return False, 'installed_historical_members_ambiguous'
    if len(set(desired_keys)) != len(desired_keys):
        return False, 'desired_historical_members_ambiguous'
    tail_key = _source_member_identity(native_tail_member)
    if tail_key not in desired_keys:
        return False, 'native_tail_not_in_desired_plan'
    virtual_tail = tail_key not in installed_keys
    if virtual_tail:
        if desired_keys[-1] != tail_key:
            return False, 'native_tail_order_mismatch'
        if str(getattr(desired[-1], 'representation_kind', '') or '') != 'raw':
            return False, 'native_tail_requires_raw_representation'
        installed_keys = installed_keys + (tail_key,)
    if installed_keys != desired_keys:
        return False, 'historical_source_members_changed'

    paired_installed: tuple[Any, ...] = installed
    if virtual_tail:
        paired_installed = installed + (None,)
    if len(paired_installed) != len(desired):
        return False, 'historical_source_members_changed'
    for existing, desired_member in zip(paired_installed, desired):
        if existing is None:
            # The only member allowed to be newly proven is the native tail;
            # it is raw native history, not a historical APPEND primitive.
            continue
        if str(getattr(existing, 'representation_kind', '') or '') != str(
            getattr(desired_member, 'representation_kind', '') or ''
        ):
            return False, 'historical_representation_identity_changed'
        if str(getattr(desired_member, 'representation_kind', '') or '') == 'chunk':
            if str(getattr(existing, 'representation_id', '') or '') != str(
                getattr(desired_member, 'representation_id', '') or ''
            ):
                return False, 'historical_representation_identity_changed'
    return True, ''

def _reconcile_hot_context_plan(
    plan: DailyTurnPlan,
    *,
    resident: Any,
) -> str:
    desired = getattr(plan, 'hot_desired_plan', None)
    frozen = getattr(plan, 'hot_receipt_frozen', None) or {}
    decision = 'BLOCKED'
    reason = 'hot_context_plan_missing'
    if desired is None:
        _set_hot_decision(plan, decision, reason)
        return decision
    if not bool(getattr(desired, 'valid', False)):
        reason = 'desired_context_plan_invalid'
        _set_hot_decision(plan, decision, reason)
        return decision
    try:
        desired_budget_status = str(getattr(desired, 'budget_status', '') or '')
        desired_gaps = tuple(getattr(desired, 'gaps', ()) or ())
        desired_representations = tuple(
            getattr(desired, 'representations', ()) or ()
        )
        current_request_sections = tuple(
            section for section in tuple(
                getattr(desired, 'ordered_sections', ()) or ()
            )
            if str(getattr(section, 'kind', '') or '') == 'current_request'
        )
        if desired_budget_status not in ('fit', 'unbounded') or desired_gaps:
            reason = 'desired_context_plan_budget_or_coverage_invalid'
            _set_hot_decision(plan, decision, reason)
            return decision
        if any(
            str(getattr(representation, 'kind', '') or '')
                not in _HOT_HISTORICAL_REPRESENTATION_KINDS
            for representation in desired_representations
        ):
            reason = 'desired_context_plan_representation_invalid'
            _set_hot_decision(plan, decision, reason)
            return decision
        if len(current_request_sections) != 1:
            reason = 'desired_context_plan_current_request_ambiguous'
            _set_hot_decision(plan, decision, reason)
            return decision
    except Exception:
        reason = 'desired_context_plan_proof_unavailable'
        _set_hot_decision(plan, decision, reason)
        return decision

    identity_decision, identity_reason, registry = _hot_live_identity_decision(
        plan,
        resident=resident,
        frozen=frozen,
    )
    if identity_decision != 'PASS':
        _set_hot_decision(plan, identity_decision, identity_reason)
        return identity_decision
    if frozen.get('receipt_read_error'):
        _set_hot_decision(plan, 'BLOCKED', 'installed_receipt_unavailable')
        return 'BLOCKED'
    receipt = frozen.get('receipt')
    if receipt is None:
        _set_hot_decision(plan, 'RESPAWN', 'installed_receipt_missing')
        return 'RESPAWN'
    if not bool(frozen.get('receipt_members_complete')) or not bool(
        frozen.get('membership_valid')
    ):
        _set_hot_decision(plan, 'RESPAWN', 'installed_receipt_proof_incomplete')
        return 'RESPAWN'

    desired_members = _context_receipt_members(plan)
    installed_members = tuple(frozen.get('members') or ())
    desired_fixed = tuple(
        member for member in desired_members
        if str(getattr(member, 'source_kind', '') or '')
            in _HOT_FIXED_SECTION_KINDS
    )
    installed_fixed = tuple(
        member for member in installed_members
        if str(getattr(member, 'source_kind', '') or '')
            in _HOT_FIXED_SECTION_KINDS
    )
    desired_fixed_kinds = tuple(
        str(getattr(member, 'source_kind', '') or '') for member in desired_fixed
    )
    installed_fixed_kinds = tuple(
        str(getattr(member, 'source_kind', '') or '') for member in installed_fixed
    )
    if set(desired_fixed_kinds) - set(installed_fixed_kinds):
        _set_hot_decision(plan, 'RESPAWN', 'legacy_receipt_fixed_proof_incomplete')
        return 'RESPAWN'
    if (
        desired_fixed_kinds != installed_fixed_kinds
        or tuple(map(_receipt_member_identity, desired_fixed))
            != tuple(map(_receipt_member_identity, installed_fixed))
    ):
        _set_hot_decision(plan, 'RESPAWN', 'fixed_context_plan_mismatch')
        return 'RESPAWN'

    desired_policy_version = str(
        getattr(desired, 'budget_policy_version', '')
        or 'continuity_context_budget_v1'
    )
    desired_measurement = str(getattr(desired, 'measurement_semantics', '') or '')
    if str(receipt.budget_policy_version) != desired_policy_version:
        _set_hot_decision(plan, 'RESPAWN', 'budget_policy_version_mismatch')
        return 'RESPAWN'
    if str(receipt.measurement_semantics) != desired_measurement:
        _set_hot_decision(plan, 'RESPAWN', 'measurement_semantics_mismatch')
        return 'RESPAWN'

    tail = _hot_native_tail_proof(
        plan,
        frozen=frozen,
        registry=registry,
    )
    if tail.get('status') == 'ambiguous':
        _set_hot_decision(plan, 'BLOCKED', str(tail.get('reason') or 'native_tail_ambiguous'))
        return 'BLOCKED'
    if tail.get('status') != 'pass' or tail.get('source_member') is None:
        _set_hot_decision(plan, 'RESPAWN', str(tail.get('reason') or 'native_tail_missing'))
        return 'RESPAWN'

    compatible, compatibility_reason = _hot_historical_compatibility(
        receipt_members=installed_members,
        desired_members=desired_members,
        native_tail_member=tail['source_member'],
    )
    if not compatible:
        _set_hot_decision(plan, 'RESPAWN', compatibility_reason)
        return 'RESPAWN'

    plan.manifest.update({
        'context_plan_fixed_section_parity': 'PASS',
        'context_plan_representation_parity': 'PASS',
        'context_plan_current_request_count': 1,
        'context_plan_native_tail_proof': 'PASS',
        'context_plan_budget_status': str(getattr(desired, 'budget_status', '')),
    })
    _set_hot_decision(plan, 'NO_OP', '')
    return 'NO_OP'


def _set_hot_decision(plan: DailyTurnPlan, decision: str, reason: str) -> None:
    plan.hot_decision = str(decision)
    plan.hot_decision_reason = str(reason or '')
    plan.manifest.update({
        'context_plan_hot_decision': str(decision),
        'context_plan_hot_decision_reason': str(reason or ''),
    })


def _prepare_hot_context_plan(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    static_system: str,
) -> str:
    try:
        context_plan, chunk_bodies = _build_production_context_plan(
            plan,
            resident=resident,
            static_system=static_system,
            mode='hot',
        )
    except DailyRuntimeError as exc:
        _set_hot_decision(plan, 'BLOCKED', str(exc.error_code))
        raise
    except (TypeError, ValueError) as exc:
        _set_hot_decision(plan, 'BLOCKED', str(exc))
        raise DailyRuntimeError(
            'normal hot ContextPlan build blocked: %s' % str(exc),
            error_code='context_plan_hot_blocked',
            retryable=False,
        ) from exc
    plan.hot_desired_plan = context_plan
    plan.hot_desired_chunk_bodies = dict(chunk_bodies)
    plan.manifest.update({
        'context_plan_consumer': 'canonical_hot',
        'context_plan_hot_pending': False,
        'context_plan_id': str(context_plan.plan_id),
        'context_plan_hash': str(context_plan.plan_hash),
        'context_plan_valid': bool(context_plan.valid),
        'context_plan_budget_status': str(context_plan.budget_status),
        'context_plan_source_hash': str(context_plan.source_hash),
    })
    _freeze_hot_receipt(plan, resident=resident)
    return _reconcile_hot_context_plan(plan, resident=resident)


def _same_context_last_good_matches(plan: DailyTurnPlan) -> bool:
    sid = str(plan.transcript_claude_session_id or '').strip()
    if not sid or plan.transcript_start_offset is None or plan.transcript_end_offset is None:
        return False
    try:
        transcript_start = int(plan.transcript_start_offset)
        transcript_end = int(plan.transcript_end_offset)
    except (TypeError, ValueError):
        return False
    if transcript_end <= transcript_start:
        return False
    last_good = get_same_context_last_good(
        int(plan.context_id),
        int(plan.context_epoch),
    )
    try:
        return (
            isinstance(last_good, dict)
            and int(last_good.get('context_id') or 0) == int(plan.context_id)
            and int(last_good.get('context_epoch') or 0) == int(plan.context_epoch)
            and int(last_good.get('resident_generation') or 0)
                == int(plan.resident_generation)
            and str(last_good.get('claude_session_id') or '') == sid
            and int(last_good.get('transcript_end_offset') or -1)
                == transcript_end
        )
    except (TypeError, ValueError):
        return False


def _commit_production_context_receipt(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
) -> bool:
    """Commit durable metadata proof after the existing full-success boundary."""
    from chat import context_receipt as receipt_store
    sid = str(plan.transcript_claude_session_id or '').strip()
    process_generation = plan.transcript_process_generation
    if (
        str(plan.manifest.get('transcript_mapping_status') or '') != 'MAPPED'
        or not sid
        or process_generation is None
        or int(process_generation) <= 0
    ):
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_receipt_unavailable',
            'error_code': 'context_receipt_unavailable',
        })
        return False
    context_plan = _context_plan_for_receipt(plan)
    if context_plan is None:
        return False
    hot_reconciliation = getattr(plan, 'hot_desired_plan', None) is not None
    if hot_reconciliation and getattr(plan, 'hot_decision', None) != 'NO_OP':
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_plan_hot_not_no_op',
            'error_code': 'context_plan_hot_not_no_op',
        })
        return False
    cursor_after_raw = plan.manifest.get('cursor_after')
    try:
        assistant_manifest_id = int(
            plan.manifest.get('assistant_message_id') or 0
        )
    except (TypeError, ValueError):
        assistant_manifest_id = 0
    if (
        isinstance(cursor_after_raw, bool)
        or plan.manifest.get('cursor_cas_success') is not True
        or assistant_manifest_id != int(assistant_message_id)
    ):
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_receipt_cursor_unavailable',
            'error_code': 'context_receipt_cursor_unavailable',
        })
        return False
    try:
        cursor_after = int(cursor_after_raw)
        persisted_cursor = dc.get_resident_history_cursor(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
    except (TypeError, ValueError, OSError, sqlite3.Error):
        cursor_after = 0
        persisted_cursor = None
    if (
        cursor_after <= 0
        or cursor_after < int(assistant_message_id)
        or persisted_cursor != cursor_after
    ):
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_receipt_cursor_unavailable',
            'error_code': 'context_receipt_cursor_unavailable',
        })
        return False
    if (
        plan.manifest.get('context_receipt_last_good_proven') is not True
        or not getattr(plan, '_same_context_last_good_proven', False)
        or not _same_context_last_good_matches(plan)
    ):
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_receipt_last_good_unproven',
            'error_code': 'context_receipt_last_good_unproven',
        })
        return False
    members = _context_receipt_members(plan)
    try:
        receipt = receipt_store.ContextReceipt.build(
            context_id=int(plan.context_id),
            context_epoch=int(plan.context_epoch),
            resident_generation=int(plan.resident_generation),
            resident_key=str(plan.resident_key),
            provider=str(plan.manifest.get('provider') or 'claude_code'),
            model_identity=str(plan.manifest.get('model') or 'unknown'),
            session_id=sid,
            process_generation=int(process_generation),
            plan_id=str(context_plan.plan_id),
            plan_hash=str(context_plan.plan_hash),
            budget_policy_version=str(
                context_plan.budget_policy_version
                or 'continuity_context_budget_v1'
            ),
            measurement_semantics=str(context_plan.measurement_semantics),
            installed_source_watermark=cursor_after,
            members=members,
        )
        conn = dc._connect(plan.db_path)
        try:
            receipt_store.ensure_context_receipt_schema(conn)
            if hot_reconciliation:
                frozen = getattr(plan, 'hot_receipt_frozen', None) or {}
                frozen_receipt = frozen.get('receipt')
                expected_revision = frozen.get('expected_receipt_revision')
                if frozen_receipt is None or expected_revision is None:
                    raise receipt_store.ContextReceiptConflict(
                        'hot receipt revision was not frozen before send'
                    )
                committed = receipt_store.hot_advance_receipt(
                    conn,
                    expected_receipt_revision=int(expected_revision),
                    receipt=receipt,
                    members=members,
                )
            else:
                existing = receipt_store.get_receipt(
                    conn,
                    context_id=int(plan.context_id),
                    context_epoch=int(plan.context_epoch),
                    resident_generation=int(plan.resident_generation),
                )
                if existing is None:
                    committed = receipt_store.create_receipt(conn, receipt, members)
                else:
                    committed = receipt_store.hot_advance_receipt(
                        conn,
                        expected_receipt_revision=int(existing.receipt_revision),
                        receipt=receipt,
                        members=members,
                    )
        finally:
            conn.close()
        plan.manifest.update({
            'context_receipt_status': 'COMMITTED',
            'context_receipt_repair_required': False,
            'context_receipt_plan_hash': str(committed.plan_hash),
            'context_receipt_revision': int(committed.receipt_revision),
            'context_receipt_assistant_message_id': int(assistant_message_id),
        })
        return True
    except Exception:
        logger.exception('production context receipt commit failed')
        plan.manifest.update({
            'context_receipt_status': 'REPAIR_REQUIRED',
            'context_receipt_repair_required': True,
            'context_receipt_error_code': 'context_receipt_unavailable',
            'error_code': 'context_receipt_unavailable',
        })
        return False



def _commit_capacity_source_receipt_supersession(
    plan: DailyTurnPlan,
    *,
    target_receipt_committed: bool,
) -> bool:
    """CAS-supersede the frozen source receipt only after target proof."""
    if not target_receipt_committed:
        plan.manifest['capacity_source_receipt_supersession'] = (
            'NOT_ATTEMPTED_TARGET_RECEIPT_FAILED'
        )
        return False
    if plan.manifest.get('capacity_source_receipt_supersession_attempted'):
        return plan.manifest.get(
            'capacity_source_receipt_supersession'
        ) == 'COMMITTED'
    plan.manifest['capacity_source_receipt_supersession_attempted'] = True

    frozen = getattr(plan, 'capacity_source_receipt_frozen', None)
    if not isinstance(frozen, dict):
        plan.manifest.update({
            'capacity_source_receipt_supersession': 'REPAIR_REQUIRED',
            'capacity_source_receipt_supersession_error': (
                'capacity_source_receipt_not_frozen'
            ),
            'context_receipt_repair_required': True,
            'error_code': 'context_receipt_supersession_not_frozen',
        })
        return False
    source_receipt = frozen.get('receipt')
    if source_receipt is None:
        plan.manifest.update({
            'capacity_source_receipt_supersession': 'SOURCE_RECEIPT_ABSENT',
            'capacity_source_receipt_supersession_skipped': True,
        })
        return True

    try:
        source_context_id = int(frozen['context_id'])
        source_context_epoch = int(frozen['context_epoch'])
        source_generation = int(frozen['resident_generation'])
        expected_revision = int(frozen['expected_receipt_revision'])
        target_generation = int(plan.resident_generation)
    except (KeyError, TypeError, ValueError) as exc:
        plan.manifest.update({
            'capacity_source_receipt_supersession': 'REPAIR_REQUIRED',
            'capacity_source_receipt_supersession_error': (
                'capacity_source_receipt_identity_invalid'
            ),
            'context_receipt_repair_required': True,
            'error_code': 'context_receipt_supersession_identity_invalid',
        })
        logger.exception('capacity source receipt identity invalid')
        return False

    if (
        source_context_id != int(plan.context_id)
        or source_context_epoch != int(plan.context_epoch)
        or target_generation != source_generation + 1
    ):
        plan.manifest.update({
            'capacity_source_receipt_supersession': 'REPAIR_REQUIRED',
            'capacity_source_receipt_supersession_error': (
                'capacity_source_target_generation_mismatch'
            ),
            'context_receipt_repair_required': True,
            'error_code': 'context_receipt_supersession_identity_invalid',
        })
        return False

    from chat import context_receipt as receipt_store
    conn = None
    try:
        conn = dc._connect(plan.db_path)
        receipt_store.mark_superseded(
            conn,
            context_id=source_context_id,
            context_epoch=source_context_epoch,
            resident_generation=source_generation,
            expected_receipt_revision=expected_revision,
            target_generation=target_generation,
        )
    except Exception as exc:
        logger.exception('capacity source receipt supersession failed')
        plan.manifest.update({
            'capacity_source_receipt_supersession': 'REPAIR_REQUIRED',
            'capacity_source_receipt_supersession_error': type(exc).__name__,
            'context_receipt_repair_required': True,
            'error_code': 'context_receipt_supersession_conflict',
        })
        return False
    finally:
        if conn is not None:
            conn.close()
    plan.manifest.update({
        'capacity_source_receipt_supersession': 'COMMITTED',
        'capacity_source_receipt_superseded': True,
    })
    return True


def _provider_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get('type') == 'text':
            return str(content.get('text') or '')
        return ''
    if isinstance(content, (list, tuple)):
        return NL.join(
            text for item in content
            for text in (_provider_content_text(item),)
            if text
        )
    return ''


def _validate_hot_no_op_payload(plan: DailyTurnPlan, content: Any) -> None:
    """Keep NO_OP on the existing incremental payload contract."""
    if getattr(plan, 'hot_decision', None) != 'NO_OP':
        return
    assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
    if assembly.get('context_plan_representation_blocks'):
        raise DailyRuntimeError(
            'normal hot NO_OP contains ContextPlan history replay',
            error_code='context_plan_hot_history_replay',
        )
    if assembly.get('carryover_messages') or str(
        assembly.get('day_handoff') or ''
    ).strip():
        raise DailyRuntimeError(
            'normal hot NO_OP contains legacy handoff/carryover replay',
            error_code='context_plan_hot_legacy_replay',
        )
    text = _provider_content_text(content)
    user_text = str(plan.user_content or '')
    suffix = str(plan.provider_display_thinking_suffix or '')
    if suffix and text.endswith(suffix):
        text = text[:-len(suffix)]
    if user_text.strip() and user_text.strip() != '[image]' and not text.endswith(user_text):
        raise DailyRuntimeError(
            'normal hot current request is not installed exactly once',
            error_code='context_plan_hot_current_request_parity_failed',
        )
    plan.manifest['context_plan_current_request_count'] = 1


def _section_install_identity(section: Any) -> tuple[str, str, str, int]:
    return (
        str(getattr(section, 'kind', '') or ''),
        str(getattr(section, 'source_ref', '') or ''),
        str(getattr(section, 'content_hash', '') or ''),
        int(getattr(section, 'estimated_tokens', 0) or 0),
    )


def _validate_production_context_install(
    *,
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
    content: Any,
) -> None:
    """Fail closed unless the consumed plan is the provider-visible install."""
    context_plan = _context_plan_for_receipt(plan)
    capacity_split = _is_capacity_context_plan(plan)
    if capacity_split:
        plan.manifest['capacity_context_install_parity'] = 'PENDING'
    if context_plan is None:
        raise DailyRuntimeError(
            'production ContextPlan is missing',
            error_code='context_plan_unavailable',
        )
    assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
    if assembly.get('carryover_messages'):
        raise DailyRuntimeError(
            'legacy carryover is still present beside ContextPlan',
            error_code='context_plan_legacy_carryover_forbidden',
        )
    if str(assembly.get('day_handoff') or '').strip():
        raise DailyRuntimeError(
            'legacy handoff replay is still present beside ContextPlan',
            error_code='context_plan_legacy_handoff_forbidden',
        )

    expected_fixed = _build_continuity_shadow_fixed_sections(
        plan=plan,
        resident=None if capacity_split else resident,
        static_system=static_system,
        require_full_state_snapshot=True,
    )
    actual_fixed = tuple(
        section for section in context_plan.ordered_sections
        if str(getattr(section, 'kind', '') or '') in {
            'invariant_system',
            'accepted_state',
            'accepted_open_loops',
        }
    )
    if tuple(map(_section_install_identity, expected_fixed)) != tuple(
        map(_section_install_identity, actual_fixed)
    ):
        raise DailyRuntimeError(
            'ContextPlan fixed-section proof does not match install',
            error_code='context_plan_fixed_section_parity_failed',
        )

    if capacity_split:
        actual_system = str(
            getattr(resident, '_system_text', '') or static_system or ''
        )
        invariant_sections = tuple(
            section for section in actual_fixed
            if str(getattr(section, 'kind', '') or '') == 'invariant_system'
        )
        if (
            len(invariant_sections) != 1
            or _sha256_text(actual_system)
                != str(invariant_sections[0].content_hash or '')
            or CAPACITY_BOUNDARY_SYSTEM_SUFFIX_V1 not in actual_system
        ):
            raise DailyRuntimeError(
                'Capacity target system does not match ContextPlan fixed proof',
                error_code='context_plan_capacity_fixed_parity_failed',
            )

    current_sections = tuple(
        section for section in context_plan.ordered_sections
        if str(getattr(section, 'kind', '') or '') == 'current_request'
    )
    if (
        len(current_sections) != 1
        or str(current_sections[0].source_ref or '')
            != 'message:%d' % int(plan.user_message_id)
    ):
        raise DailyRuntimeError(
            'ContextPlan current-request proof is invalid',
            error_code='context_plan_current_request_parity_failed',
        )
    try:
        from continuity.sources import evidence_ref
        current_row = _load_context_source_rows(
            {int(plan.user_message_id)},
            db_path=_production_continuity_store_path(plan),
        ).get(int(plan.user_message_id))
        if current_row is None:
            raise ValueError('current request source row is unavailable')
        current_evidence = evidence_ref(current_row, prefix='message')
        current_identity = (
            str(current_sections[0].source_ref or ''),
            str(current_sections[0].content_hash or ''),
            int(current_sections[0].estimated_tokens or 0),
        )
        expected_current_identity = (
            str(current_evidence.source_ref),
            str(current_evidence.content_hash),
            int(current_evidence.logical_size),
        )
    except Exception as exc:
        raise DailyRuntimeError(
            'ContextPlan current-request source is unavailable',
            error_code='context_plan_current_request_parity_failed',
        ) from exc
    if current_identity != expected_current_identity:
        raise DailyRuntimeError(
            'ContextPlan current-request identity does not match install',
            error_code='context_plan_current_request_parity_failed',
        )

    if capacity_split:
        if not assembly.get('capacity_context_bootstrap'):
            raise DailyRuntimeError(
                'Capacity first-stdin bootstrap is missing',
                error_code='context_plan_capacity_bootstrap_missing',
            )
        if assembly.get('context_plan_representation_blocks') and any(
            str(block.get('kind') or '') == 'raw'
            for block in assembly.get('context_plan_representation_blocks')
            if isinstance(block, dict)
        ):
            raise DailyRuntimeError(
                'raw ContextPlan history was replayed in target stdin',
                error_code='context_plan_capacity_raw_replayed',
            )
        raw_refs = tuple(
            str(member.source_ref)
            for representation in tuple(context_plan.representations)
            if str(getattr(representation, 'kind', '') or '') == 'raw'
            for member in tuple(getattr(representation, 'source_members', ()) or ())
        )
        installed_raw_refs = tuple(
            str(value)
            for value in (plan.manifest.get('capacity_context_raw_source_refs') or ())
        )
        if (
            plan.manifest.get('capacity_context_raw_carrier_parity') != 'PASS'
            or installed_raw_refs != raw_refs
        ):
            raise DailyRuntimeError(
                'Capacity raw carrier does not match ContextPlan',
                error_code='context_plan_capacity_raw_parity_failed',
            )
        chunk_refs = tuple(
            str(member.source_ref)
            for representation in tuple(context_plan.representations)
            if str(getattr(representation, 'kind', '') or '') == 'chunk'
            for member in tuple(getattr(representation, 'source_members', ()) or ())
        )
        if len(chunk_refs) != len(set(chunk_refs)):
            raise DailyRuntimeError(
                'Capacity ContextPlan chunk membership overlaps',
                error_code='context_plan_capacity_chunk_parity_failed',
            )
        if set(raw_refs) & set(chunk_refs):
            raise DailyRuntimeError(
                'raw and chunk ContextPlan membership overlaps',
                error_code='context_plan_capacity_representation_overlap',
            )
    blocks = assembly.get('context_plan_representation_blocks') or []
    expected_blocks = tuple(
        representation for representation in context_plan.representations
        if not capacity_split
        or str(getattr(representation, 'kind', '') or '') == 'chunk'
    )
    if len(blocks) != len(expected_blocks):
        raise DailyRuntimeError(
            'ContextPlan representation install is incomplete',
            error_code='context_plan_representation_parity_failed',
        )

    receipt = assembly.get('_context_install_render_receipt')
    if not isinstance(receipt, dict):
        raise DailyRuntimeError(
            'ContextPlan structural render receipt is missing',
            error_code='context_plan_render_receipt_missing',
        )
    payload_hash, payload_tokens, payload_kind = (
        _continuity_shadow_fingerprint(content)
    )
    if (
        str(receipt.get('final_payload_hash') or '') != str(payload_hash)
        or int(receipt.get('final_payload_token_estimate', -1) or -1)
            != int(payload_tokens)
        or str(receipt.get('final_payload_kind') or '') != str(payload_kind)
    ):
        raise DailyRuntimeError(
            'provider payload differs from structural render receipt',
            error_code='context_plan_render_receipt_mismatch',
        )
    try:
        current_request_slots = int(
            receipt.get('current_request_slots', 0) or 0
        )
    except (TypeError, ValueError):
        current_request_slots = 0
    if (
        current_request_slots != 1
        or str(receipt.get('current_request_carrier') or '') != 'tail'
    ):
        raise DailyRuntimeError(
            'current request structural carrier is not exactly once',
            error_code='context_plan_current_request_parity_failed',
        )

    expected_representation_ids = tuple(
        str(getattr(representation, 'representation_id', '') or '')
        for representation in expected_blocks
    )
    installed_representation_ids = tuple(
        str(value or '')
        for value in (receipt.get('representation_ids') or ())
    )
    if installed_representation_ids != expected_representation_ids:
        raise DailyRuntimeError(
            'ContextPlan representation carrier identity does not match render',
            error_code='context_plan_representation_parity_failed',
        )

    expected_body_hashes = []
    for block, representation in zip(blocks, expected_blocks):
        if (
            not isinstance(block, dict)
            or str(block.get('kind') or '') != str(representation.kind)
            or str(block.get('representation_id') or '')
                != str(representation.representation_id)
        ):
            raise DailyRuntimeError(
                'ContextPlan representation identity does not match install',
                error_code='context_plan_representation_parity_failed',
            )
        body = str(block.get('body') or '').strip()
        if not body:
            raise DailyRuntimeError(
                'ContextPlan representation body is not installed',
                error_code='context_plan_representation_parity_failed',
            )
        if capacity_split and str(
            getattr(representation, 'kind', '') or ''
        ) == 'chunk':
            expected_body = _context_plan_chunk_bodies(plan).get(
                str(getattr(representation, 'chunk_id', '') or '')
            )
            if expected_body is not None and body != str(expected_body).strip():
                raise DailyRuntimeError(
                    'ContextPlan chunk body does not match install',
                    error_code='context_plan_representation_parity_failed',
                )
        expected_body_hashes.append(_sha256_text(body))

    installed_body_hashes = tuple(
        str(value or '')
        for value in (receipt.get('representation_body_hashes') or ())
    )
    if tuple(expected_body_hashes) != installed_body_hashes:
        raise DailyRuntimeError(
            'ContextPlan representation body proof does not match render',
            error_code='context_plan_representation_parity_failed',
        )



    if capacity_split:
        if assembly.get('current_day_history'):
            raise DailyRuntimeError(
                'Capacity current request has a second history carrier',
                error_code='context_plan_current_request_parity_failed',
            )
        current_source_ref = 'message:%d' % int(plan.user_message_id)
        if (
            plan.manifest.get('capacity_context_current_user_in_candidate')
            is not False
            or current_source_ref in raw_refs
        ):
            raise DailyRuntimeError(
                'current user is present in Capacity forged JSONL',
                error_code='context_plan_capacity_current_user_in_candidate',
            )

    state_text = str(assembly.get('state') or '').strip()
    has_state_section = any(
        str(getattr(section, 'kind', '') or '') == 'accepted_state'
        for section in actual_fixed
    )
    if bool(state_text) != has_state_section:
        raise DailyRuntimeError(
            'accepted state install does not match ContextPlan proof',
            error_code='context_plan_fixed_section_parity_failed',
        )
    if state_text and (
        not has_state_section
        or _sha256_text(state_text)
            != str(next(
                section for section in actual_fixed
                if str(getattr(section, 'kind', '') or '') == 'accepted_state'
            ).content_hash or '')
    ):
        raise DailyRuntimeError(
            'accepted state content does not match ContextPlan proof',
            error_code='context_plan_fixed_section_parity_failed',
        )

    open_loops_text = _format_context_plan_open_loops(
        assembly.get('context_plan_accepted_open_loops'),
    )
    has_open_loops_section = any(
        str(getattr(section, 'kind', '') or '') == 'accepted_open_loops'
        for section in actual_fixed
    )
    if bool(open_loops_text) != has_open_loops_section:
        raise DailyRuntimeError(
            'accepted open-loops install does not match ContextPlan proof',
            error_code='context_plan_fixed_section_parity_failed',
        )

    expected_fixed_carrier_kinds = tuple(
        kind for kind, value in (
            ('accepted_open_loops', open_loops_text),
            ('accepted_state', state_text),
        )
        if value
    )
    installed_fixed_carrier_kinds = tuple(
        str(value or '')
        for value in (receipt.get('fixed_section_kinds') or ())
    )
    if installed_fixed_carrier_kinds != expected_fixed_carrier_kinds:
        raise DailyRuntimeError(
            'ContextPlan fixed carrier identity does not match render',
            error_code='context_plan_fixed_section_parity_failed',
        )
    expected_fixed_body_hashes = tuple(
        _sha256_text(str(value))
        for value in (open_loops_text, state_text)
        if value
    )
    installed_fixed_body_hashes = tuple(
        str(value or '')
        for value in (receipt.get('fixed_section_body_hashes') or ())
    )
    if installed_fixed_body_hashes != expected_fixed_body_hashes:
        raise DailyRuntimeError(
            'ContextPlan fixed carrier body proof does not match render',
            error_code='context_plan_fixed_section_parity_failed',
        )

    plan.manifest.update({
        'context_plan_fixed_section_parity': 'PASS',
        'context_plan_representation_parity': 'PASS',
        'context_plan_current_request_count': 1,
        'context_plan_provider_content_hash': str(payload_hash),
    })
    if capacity_split:
        plan.manifest['capacity_context_install_parity'] = 'PASS'



def _observe_production_context_plan(
    *,
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
    content: Any,
) -> None:
    context_plan = _context_plan_for_receipt(plan)
    if context_plan is None:
        return
    _validate_production_context_install(
        plan=plan,
        resident=resident,
        static_system=static_system,
        content=content,
    )
    content_hash, content_tokens, content_kind = _continuity_shadow_fingerprint(content)
    representations = tuple(context_plan.representations)
    observation = {
        'event': 'continuity_shadow_observation',
        'status': 'ready',
        'error_code': None,
        'request_id': str(plan.request_id),
        'chat_id': str(plan.chat_id),
        'context_id': int(plan.context_id),
        'context_epoch': int(plan.context_epoch),
        'resident_generation': int(plan.resident_generation),
        'turn_kind': _continuity_shadow_turn_kind(plan),
        'production_content_hash': content_hash,
        'production_content_token_estimate': content_tokens,
        'production_content_kind': content_kind,
        'source_member_count': len(context_plan.source_members),
        'chunk_binding_count': sum(
            1 for item in representations if item.kind == 'chunk'
        ),
        'chunk_surface': 'ready' if _context_plan_chunk_bodies(plan) else 'empty',
        'plan_id': str(context_plan.plan_id),
        'plan_hash': str(context_plan.plan_hash),
        'plan_valid': bool(context_plan.valid),
        'budget_status': str(context_plan.budget_status),
        'token_budget': context_plan.token_budget,
        'reserve_budget': context_plan.reserve_budget,
        'recent_raw_target': (
            context_plan.budget_policy.recent_raw_target
            if context_plan.budget_policy is not None else None
        ),
        'selected_token_estimate': context_plan.selected_token_estimate,
        'fixed_section_token_estimate': context_plan.fixed_section_token_estimate,
        'total_token_estimate': context_plan.total_token_estimate,
        'remaining_budget': context_plan.remaining_budget,
        'raw_representation_count': sum(
            1 for item in representations if item.kind == 'raw'
        ),
        'chunk_representation_count': sum(
            1 for item in representations if item.kind == 'chunk'
        ),
        'gap_codes': sorted(str(item.code) for item in context_plan.gaps),
        'exclusion_codes': sorted(str(item.code) for item in context_plan.exclusions),
        'runtime_transition_source': _continuity_shadow_turn_kind(plan),
        'shadow_plan_available': True,
        'installed_context_proven': True,
        'source_proof_status': 'ready',
        'source_proof_error_code': None,
        'budget_error_code': None,
        'resident_session_present': bool(
            str(getattr(resident, 'session_id', None) or '').strip()
        ),
        'fixed_section_install_parity': 'PASS',
        'representation_install_parity': 'PASS',
    }
    logger.info(
        'continuity_shadow_observation %s',
        _continuity_shadow_canonical(observation),
    )

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
    context_plan_gate = _context_plan_consumer_enabled()
    context_plan_consumer = bool(cold_like and context_plan_gate)
    hot_context_plan_pending = bool(turn_kind == 'hot' and context_plan_gate)
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
        inject_handoff=bool(cold_like and not context_plan_consumer),
        inject_carryover=bool(cold_like and not context_plan_consumer),
        db_path=db_path,
        history_token_budget=history_token_budget,
        provider_claude_session_id=provider_sid,
        **({'history_override': []} if context_plan_consumer else {}),
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
    if context_plan_consumer:
        manifest['context_plan_runtime_overhead'] = (
            ['task_feedback'] if task_feedback else []
        )
    daily_plan = DailyTurnPlan(
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
    if context_plan_consumer:
        context_plan, chunk_bodies = _build_production_context_plan(
            daily_plan,
            resident=resident,
            static_system=static_system,
            mode='cold',
        )
        daily_plan.continuity_plan = context_plan
        daily_plan.continuity_chunk_bodies = dict(chunk_bodies)
        daily_plan.assembly = _project_context_plan_history(
            daily_plan,
            context_plan=context_plan,
            chunk_bodies=chunk_bodies,
        )
        daily_plan.manifest.update({
            'context_plan_consumer': 'canonical',
            'context_plan_id': str(context_plan.plan_id),
            'context_plan_hash': str(context_plan.plan_hash),
            'context_plan_valid': bool(context_plan.valid),
            'context_plan_budget_status': str(context_plan.budget_status),
            'context_plan_source_hash': str(context_plan.source_hash),
        })
    elif hot_context_plan_pending:
        # The normal-hot desired plan is deliberately deferred until after the
        # read-only Capacity Swap door in ensure_resident_and_stream().  A
        # capacity reason must not be turned into a hot store/budget failure.
        daily_plan.manifest.update({
            'context_plan_consumer': 'canonical_hot_pending',
            'context_plan_hot_pending': True,
        })
    return daily_plan


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

        # A process may die after mapping/registry commit A and before terminal
        # promotion/cursor commit B. Recovery itself re-checks each receipt's
        # context/generation lease while holding the write lock, so a live
        # terminalization is skipped rather than mistaken for crash residue.
        dc.recover_pending_terminalizations(db_path=db_path)

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
        reason = peek(
            effective,
            tool_profile=plan.tool_profile,
            allow_stale_cache_guard=True,
        )

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

    replacement = prepare_daily_turn(
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
    replacement.capacity_context_plan = getattr(plan, 'capacity_context_plan', None)
    replacement.capacity_context_chunk_bodies = dict(
        getattr(plan, 'capacity_context_chunk_bodies', None) or {}
    )
    replacement.capacity_context_bootstrap = copy.deepcopy(
        getattr(plan, 'capacity_context_bootstrap', None)
    )
    replacement.capacity_source_receipt_frozen = copy.deepcopy(
        getattr(plan, 'capacity_source_receipt_frozen', None)
    )
    return replacement


# Process/session identity transferred from staged (the new Claude process).
_CAPACITY_SWAP_PROCESS_IDENTITY = (
    '_proc', '_system_text', '_session_id', '_cold', '_generation',
    '_tool_profile', '_model_identity', '_history_rewrite_epoch', '_last_used',
    '_last_cache_refresh_at', '_last_cache_refresh_monotonic',
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
    provider_prompt = getattr(current, 'provider_display_thinking_prompt', '')
    for f in fields(DailyTurnPlan):
        setattr(current, f.name, getattr(replacement, f.name))
    current.provider_display_thinking_suffix = provider_suffix
    current.provider_display_thinking_prompt = provider_prompt
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


def _log_transcript_mapping_diagnostic(
    event_name: str,
    plan: DailyTurnPlan,
    *,
    error_code: Optional[str] = None,
    mapping_status: Optional[str] = None,
    mapping_event_count: Optional[int] = None,
    mapping_scan_offset: Optional[int] = None,
) -> None:
    """Emit metadata-only mapping diagnostics before rollback can erase state."""
    manifest = plan.manifest or {}
    code = error_code
    if code is None:
        code = manifest.get('transcript_mapping_error_code')
    status = mapping_status
    if status is None:
        status = manifest.get('transcript_mapping_status')
    event_count = mapping_event_count
    if event_count is None:
        event_count = manifest.get('transcript_mapping_event_count')
    scan_offset = mapping_scan_offset
    if scan_offset is None:
        scan_offset = manifest.get('transcript_mapping_scan_offset')
    logger.warning(
        '%s error_code=%s mapping_status=%s mapping_event_count=%s '
        'mapping_scan_offset=%s context_id=%s context_epoch=%s '
        'resident_generation=%s process_generation=%s session_id=%s '
        'start_offset=%s end_offset=%s observation_error=%s',
        str(event_name),
        str(code or 'transcript_mapping_blocked'),
        str(status or 'UNKNOWN'),
        event_count,
        scan_offset,
        plan.context_id,
        plan.context_epoch,
        plan.resident_generation,
        plan.transcript_process_generation,
        str(plan.transcript_claude_session_id or ''),
        plan.transcript_start_offset,
        plan.transcript_end_offset,
        str(plan.transcript_observation_error_code or ''),
    )


def _freeze_transcript_mapping_failure(plan: DailyTurnPlan) -> dict[str, Any]:
    """Freeze mapping metadata before rollback restores the registry snapshot."""
    manifest = plan.manifest or {}
    raw_scan_offset = manifest.get('transcript_mapping_scan_offset')
    try:
        scan_offset = int(raw_scan_offset) if raw_scan_offset is not None else None
    except (TypeError, ValueError):
        scan_offset = raw_scan_offset
    return {
        'mapping_status': str(
            manifest.get('transcript_mapping_status') or 'UNKNOWN'
        ),
        'mapping_error_code': str(
            manifest.get('transcript_mapping_error_code')
            or 'transcript_mapping_blocked'
        ),
        'mapping_event_count': int(
            manifest.get('transcript_mapping_event_count') or 0
        ),
        'mapping_scan_offset': scan_offset,
        'context_id': plan.context_id,
        'context_epoch': plan.context_epoch,
        'resident_generation': plan.resident_generation,
        'transcript_process_generation': plan.transcript_process_generation,
        'transcript_claude_session_id': str(
            plan.transcript_claude_session_id or ''
        ),
        'transcript_start_offset': plan.transcript_start_offset,
        'transcript_end_offset': plan.transcript_end_offset,
        'transcript_observation_error_code': str(
            plan.transcript_observation_error_code or ''
        ),
    }


def _capture_transcript_registry_snapshot(plan: DailyTurnPlan) -> Optional[dict[str, Any]]:
    """Capture the registry identity before this turn can mutate its watermark."""
    existing = get_context_claude_session(
        int(plan.context_id),
        int(plan.resident_generation),
        db_path=plan.db_path,
    )
    snapshot = dict(existing) if existing is not None else None
    plan._transcript_registry_before_mapping = snapshot
    return snapshot


def _rollback_failed_terminalization(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
) -> dict[str, Any]:
    """Remove a pending assistant only after proving its full turn identity."""
    snapshot = getattr(plan, '_transcript_registry_before_mapping', None)
    result = dc.rollback_daily_assistant_terminalization(
        assistant_message_id=int(assistant_message_id),
        context_id=int(plan.context_id),
        context_epoch=int(plan.context_epoch),
        resident_generation=int(plan.resident_generation),
        registry_snapshot=snapshot,
        mapping_receipt_id=getattr(plan, '_terminal_mapping_receipt_id', None),
        expected_claude_session_id=str(plan.transcript_claude_session_id or ''),
        expected_transcript_path=str(plan.transcript_path or ''),
        db_path=plan.db_path,
    )
    plan.manifest['terminal_rollback_status'] = 'ROLLED_BACK'
    plan.manifest['terminal_rollback'] = dict(result)
    return result


def _raise_terminal_rollback_unproven(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
    cause: Exception,
) -> None:
    plan.manifest['terminal_rollback_status'] = 'UNPROVEN'
    plan.manifest['terminal_rollback_error_code'] = type(cause).__name__
    abort_daily_turn(
        plan,
        error_code='terminal_rollback_unproven',
        respawn=False,
    )
    raise DailyRuntimeError(
        'failed terminalization rollback could not be proven',
        error_code='terminal_rollback_unproven',
    ) from cause


def finalize_transcript_mapping_after_success(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
) -> dict[str, Any]:
    """Register session + map events after the canonical assistant persist.

    Failures only update mapping manifest fields (BLOCKED). Never raises to
    this helper; the caller decides whether BLOCKED is terminal before cursor
    advancement.
    """
    try:
        existing = get_context_claude_session(
            int(plan.context_id),
            int(plan.resident_generation),
            db_path=plan.db_path,
        )
        plan._transcript_registry_before_mapping = (
            dict(existing) if existing is not None else None
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
            _log_transcript_mapping_diagnostic(
                'TRANSCRIPT_MAPPING_OBSERVATION_BLOCKED',
                plan,
                error_code=str(plan.transcript_observation_error_code),
                mapping_status='BLOCKED',
                mapping_event_count=0,
                mapping_scan_offset=None,
            )
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
                terminal_receipt={
                    'expected_cursor': plan.cursor_before,
                    'transcript_path': plan.transcript_path,
                    'claude_session_id': sid,
                    'transcript_start_offset': plan.transcript_start_offset,
                    'transcript_end_offset': plan.transcript_end_offset,
                    'pre_registry_snapshot': getattr(
                        plan, '_transcript_registry_before_mapping', None,
                    ),
                },
            ),
            db_path=plan.db_path,
        )
        if result.ok:
            if result.terminal_receipt_id is not None:
                plan._terminal_mapping_receipt_id = int(result.terminal_receipt_id)
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
            _log_transcript_mapping_diagnostic(
                'TRANSCRIPT_MAPPING_PASS_BLOCKED',
                plan,
                error_code=str(result.error_code or 'mapping_blocked'),
            )
    except SessionRegistryError as exc:
        error_code = str(getattr(exc, 'error_code', None) or 'registry_error')
        _log_transcript_mapping_diagnostic(
            'TRANSCRIPT_MAPPING_SESSION_REGISTRY_BLOCKED',
            plan,
            error_code=error_code,
            mapping_status='BLOCKED',
            mapping_event_count=0,
            mapping_scan_offset=None,
        )
        logger.warning(
            'transcript registry/mapping blocked: error_code=%s '
            'context_id=%s resident_generation=%s session_id=%s '
            'process_generation=%s',
            error_code,
            plan.context_id,
            plan.resident_generation,
            str(plan.transcript_claude_session_id or ''),
            plan.transcript_process_generation,
            exc_info=True,
        )
        _set_transcript_mapping_manifest(
            plan,
            status='BLOCKED',
            error_code=error_code,
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
    if getattr(plan, 'continuity_plan', None) is not None:
        raise DailyRuntimeError(
            'ContextPlan consumer cannot rebuild through legacy history selector',
            error_code='context_plan_legacy_selector_forbidden',
        )
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
        effective_history_budget,
        resident_rebuild_prompt_target,
        estimate_text_tokens,
        estimate_whole_prompt,
        should_refuse_no_benefit_hard_context_respawn,
    )
    from chat.context_lean import cc_history_token_budget

    resident_rebuild_target_val = resident_rebuild_prompt_target()
    cold_history_budget_val = int(
        (plan.assembly.get('manifest') or {}).get('cold_history_budget')
        or cc_history_token_budget()
    )
    cold_history_trimmed_flag = bool(
        (plan.assembly.get('manifest') or {}).get('cold_history_trimmed')
    )
    cold_prompt_estimate = estimate_whole_prompt(static_system, content)
    if cold_prompt_estimate > resident_rebuild_target_val:
        history = plan.assembly.get('current_day_history') or []
        history_tokens_est = estimate_text_tokens(_format_history_messages(history))
        non_history_est = max(0, cold_prompt_estimate - history_tokens_est)
        new_budget = effective_history_budget(
            default_history_budget=cold_history_budget_val,
            non_history_estimate=non_history_est,
            cold_target=resident_rebuild_target_val,
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

    if cold_prompt_estimate > resident_rebuild_target_val:
        plan.manifest['cold_budget_overflow'] = True
        plan.manifest['cold_prompt_estimate'] = int(cold_prompt_estimate)
        plan.manifest['cold_prompt_target'] = int(resident_rebuild_target_val)
        plan.manifest['cold_history_budget'] = int(cold_history_budget_val)
        plan.manifest['cold_budget_mode'] = 'token_budget'
        raise ColdBootstrapOverflow(
            estimate=cold_prompt_estimate,
            target=resident_rebuild_target_val,
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
    plan.manifest['cold_prompt_target'] = int(resident_rebuild_target_val)
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
    for attr in (
        '_continuity_shadow_capacity_pending',
        '_continuity_shadow_capacity_pending_error',
    ):
        if hasattr(plan, attr):
            delattr(plan, attr)
    if not is_capacity_swap_reason(trigger_reason):
        return {'ok': False, 'error_code': 'trigger_not_capacity'}

    hooks = staged_hooks or _default_capacity_swap_staged_hooks(resident)
    gate_on = _context_plan_consumer_enabled()
    prepare_result = None
    if gate_on:
        target_system = with_capacity_boundary_suffix(static_system)
        try:
            frozen = _freeze_hot_receipt(plan, resident=resident)
            plan.capacity_source_receipt_frozen = frozen
            context_plan, chunk_bodies = _build_production_context_plan(
                plan,
                resident=None,
                static_system=target_system,
                mode='capacity',
            )
            plan.capacity_context_plan = context_plan
            plan.capacity_context_chunk_bodies = dict(chunk_bodies)
            plan.capacity_context_bootstrap = _freeze_capacity_context_bootstrap(
                plan,
                context_plan=context_plan,
            )
            plan.manifest.update({
                'context_plan_consumer': 'canonical_capacity',
                'context_plan_id': str(context_plan.plan_id),
                'context_plan_hash': str(context_plan.plan_hash),
                'context_plan_valid': bool(context_plan.valid),
                'context_plan_budget_status': str(context_plan.budget_status),
                'context_plan_source_hash': str(context_plan.source_hash),
                'context_plan_capacity_source_receipt_revision': (
                    frozen.get('expected_receipt_revision')
                ),
            })
            prepare_result = prepare_capacity_swap_for_context_plan(
                plan=plan,
                context_plan=context_plan,
                trigger_reason=trigger_reason,
                static_system=target_system,
                claude_home=claude_home,
            )
        except DailyRuntimeError as exc:
            return {
                'ok': False,
                'error_code': str(exc.error_code),
                'detail': str(exc),
            }
        except Exception as exc:
            logger.exception('capacity ContextPlan preparation failed')
            return {
                'ok': False,
                'error_code': 'context_plan_capacity_prepare_failed',
                'detail': str(exc),
            }
    handoff = run_capacity_swap_handoff(
        plan=plan,
        trigger_reason=trigger_reason,
        static_system=static_system,
        env=env,
        live_resident=resident,
        staged_hooks=hooks,
        claude_home=claude_home,
        prepare_result=prepare_result,
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
        if gate_on:
            context_plan = getattr(plan, 'capacity_context_plan', None)
            if context_plan is None:
                raise DailyRuntimeError(
                    'capacity ContextPlan was lost during reprepare',
                    error_code='context_plan_capacity_lost_after_reprepare',
                )
            plan.assembly = _project_capacity_context_bootstrap(plan)
            raw_refs = tuple(
                str(member.source_ref)
                for representation in context_plan.representations
                if str(getattr(representation, 'kind', '') or '') == 'raw'
                for member in tuple(getattr(representation, 'source_members', ()) or ())
            )
            chunk_refs = tuple(
                str(member.source_ref)
                for representation in context_plan.representations
                if str(getattr(representation, 'kind', '') or '') == 'chunk'
                for member in tuple(getattr(representation, 'source_members', ()) or ())
            )
            selected_ids = tuple(
                int(value)
                for value in tuple(getattr(handoff.candidate, 'selected_message_ids', ()) or ())
                if int(value) > 0
            )
            if int(plan.user_message_id) in selected_ids:
                raise DailyRuntimeError(
                    'current user entered Capacity forged JSONL',
                    error_code='context_plan_capacity_current_user_in_candidate',
                )
            plan.manifest.update({
                'context_plan_consumer': 'canonical_capacity',
                'context_plan_id': str(context_plan.plan_id),
                'context_plan_hash': str(context_plan.plan_hash),
                'context_plan_valid': bool(context_plan.valid),
                'context_plan_budget_status': str(context_plan.budget_status),
                'context_plan_source_hash': str(context_plan.source_hash),
                'capacity_context_raw_source_refs': raw_refs,
                'capacity_context_chunk_source_refs': chunk_refs,
                'capacity_context_raw_carrier_parity': 'PASS',
                'capacity_context_raw_candidate_sha256': str(
                    handoff.jsonl_sha256
                    or getattr(handoff.candidate, 'output_sha256', '')
                    or ''
                ),
                'capacity_context_current_user_in_candidate': False,
                'capacity_context_install_parity': 'PENDING',
            })
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

    # Gate OFF only: preserve the existing Capacity shadow metadata path.
    if not gate_on:
        # Production commit succeeded. Shadow metadata is best-effort and fail-open.
        try:
            # Candidate metadata is private and transient. Attach it only after
            # staged install, registry publication, and target binding all succeed.
            candidate = handoff.candidate
            fixed_sections = tuple(
                section for section in _build_continuity_shadow_fixed_sections(
                    plan=plan,
                    resident=resident,
                    static_system=effective,
                )
                if str(getattr(section, 'kind', '') or '') in {
                    'invariant_system',
                    'accepted_state',
                }
            )
            anchor_status = getattr(
                getattr(candidate, 'anchor_status', None),
                'value',
                getattr(candidate, 'anchor_status', ''),
            )
            candidate_sid = str(
                handoff.candidate_session_id
                or getattr(candidate, 'candidate_session_id', '')
                or ''
            ).strip()
            pending_source_context_id = int(handoff.source_context_id or 0)
            pending_source_context_epoch = int(handoff.source_context_epoch or 0)
            pending_source_generation = int(handoff.source_resident_generation or 0)
            pending_target_generation = int(target_gen)
            baseline_sha = str(handoff.jsonl_sha256 or '').strip()
            if (
                not candidate_sid
                or pending_source_context_id != source_ctx
                or pending_source_context_epoch != source_epoch
                or pending_source_generation <= 0
                or pending_target_generation <= 0
                or pending_target_generation != pending_source_generation + 1
                or pending_target_generation != int(plan.resident_generation)
            ):
                raise DailyRuntimeError(
                    'capacity shadow pending identity is incomplete',
                    error_code='capacity_target_identity_mismatch',
                )
            if not baseline_sha:
                raise DailyRuntimeError(
                    'published capacity baseline sha256 is missing',
                    error_code='capacity_baseline_sha_missing',
                )
            selected_ids = tuple(
                int(value) for value in getattr(
                    candidate, 'selected_message_ids', ()
                ) if int(value) > 0
            )
            plan._continuity_shadow_capacity_pending = {
                'source_context_id': pending_source_context_id,
                'source_context_epoch': pending_source_context_epoch,
                'source_resident_generation': pending_source_generation,
                'target_resident_generation': pending_target_generation,
                'candidate_session_id': candidate_sid,
                'selected_message_ids': selected_ids,
                'anchor_status': str(anchor_status or ''),
                'anchor_message_id': int(
                    getattr(candidate, 'anchor_message_id', 0) or 0
                ),
                'capacity_baseline_sha256': baseline_sha,
                'trigger_reason': str(trigger_reason),
                'fixed_section_fingerprints': _shadow_fixed_section_fingerprints(
                    fixed_sections,
                ),
            }
        except Exception as exc:
            err = str(getattr(exc, 'error_code', None) or '')
            if not err:
                err = 'capacity_shadow_pending_build_failed'
            plan._continuity_shadow_capacity_pending_error = err
            logger.exception(
                'capacity shadow pending build failed code=%s',
                err,
                exc_info=True,
            )

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
    for attr in (
        '_continuity_shadow_capacity_pending',
        '_continuity_shadow_capacity_pending_error',
    ):
        if hasattr(plan, attr):
            delattr(plan, attr)
    plan.manifest['capacity_swap_pre_flush_rollback'] = True
    return True

def _raise_capacity_pre_send_failure(
    plan: DailyTurnPlan,
    *,
    resident: Any,
    exc: Exception,
) -> None:
    """Rollback a staged Capacity target before surfacing a pre-send failure."""
    if not _is_capacity_context_plan(plan):
        raise exc
    manifest = getattr(plan, 'manifest', None)
    if not isinstance(manifest, dict):
        manifest = {}
        plan.manifest = manifest
    if bool(getattr(plan, '_current_user_stdin_flushed', False)):
        raise exc
    if not getattr(plan, '_capacity_swap_install_state', None):
        raise exc
    if (
        manifest.get('capacity_swap_pre_flush_blocked')
        or manifest.get('capacity_swap_pre_flush_rollback_unproven')
    ):
        raise exc

    error_code = str(
        getattr(exc, 'error_code', None)
        or 'context_plan_capacity_pre_send_failed'
    )
    try:
        rolled = _rollback_capacity_swap_if_unflushed(
            plan,
            resident=resident,
        )
    except Exception as rollback_exc:
        manifest.update({
            'capacity_swap_pre_flush_rollback_unproven': True,
            'capacity_swap_pre_flush_error_code': error_code,
            'capacity_swap_pre_flush_rollback_error_code': (
                type(rollback_exc).__name__
            ),
        })
        raise DailyRuntimeError(
            'gate-on Capacity ContextPlan rollback could not be proven',
            error_code='context_plan_capacity_rollback_unproven',
            retryable=False,
        ) from rollback_exc
    if not rolled:
        manifest.update({
            'capacity_swap_pre_flush_rollback_unproven': True,
            'capacity_swap_pre_flush_error_code': error_code,
        })
        raise DailyRuntimeError(
            'gate-on Capacity ContextPlan rollback could not be proven',
            error_code='context_plan_capacity_rollback_unproven',
            retryable=False,
        ) from exc

    manifest.update({
        'capacity_swap_pre_flush_blocked': True,
        'capacity_swap_pre_flush_error_code': error_code,
    })
    raise DailyRuntimeError(
        'gate-on Capacity ContextPlan carrier failed before stdin',
        error_code=error_code,
        retryable=False,
    ) from exc


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



def _continuity_shadow_canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def _continuity_shadow_fingerprint(value: Any) -> tuple[str, int, str]:
    if isinstance(value, str):
        serialized = value
        kind = 'text'
    elif isinstance(value, (list, dict)):
        serialized = _continuity_shadow_canonical(value)
        kind = 'multimodal'
    else:
        serialized = _continuity_shadow_canonical(value)
        kind = type(value).__name__
    digest = hashlib.sha256(serialized.encode('utf-8')).hexdigest()
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1
    estimate = int(estimate_tokens_heuristic_cjk1_ascii4_v1(serialized))
    return digest, estimate, kind


def _bind_context_install_render_receipt(
    assembly: Any,
    content: Any,
) -> None:
    """Bind the renderer's actual carrier operations to final provider content."""
    if not isinstance(assembly, dict):
        return
    receipt = assembly.get('_context_install_render_receipt')
    if not isinstance(receipt, dict):
        return
    digest, estimate, kind = _continuity_shadow_fingerprint(content)
    receipt.update({
        'final_payload_hash': str(digest),
        'final_payload_token_estimate': int(estimate),
        'final_payload_kind': str(kind),
    })


def _accepted_state_text(assembly: Any) -> str:
    from chat.persona_state_semantic import (
        format_persona_semantic_snapshot,
        translate_raw_state_to_persona_semantic,
    )
    value = assembly if isinstance(assembly, dict) else {}
    state_snapshot = value.get('state_snapshot')
    if not isinstance(state_snapshot, dict) or not state_snapshot:
        return ''
    return format_persona_semantic_snapshot(
        translate_raw_state_to_persona_semantic(state_snapshot),
    )


def _build_continuity_shadow_fixed_sections(
    *,
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
    require_full_state_snapshot: bool = False,
) -> tuple[Any, ...]:
    from continuity.context_plan import ContextSection
    from chat.persona_state_semantic import (
        format_persona_semantic_snapshot,
        translate_raw_state_to_persona_semantic,
    )
    from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1

    resident_system = getattr(resident, '_system_text', '')
    if not isinstance(resident_system, str) or not resident_system.strip():
        resident_system = str(static_system or '')
    sections = []
    system_hash = _sha256_text(resident_system)
    sections.append(ContextSection(
        kind='invariant_system',
        source_ref='system:%s' % system_hash[:32],
        content_hash=system_hash,
        estimated_tokens=int(
            estimate_tokens_heuristic_cjk1_ascii4_v1(resident_system)
        ),
    ))

    assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
    state_serialized = _accepted_state_text(assembly)
    if not state_serialized and not require_full_state_snapshot:
        # Legacy shadow/test callers may only have the transport field.  The
        # production ContextPlan path opts out so a hot delta cannot become a
        # durable accepted-state identity.
        state = assembly.get('state')
        if state:
            state_serialized = (
                state if isinstance(state, str)
                else _continuity_shadow_canonical(state)
            )
    if state_serialized:
        state_hash = _sha256_text(state_serialized)
        sections.append(ContextSection(
            kind='accepted_state',
            source_ref='state:%s' % state_hash[:32],
            content_hash=state_hash,
            estimated_tokens=int(
                estimate_tokens_heuristic_cjk1_ascii4_v1(state_serialized)
            ),
        ))

    handoff = assembly.get('day_handoff_content')
    open_loops = handoff.get('open_loops') if isinstance(handoff, dict) else None
    if open_loops:
        loops_serialized = (
            open_loops
            if isinstance(open_loops, str)
            else _continuity_shadow_canonical(open_loops)
        )
        loops_hash = _sha256_text(loops_serialized)
        manifest = assembly.get('manifest')
        manifest = manifest if isinstance(manifest, dict) else {}
        identity = {
            key: value
            for key, value in (
                ('source_day', handoff.get('source_day')),
                ('source_sha256', handoff.get('source_sha256')),
                ('source_last_message_id', handoff.get('source_last_message_id')),
                ('source_epoch', handoff.get('source_epoch') or manifest.get('context_epoch')),
                ('boundary_message_id', handoff.get('boundary_message_id') or manifest.get('boundary_message_id')),
            )
            if value not in (None, '')
        }
        identity_hash = _sha256_text(_continuity_shadow_canonical(identity))
        sections.append(ContextSection(
            kind='accepted_open_loops',
            source_ref='handoff:%s' % identity_hash[:32],
            content_hash=loops_hash,
            estimated_tokens=int(
                estimate_tokens_heuristic_cjk1_ascii4_v1(loops_serialized)
            ),
        ))
    return tuple(sections)


def _continuity_shadow_turn_kind(plan: DailyTurnPlan) -> str:
    manifest = plan.manifest if isinstance(plan.manifest, dict) else {}
    value = str(manifest.get('turn_kind') or '').strip()
    if value:
        return value
    return 'respawn' if plan.is_respawn else 'cold' if plan.is_cold else 'hot'


def _shadow_fixed_section_fingerprints(sections: Any) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            str(getattr(section, 'kind', '') or ''),
            str(getattr(section, 'source_ref', '') or ''),
            str(getattr(section, 'content_hash', '') or ''),
            int(getattr(section, 'estimated_tokens', 0) or 0),
        )
        for section in (sections or ())
    )


def _shadow_assembly_message_ids(plan: DailyTurnPlan) -> tuple[int, ...]:
    assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
    ids: list[int] = []

    def add(items: Any) -> None:
        for item in items or ():
            if not isinstance(item, dict):
                continue
            value = item.get('message_id', item.get('id'))
            try:
                mid = int(value)
            except (TypeError, ValueError):
                continue
            if mid > 0 and mid not in ids:
                ids.append(mid)

    add(assembly.get('current_day_history'))
    add(assembly.get('carryover_messages'))
    return tuple(ids)


def _shadow_load_source_rows(
    message_ids: tuple[int, ...],
    *,
    db_path: Optional[str],
) -> list[dict[str, Any]]:
    """Read complete durable chat rows for R1 derivation; never writes."""
    ids = tuple(sorted({int(value) for value in message_ids if int(value) > 0}))
    if not ids:
        return []
    path = os.path.abspath(db_path or dc.DEFAULT_DB_PATH)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    conn = sqlite3.connect(
        Path(path).resolve().as_uri() + '?mode=ro',
        uri=True,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ','.join('?' for _ in ids)
        rows = conn.execute(
            'SELECT * FROM chat_messages WHERE id IN (%s) ORDER BY id ASC'
            % placeholders,
            ids,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _shadow_source_member_message_ids(
    turns: Any,
    events: Any,
) -> set[int]:
    represented: set[int] = set()

    def add_ref(value: Any) -> None:
        source_ref = str(getattr(value, 'source_ref', '') or '')
        parts = source_ref.split(':')
        if len(parts) >= 2 and parts[0] == 'message':
            try:
                mid = int(parts[1])
            except (TypeError, ValueError):
                return
            if mid > 0:
                represented.add(mid)

    for turn in turns:
        add_ref(turn.user_input_ref)
        for ref in turn.assistant_committed_output_refs:
            add_ref(ref)
        for ref in turn.tool_outcome_refs:
            add_ref(ref)
    for event in events:
        add_ref(event.committed_content_ref)
    return represented


def _shadow_derive_members_for_ids(
    message_ids: tuple[int, ...],
    *,
    db_path: Optional[str],
) -> tuple[Any, ...]:
    from continuity.sources import (
        build_source_members,
        derive_autonomous_events,
        derive_completed_turns,
    )

    rows = _shadow_load_source_rows(message_ids, db_path=db_path)
    loaded_ids = {int(row.get('id') or 0) for row in rows}
    missing = sorted(set(message_ids) - loaded_ids)
    if missing:
        raise ValueError('installed source rows missing: %s' % missing)
    turns = derive_completed_turns(rows)
    events = derive_autonomous_events(rows)
    members = build_source_members(turns, events)
    represented = _shadow_source_member_message_ids(turns, events)
    uncovered = sorted(set(message_ids) - represented)
    if uncovered:
        raise ValueError(
            'installed source projection unavailable: %s' % uncovered
        )
    return tuple(members)


def _shadow_receipt_pending_metadata(
    *,
    plan: DailyTurnPlan,
    turn_kind: str,
    production_content_hash: str,
    production_content_token_estimate: int,
    fixed_sections: Any,
    base_plan_id: str,
    base_plan_hash: str,
) -> dict[str, Any]:
    return {
        'turn_kind': str(turn_kind),
        'production_content_hash': str(production_content_hash or ''),
        'production_content_token_estimate': int(
            production_content_token_estimate or 0
        ),
        'fixed_section_fingerprints': _shadow_fixed_section_fingerprints(
            fixed_sections,
        ),
        'base_plan_id': str(base_plan_id or ''),
        'base_plan_hash': str(base_plan_hash or ''),
    }


def _log_continuity_shadow_receipt_event(
    plan: DailyTurnPlan,
    *,
    status: str,
    error_code: Optional[str] = None,
    receipt: Any = None,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> None:
    payload = {
        'event': 'continuity_shadow_receipt_commit',
        'status': str(status),
        'error_code': error_code,
        'context_id': int(plan.context_id),
        'context_epoch': int(plan.context_epoch),
        'resident_generation': int(plan.resident_generation),
        'membership_hash': (
            str(receipt.membership_hash)
            if receipt is not None else None
        ),
        'member_count': (
            len(receipt.installed_source_members)
            if receipt is not None else 0
        ),
    }
    # Capacity receipts expose only machine metadata; keep the whitelist local
    # so source/body/provider payloads can never reach the structured sink.
    if extra_metadata:
        for key in (
            'source_turn_kind',
            'capacity_anchor_present',
            'capacity_anchor_status',
            'capacity_baseline_sha256',
            'source_generation',
            'target_generation',
        ):
            if key in extra_metadata:
                payload[key] = extra_metadata[key]
    logger.info(
        'continuity_shadow_receipt_commit %s',
        _continuity_shadow_canonical(payload),
    )


def _commit_continuity_shadow_receipt(
    plan: DailyTurnPlan,
    *,
    assistant_message_id: int,
) -> bool:
    """Commit one process-local receipt after the existing full-success boundary."""
    def _clear_pending() -> None:
        for attr in (
            '_continuity_shadow_pending_receipt',
            '_continuity_shadow_capacity_pending',
            '_continuity_shadow_capacity_pending_error',
        ):
            if hasattr(plan, attr):
                delattr(plan, attr)

    if str(plan.manifest.get('transcript_mapping_status') or '') != 'MAPPED':
        _log_continuity_shadow_receipt_event(
            plan, status='skipped', error_code='mapping_not_mapped',
        )
        _clear_pending()
        return False
    sid = str(plan.transcript_claude_session_id or '').strip()
    process_generation = plan.transcript_process_generation
    if (
        not sid
        or process_generation is None
        or int(process_generation) <= 0
    ):
        _log_continuity_shadow_receipt_event(
            plan, status='skipped', error_code='runtime_identity_unavailable',
        )
        _clear_pending()
        return False

    from chat import daily_continuity_shadow_receipt as receipt_store

    turn_kind = _continuity_shadow_turn_kind(plan)
    pending = getattr(plan, '_continuity_shadow_pending_receipt', None)
    capacity_pending = getattr(plan, '_continuity_shadow_capacity_pending', None)
    try:
        if turn_kind == 'capacity_swap':
            if not isinstance(capacity_pending, dict):
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_receipt_pending_metadata_missing',
                )
                return False

            selected_ids: list[int] = []
            for value in capacity_pending.get('selected_message_ids', ()):
                try:
                    mid = int(value)
                except (TypeError, ValueError):
                    continue
                if mid > 0 and mid not in selected_ids:
                    selected_ids.append(mid)
            current_user_id = int(plan.user_message_id)
            candidate_sid = str(
                capacity_pending.get('candidate_session_id') or ''
            ).strip()
            pending_source_context_id = int(
                capacity_pending.get('source_context_id') or 0
            )
            pending_source_context_epoch = int(
                capacity_pending.get('source_context_epoch') or 0
            )
            source_generation = int(
                capacity_pending.get('source_resident_generation') or 0
            )
            pending_target_generation = int(
                capacity_pending.get('target_resident_generation') or 0
            )
            baseline_sha = str(
                capacity_pending.get('capacity_baseline_sha256') or ''
            ).strip()
            if (
                not candidate_sid
                or candidate_sid != sid
                or pending_source_context_id != int(plan.context_id)
                or pending_source_context_epoch != int(plan.context_epoch)
                or source_generation <= 0
                or pending_target_generation <= 0
                or pending_target_generation != int(plan.resident_generation)
                or pending_target_generation != source_generation + 1
            ):
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_target_identity_mismatch',
                )
                return False
            if not baseline_sha:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_baseline_sha_missing',
                )
                return False
            if current_user_id in selected_ids:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_current_user_in_candidate',
                )
                return False

            anchor_status = str(
                getattr(
                    capacity_pending.get('anchor_status'),
                    'value',
                    capacity_pending.get('anchor_status', ''),
                ) or ''
            )
            anchor_id = int(capacity_pending.get('anchor_message_id') or 0)
            retained_statuses = {'ANCHOR_RETAINED', 'ANCHOR_IMAGE_DEGRADED'}
            unavailable_statuses = {'ANCHOR_UNAVAILABLE', 'ANCHOR_TOO_LARGE'}
            anchor = None
            if anchor_status in retained_statuses:
                if anchor_id <= 0 or anchor_id not in selected_ids:
                    _log_continuity_shadow_receipt_event(
                        plan,
                        status='skipped',
                        error_code='capacity_anchor_projection_unavailable',
                    )
                    return False
                try:
                    from continuity.sources import (
                        evidence_ref,
                        is_formal_user_source_row,
                    )
                    anchor_rows = _shadow_load_source_rows(
                        (anchor_id,), db_path=plan.db_path,
                    )
                    if (
                        len(anchor_rows) != 1
                        or not is_formal_user_source_row(anchor_rows[0])
                    ):
                        raise ValueError('capacity anchor is not formal user')
                    anchor_ref = evidence_ref(anchor_rows[0])
                except Exception:
                    _log_continuity_shadow_receipt_event(
                        plan,
                        status='skipped',
                        error_code='capacity_anchor_projection_unavailable',
                    )
                    return False
                anchor = receipt_store.CapacityAnchorEvidence(
                    message_id=anchor_id,
                    source_ref=str(anchor_ref.source_ref),
                    source_revision=str(anchor_ref.source_revision),
                    source_content_hash=str(anchor_ref.content_hash),
                    anchor_status=anchor_status,
                    logical_size=int(anchor_ref.logical_size),
                )
                tail_ids = tuple(mid for mid in selected_ids if mid != anchor_id)
            elif anchor_status in unavailable_statuses:
                if anchor_id > 0 and anchor_id in selected_ids:
                    _log_continuity_shadow_receipt_event(
                        plan,
                        status='skipped',
                        error_code='capacity_anchor_projection_unavailable',
                    )
                    return False
                tail_ids = tuple(selected_ids)
            else:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_anchor_projection_unavailable',
                )
                return False

            try:
                tail_members = _shadow_derive_members_for_ids(
                    tail_ids,
                    db_path=plan.db_path,
                )
            except Exception:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_tail_projection_unavailable',
                )
                return False
            try:
                current_members = _shadow_derive_members_for_ids(
                    (current_user_id, int(assistant_message_id)),
                    db_path=plan.db_path,
                )
            except Exception:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_current_turn_projection_unavailable',
                )
                return False
            if len(current_members) != 1:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='capacity_current_turn_projection_unavailable',
                )
                return False

            target_generation = int(plan.resident_generation)
            receipt = receipt_store.InstalledContextShadowReceipt.build(
                context_id=int(plan.context_id),
                context_epoch=int(plan.context_epoch),
                resident_generation=target_generation,
                resident_key=str(plan.resident_key),
                claude_session_id=sid,
                process_generation=int(process_generation),
                base_plan_id='',
                base_plan_hash='',
                source_members=tuple(tail_members) + tuple(current_members),
                fixed_section_fingerprints=capacity_pending.get(
                    'fixed_section_fingerprints', ()
                ),
                production_content_hash=capacity_pending.get(
                    'production_content_hash', ''
                ),
                production_content_token_estimate=capacity_pending.get(
                    'production_content_token_estimate', 0
                ),
                source_turn_kind='capacity_swap',
                capacity_anchor=anchor,
                capacity_baseline_sha256=str(
                    capacity_pending.get('capacity_baseline_sha256', '') or ''
                ),
                capacity_source_generation=(
                    source_generation if source_generation > 0 else None
                ),
            )
            receipt_store.commit(receipt)
            if (
                source_generation > 0
                and (
                    source_generation != target_generation
                    or int(capacity_pending.get('source_context_id', plan.context_id))
                    != int(plan.context_id)
                    or int(capacity_pending.get('source_context_epoch', plan.context_epoch))
                    != int(plan.context_epoch)
                )
            ):
                receipt_store.drop(
                    int(capacity_pending.get('source_context_id', plan.context_id)),
                    int(capacity_pending.get('source_context_epoch', plan.context_epoch)),
                    source_generation,
                )
            _log_continuity_shadow_receipt_event(
                plan,
                status='committed',
                receipt=receipt,
                extra_metadata={
                    'source_turn_kind': 'capacity_swap',
                    'capacity_anchor_present': bool(anchor),
                    'capacity_anchor_status': anchor_status,
                    'capacity_baseline_sha256': receipt.capacity_baseline_sha256,
                    'source_generation': source_generation,
                    'target_generation': target_generation,
                },
            )
            return True

        if turn_kind == 'hot':
            prior = receipt_store.get(
                plan.context_id,
                plan.context_epoch,
                plan.resident_generation,
            )
            if prior is None:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='installed_context_receipt_missing',
                )
                return False
            if not prior.matches_live(
                context_id=plan.context_id,
                context_epoch=plan.context_epoch,
                resident_generation=plan.resident_generation,
                resident_key=plan.resident_key,
                claude_session_id=sid,
                process_generation=int(process_generation),
            ):
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='installed_context_receipt_identity_mismatch',
                )
                return False
            new_members = _shadow_derive_members_for_ids(
                (int(plan.user_message_id), int(assistant_message_id)),
                db_path=plan.db_path,
            )
            if len(new_members) != 1:
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='installed_source_projection_unavailable',
                )
                return False
            receipt = receipt_store.advance(
                prior,
                new_members=new_members,
                production_content_hash=(
                    pending.get('production_content_hash', prior.production_content_hash)
                    if isinstance(pending, dict) else prior.production_content_hash
                ),
                production_content_token_estimate=(
                    pending.get(
                        'production_content_token_estimate',
                        prior.production_content_token_estimate,
                    )
                    if isinstance(pending, dict)
                    else prior.production_content_token_estimate
                ),
                source_turn_kind='hot',
            )
        else:
            if not isinstance(pending, dict):
                _log_continuity_shadow_receipt_event(
                    plan,
                    status='skipped',
                    error_code='shadow_receipt_plan_metadata_missing',
                )
                return False
            ids = list(_shadow_assembly_message_ids(plan))
            ids.extend((int(plan.user_message_id), int(assistant_message_id)))
            unique_ids: list[int] = []
            for value in ids:
                if value > 0 and value not in unique_ids:
                    unique_ids.append(value)
            members = _shadow_derive_members_for_ids(
                tuple(unique_ids),
                db_path=plan.db_path,
            )
            receipt = receipt_store.InstalledContextShadowReceipt.build(
                context_id=plan.context_id,
                context_epoch=plan.context_epoch,
                resident_generation=plan.resident_generation,
                resident_key=plan.resident_key,
                claude_session_id=sid,
                process_generation=int(process_generation),
                base_plan_id=pending.get('base_plan_id', ''),
                base_plan_hash=pending.get('base_plan_hash', ''),
                source_members=members,
                fixed_section_fingerprints=pending.get(
                    'fixed_section_fingerprints', ()
                ),
                production_content_hash=pending.get(
                    'production_content_hash', ''
                ),
                production_content_token_estimate=pending.get(
                    'production_content_token_estimate', 0
                ),
                source_turn_kind=turn_kind,
            )
        receipt_store.commit(receipt)
        _log_continuity_shadow_receipt_event(
            plan, status='committed', receipt=receipt,
        )
        return True
    except Exception:
        _log_continuity_shadow_receipt_event(
            plan,
            status='skipped',
            error_code='installed_source_projection_unavailable',
        )
        return False
    finally:
        _clear_pending()

def _observe_continuity_shadow(
    *,
    plan: DailyTurnPlan,
    resident: Any,
    static_system: str,
    content: Any,
) -> None:
    # Normal hot keeps its incremental/no-replay contract.  Only a plan that
    # is actually installed on this turn may enter production install proof.
    if (
        getattr(plan, 'continuity_plan', None) is not None
        or getattr(plan, 'capacity_context_plan', None) is not None
    ):
        _observe_production_context_plan(
            plan=plan,
            resident=resident,
            static_system=static_system,
            content=content,
        )
        return
    if getattr(plan, 'hot_desired_plan', None) is not None:
        # Gate-on normal hot already built and reconciled one canonical
        # desired plan before this point.  The legacy shadow branch must not
        # build a second plan or become an installed-proof authority.
        plan.manifest['context_plan_hot_shadow'] = 'disabled'
        return
    turn_kind = _continuity_shadow_turn_kind(plan)
    if hasattr(plan, '_continuity_shadow_pending_receipt'):
        delattr(plan, '_continuity_shadow_pending_receipt')
    observation = {
        'event': 'continuity_shadow_observation',
        'status': 'failed',
        'error_code': 'unexpected_exception',
        'request_id': str(plan.request_id),
        'chat_id': str(plan.chat_id),
        'context_id': int(plan.context_id),
        'context_epoch': int(plan.context_epoch),
        'resident_generation': int(plan.resident_generation),
        'turn_kind': turn_kind,
        'production_content_hash': None,
        'production_content_token_estimate': None,
        'production_content_kind': None,
        'source_member_count': 0,
        'chunk_binding_count': 0,
        'chunk_surface': 'unavailable',
        'plan_id': None,
        'plan_hash': None,
        'plan_valid': None,
        'budget_status': None,
        'token_budget': None,
        'reserve_budget': None,
        'recent_raw_target': None,
        'selected_token_estimate': None,
        'fixed_section_token_estimate': None,
        'total_token_estimate': None,
        'remaining_budget': None,
        'raw_representation_count': 0,
        'chunk_representation_count': 0,
        'gap_codes': [],
        'exclusion_codes': [],
        'runtime_transition_source': turn_kind,
        'shadow_plan_available': False,
        'installed_context_proven': False,
        'source_proof_status': None,
        'source_proof_error_code': None,
        'budget_error_code': None,
    }
    try:
        content_hash, content_tokens, content_kind = _continuity_shadow_fingerprint(content)
        observation.update({
            'production_content_hash': content_hash,
            'production_content_token_estimate': content_tokens,
            'production_content_kind': content_kind,
        })
        if turn_kind == 'hot':
            observation.update({
                'status': 'blocked',
                'error_code': 'hot_budget_policy_unmapped',
                'budget_status': 'blocked',
                'budget_error_code': 'hot_budget_policy_unmapped',
            })
            from chat import daily_continuity_shadow_receipt as receipt_store
            live_sid = str(getattr(resident, 'session_id', None) or '').strip()
            live_generation = int(
                getattr(resident, 'generation', 0) or 0
            )
            receipt = receipt_store.get(
                plan.context_id,
                plan.context_epoch,
                plan.resident_generation,
            )
            if receipt is None:
                observation['source_proof_status'] = 'blocked'
                observation['source_proof_error_code'] = (
                    'installed_context_receipt_missing'
                )
            elif not receipt.matches_live(
                context_id=plan.context_id,
                context_epoch=plan.context_epoch,
                resident_generation=plan.resident_generation,
                resident_key=plan.resident_key,
                claude_session_id=live_sid,
                process_generation=live_generation,
            ):
                observation['source_proof_status'] = 'blocked'
                observation['source_proof_error_code'] = (
                    'installed_context_receipt_identity_mismatch'
                )
            else:
                observation['source_proof_status'] = 'ready'
                observation['installed_context_proven'] = True
                plan._continuity_shadow_pending_receipt = (
                    _shadow_receipt_pending_metadata(
                        plan=plan,
                        turn_kind='hot',
                        production_content_hash=content_hash,
                        production_content_token_estimate=content_tokens,
                        fixed_sections=(),
                        base_plan_id=receipt.base_plan_id,
                        base_plan_hash=receipt.base_plan_hash,
                    )
                )
            logger.info(
                'continuity_shadow_observation %s',
                _continuity_shadow_canonical(observation),
            )
            return

        if turn_kind == 'capacity_swap':
            capacity_pending = getattr(
                plan, '_continuity_shadow_capacity_pending', None,
            )
            observation.update({
                'status': 'blocked',
                'error_code': 'installed_context_policy_unmapped',
                'source_proof_status': 'blocked',
                'source_proof_error_code': (
                    'capacity_receipt_pending_commit'
                    if isinstance(capacity_pending, dict)
                    else str(
                        getattr(
                            plan,
                            '_continuity_shadow_capacity_pending_error',
                            '',
                        ) or 'capacity_receipt_deferred'
                    )
                ),
            })
            if isinstance(capacity_pending, dict):
                # Keep the full-success receipt boundary supplied with the
                # current production prompt measurement without storing body.
                updated_pending = dict(capacity_pending)
                updated_pending.update({
                    'production_content_hash': content_hash,
                    'production_content_token_estimate': content_tokens,
                })
                plan._continuity_shadow_capacity_pending = updated_pending
        else:
            store_path = str(os.environ.get(
                'HAYA_CONTINUITY_SHADOW_STORE_PATH', ''
            ) or '').strip()
            if not store_path:
                observation.update({
                    'status': 'blocked',
                    'error_code': 'shadow_store_unconfigured',
                    'source_proof_status': 'blocked',
                })
            else:
                assembly = plan.assembly if isinstance(plan.assembly, dict) else {}
                manifest = assembly.get('manifest')
                manifest = manifest if isinstance(manifest, dict) else {}
                try:
                    recent_raw_target = int(manifest.get('cold_history_budget'))
                except (TypeError, ValueError):
                    recent_raw_target = 0
                if recent_raw_target <= 0:
                    observation.update({
                        'status': 'blocked',
                        'error_code': 'budget_policy_unmapped',
                        'source_proof_status': 'blocked',
                    })
                else:
                    from chat.cold_bootstrap_budget import (
                        resident_rebuild_prompt_target,
                        cold_safety_margin,
                    )
                    from chat.daily_continuity_shadow import (
                        build_daily_continuity_shadow_plan,
                    )
                    from continuity.context_plan import ContextBudgetPolicy

                    target = int(resident_rebuild_prompt_target())
                    reserve = int(cold_safety_margin())
                    if target <= 0 or reserve < 0:
                        raise ValueError('cold budget policy is invalid')
                    policy = ContextBudgetPolicy(
                        token_budget=target + reserve,
                        reserve_budget=reserve,
                        recent_raw_target=recent_raw_target,
                    )
                    fixed_sections = _build_continuity_shadow_fixed_sections(
                        plan=plan,
                        resident=resident,
                        static_system=static_system,
                    )
                    result = build_daily_continuity_shadow_plan(
                        source_db_path=plan.db_path or dc.DEFAULT_DB_PATH,
                        shadow_store_path=store_path,
                        current_user_message_id=int(plan.user_message_id),
                        budget_policy=policy,
                        accepted_fixed_sections=fixed_sections,
                    )
                    observation.update({
                        'status': str(result.status),
                        'error_code': result.error_code,
                        'chunk_surface': str(result.chunk_surface),
                        'source_member_count': int(result.source_member_count),
                        'chunk_binding_count': int(result.chunk_binding_count),
                        'shadow_plan_available': bool(result.plan is not None),
                        'installed_context_proven': False,
                        'source_proof_status': 'blocked',
                    })
                    shadow_plan = result.plan
                    if shadow_plan is not None:
                        representations = tuple(shadow_plan.representations)
                        budget_policy = shadow_plan.budget_policy
                        observation.update({
                            'plan_id': shadow_plan.plan_id,
                            'plan_hash': shadow_plan.plan_hash,
                            'plan_valid': bool(shadow_plan.valid),
                            'budget_status': shadow_plan.budget_status,
                            'token_budget': shadow_plan.token_budget,
                            'reserve_budget': shadow_plan.reserve_budget,
                            'recent_raw_target': (
                                budget_policy.recent_raw_target
                                if budget_policy is not None else None
                            ),
                            'selected_token_estimate': shadow_plan.selected_token_estimate,
                            'fixed_section_token_estimate': shadow_plan.fixed_section_token_estimate,
                            'total_token_estimate': shadow_plan.total_token_estimate,
                            'remaining_budget': shadow_plan.remaining_budget,
                            'raw_representation_count': sum(
                                1 for item in representations if item.kind == 'raw'
                            ),
                            'chunk_representation_count': sum(
                                1 for item in representations if item.kind == 'chunk'
                            ),
                            'gap_codes': sorted({
                                str(item.code) for item in shadow_plan.gaps
                            }),
                            'exclusion_codes': sorted({
                                str(item.code) for item in shadow_plan.exclusions
                            }),
                        })
                        plan._continuity_shadow_pending_receipt = (
                            _shadow_receipt_pending_metadata(
                                plan=plan,
                                turn_kind=turn_kind,
                                production_content_hash=content_hash,
                                production_content_token_estimate=content_tokens,
                                fixed_sections=fixed_sections,
                                base_plan_id=shadow_plan.plan_id,
                                base_plan_hash=shadow_plan.plan_hash,
                            )
                        )
    except Exception:
        logger.exception('continuity shadow observation failed', exc_info=True)
        observation.update({
            'status': 'failed',
            'error_code': 'unexpected_exception',
            'source_proof_status': 'failed',
        })
    logger.info(
        'continuity_shadow_observation %s',
        _continuity_shadow_canonical(observation),
    )


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
                if _context_plan_consumer_enabled():
                    heartbeat.stop()
                    _release_lease(plan)
                    raise DailyRuntimeError(
                        'gate-on Capacity ContextPlan carrier blocked: %s'
                        % str(swap.get('error_code') or 'capacity_swap_failed'),
                        error_code=str(
                            swap.get('error_code')
                            or 'context_plan_capacity_blocked'
                        ),
                        retryable=False,
                    )
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

        if bool(plan.manifest.get('context_plan_hot_pending')):
            # Capacity has already had first refusal above.  Only now may a
            # normal-hot store/budget read decide whether this turn is NO_OP,
            # RESPAWN, or BLOCKED.
            try:
                hot_decision = _prepare_hot_context_plan(
                    plan,
                    resident=resident,
                    static_system=effective_system,
                )
            except Exception:
                heartbeat.stop()
                _release_lease(plan)
                raise
            if hot_decision == 'BLOCKED':
                heartbeat.stop()
                _release_lease(plan)
                raise DailyRuntimeError(
                    'normal hot ContextPlan reconciliation blocked: %s'
                    % str(plan.hot_decision_reason or 'unknown'),
                    error_code='context_plan_hot_blocked',
                    retryable=False,
                )
            if hot_decision == 'RESPAWN':
                heartbeat.stop()
                if _reprep_depth >= 1:
                    _release_lease(plan)
                    raise DailyRuntimeError(
                        'normal hot ContextPlan respawn loop',
                        error_code='context_plan_hot_respawn_loop',
                        retryable=False,
                    )
                replacement = reprepare_after_registered_session_change(
                    plan,
                    resident=resident,
                    static_system=effective_system,
                    static_system_sha256=(
                        plan.manifest.get('static_system_sha256')
                        or _sha256_text(effective_system)
                    ),
                    persona_sha256=plan.manifest.get('persona_sha256') or '',
                    provider=str(plan.manifest.get('provider') or 'claude_code'),
                    model=str(plan.manifest.get('model') or ''),
                )
                _adopt_reprepared_plan_in_place(plan, replacement, resident=resident)
                yield from ensure_resident_and_stream(
                    plan,
                    resident=resident,
                    env=env,
                    static_system=effective_system,
                    _reprep_depth=_reprep_depth + 1,
                    _registry_reprep_depth=_registry_reprep_depth,
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

        _bind_context_install_render_receipt(plan.assembly, content)

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
            # Synthetic resident heartbeat keeps the browser transport alive
            # during long Claude turns; it is not model activity and is only
            # enabled when the resident explicitly supports the kwarg.
            if 'idle_heartbeat_sec' in params or accepts_kwargs:
                send_kwargs['idle_heartbeat_sec'] = 10.0
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

        _validate_hot_no_op_payload(plan, content)
        _observe_continuity_shadow(
            plan=plan,
            resident=resident,
            static_system=effective_system,
            content=content,
        )

        try:
            for evt, payload in resident.send_turn(content, **send_kwargs):
                if heartbeat.failed:
                    close_local_resident_if_bound(resident, expected_key=plan.resident_key)
                    raise LeaseHeartbeatTerminalFailure('lease heartbeat failed during stream')
                if evt == 'done':
                    receipt = (
                        getattr(payload[2], 'terminal_receipt', None)
                        if (
                            isinstance(payload, tuple)
                            and len(payload) >= 3
                            and isinstance(payload[2], dict)
                        )
                        else None
                    )
                    if (
                        receipt is not None
                        and not isinstance(receipt, cc_resident.ProviderTerminalReceipt)
                    ):
                        raise DailyRuntimeError(
                            'resident terminal receipt has invalid type',
                            error_code='provider_terminal_receipt_invalid',
                        )
                    plan.terminal_receipt = receipt
                    _capture_transcript_end(plan, resident)
                    # Safety net: stream completed without on_stdin_flushed hook.
                    if getattr(plan, '_capacity_swap_install_state', None) is not None:
                        _commit_capacity_swap_after_stdin_flush(plan)
                yield evt, payload
        except Exception as exc:
            # Post-flush: CURRENT user already sent → fail closed, never resend.
            if bool(getattr(plan, '_current_user_stdin_flushed', False)):
                raise
            # Pre-flush CapSwap failure: restore old live. Gate-on split
            # carrier stops here; gate-off retains the frozen fallback order.
            if _is_capacity_context_plan(plan):
                _raise_capacity_pre_send_failure(
                    plan,
                    resident=resident,
                    exc=exc,
                )
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
    except Exception as exc:
        if _is_capacity_context_plan(plan):
            _raise_capacity_pre_send_failure(
                plan,
                resident=resident,
                exc=exc,
            )
        raise
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
        cursor_result = dc.finalize_daily_assistant_and_advance_cursor(
            plan.context_id,
            plan.resident_generation,
            aid,
            expected_cursor=plan.cursor_before,
            expected_context_epoch=plan.context_epoch,
            terminal_receipt_id=getattr(plan, '_terminal_mapping_receipt_id', None),
            db_path=plan.db_path,
        )
        cursor_after_raw = cursor_result.get('history_cursor_message_id')
        cursor_result_valid = True
        try:
            cursor_after = int(cursor_after_raw)
            cursor_result_valid = (
                not isinstance(cursor_after_raw, bool)
                and cursor_after >= aid
                and int(cursor_result.get('context_id')) == int(plan.context_id)
                and int(cursor_result.get('resident_generation'))
                    == int(plan.resident_generation)
                and isinstance(cursor_result.get('advanced'), bool)
            )
        except (AttributeError, TypeError, ValueError):
            cursor_after = 0
            cursor_result_valid = False
        cas_success = bool(
            cursor_result_valid and cursor_result.get('advanced') is True
        )
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
        'cursor_after': int(cursor_after) if cursor_result_valid else None,
        'cursor_cas_success': cas_success,
        'cursor_result_valid': cursor_result_valid,
        'unexpected_save_marker': bool(unexpected_save_marker),
        'stop_reason': stop_reason,
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'error_code': None,
    })
    binding = get_local_binding()
    if cursor_result_valid and binding and binding.resident_key == plan.resident_key:
        binding.bound_cursor_message_id = int(
            plan.manifest['cursor_after']
        )
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
    for attr in (
        '_continuity_shadow_capacity_pending',
        '_continuity_shadow_capacity_pending_error',
    ):
        if hasattr(plan, attr):
            delattr(plan, attr)
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
        source_kind=dc.SOURCE_KIND_CHAT,
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
    display_segments: str = '',
    source_kind: str = dc.SOURCE_KIND_DAILY_PENDING,
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
        display_segments=display_segments,
        source_kind=source_kind,
        db_path=plan.db_path,
    )


def build_canonical_turn_for_plan(
    plan: DailyTurnPlan,
    *,
    mode: str = 'auto',
    stop_reason: str = 'end_turn',
) -> Any:
    """Build the durable projection before the assistant row is inserted.

    The provider stream has already reached ``result`` when this is called,
    but its text is only a preview. A missing or unstable transcript is a hard
    terminal failure; callers must never persist that preview as success.
    """
    from chat.canonical_turn import CanonicalTurnError, build_canonical_turn

    if plan.transcript_observation_error_code:
        raise CanonicalTurnError(
            str(plan.transcript_observation_error_code),
            error_code=str(plan.transcript_observation_error_code),
        )
    required = (
        plan.transcript_path,
        plan.transcript_claude_session_id,
        plan.transcript_start_offset,
        plan.transcript_end_offset,
        plan.transcript_process_generation,
    )
    if any(value is None or value == '' for value in required):
        raise CanonicalTurnError(
            'transcript finality identity is incomplete',
            error_code='transcript_finality_incomplete',
        )
    turn = build_canonical_turn(
        str(plan.transcript_path),
        start_offset=int(plan.transcript_start_offset),
        end_offset=int(plan.transcript_end_offset),
        session_id=str(plan.transcript_claude_session_id),
        mode=mode,
        stop_reason=stop_reason,
        mapping_status=str(plan.manifest.get('transcript_mapping_status') or 'UNVERIFIED'),
        message_id=None,
        context_id=plan.context_id,
        context_epoch=plan.context_epoch,
        resident_generation=plan.resident_generation,
        transcript_process_generation=plan.transcript_process_generation,
        terminal_receipt=plan.terminal_receipt,
    )
    plan._canonical_turn = turn
    plan.manifest.update({
        'canonical_projection_hash': turn.projection_hash,
        'canonical_content_length': len(turn.content),
        'canonical_display_segments': turn.display_segments,
        'canonical_provider_round_count': len(turn.provider_rounds),
        'transcript_finality_status': turn.terminal_state,
        'transcript_finality_identity': dict(turn.transcript_identity),
    })
    return turn


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
    canonical = getattr(plan, '_canonical_turn', None)
    if canonical is None:
        abort_daily_turn(plan, error_code='canonical_turn_missing')
        raise DailyRuntimeError(
            'canonical transcript projection missing',
            error_code='canonical_turn_missing',
        )
    if not hasattr(plan, '_transcript_registry_before_mapping'):
        try:
            _capture_transcript_registry_snapshot(plan)
        except Exception as exc:
            abort_daily_turn(plan, error_code='registry_snapshot_failed')
            raise DailyRuntimeError(
                'transcript registry snapshot failed',
                error_code='registry_snapshot_failed',
            ) from exc
    # The row is already the canonical projection at this point, but it is not
    # a successful turn until the same transcript range maps successfully.
    finalize_transcript_mapping_after_success(
        plan, assistant_message_id=int(assistant_message_id),
    )
    if str(plan.manifest.get('transcript_mapping_status') or '') != 'MAPPED':
        mapping_failure = _freeze_transcript_mapping_failure(plan)
        _log_transcript_mapping_diagnostic(
            'TRANSCRIPT_MAPPING_TERMINAL_BLOCKED',
            plan,
            error_code=mapping_failure['mapping_error_code'],
            mapping_status=mapping_failure['mapping_status'],
            mapping_event_count=mapping_failure['mapping_event_count'],
            mapping_scan_offset=mapping_failure['mapping_scan_offset'],
        )
        try:
            _rollback_failed_terminalization(
                plan,
                assistant_message_id=int(assistant_message_id),
            )
        except Exception as exc:
            _raise_terminal_rollback_unproven(
                plan,
                assistant_message_id=int(assistant_message_id),
                cause=exc,
            )
        abort_daily_turn(
            plan,
            error_code=mapping_failure['mapping_error_code'],
        )
        raise DailyRuntimeError(
            'transcript mapping did not reach FINAL [%s]'
            % mapping_failure['mapping_error_code'],
            error_code=mapping_failure['mapping_error_code'],
        )
    try:
        complete_daily_turn(
            plan,
            assistant_message_id=int(assistant_message_id),
            stop_reason=str(getattr(canonical, 'stop_reason', None) or usage.get('stop_reason') or 'end_turn'),
            input_tokens=usage.get('input_tokens'),
            output_tokens=usage.get('output_tokens'),
            unexpected_save_marker=unexpected_save_marker,
        )
    except CursorCASConflictAfterPersist as exc:
        try:
            _rollback_failed_terminalization(
                plan,
                assistant_message_id=int(assistant_message_id),
            )
        except Exception as rollback_exc:
            _raise_terminal_rollback_unproven(
                plan,
                assistant_message_id=int(assistant_message_id),
                cause=rollback_exc,
            )
        raise exc
    # Same-context last-good: only after full success (result + persist + JSONL + cursor).
    end_off = plan.transcript_end_offset
    start_off = plan.transcript_start_offset
    sid = str(plan.transcript_claude_session_id or '').strip()
    plan._same_context_last_good_proven = False
    plan.manifest['context_receipt_last_good_proven'] = False
    mapping_proven = (
        str(plan.manifest.get('transcript_mapping_status') or '') == 'MAPPED'
    )
    jsonl_grew = (
        start_off is not None
        and end_off is not None
        and int(end_off) > int(start_off)
        and bool(sid)
    )
    if jsonl_grew and mapping_proven:
        try:
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
        except Exception:
            logger.warning('same-context last-good note failed', exc_info=True)
        plan._same_context_last_good_proven = _same_context_last_good_matches(plan)
        plan.manifest['context_receipt_last_good_proven'] = bool(
            plan._same_context_last_good_proven
        )
    elif jsonl_grew:
        logger.warning(
            'same-context last-good withheld because transcript mapping is %s',
            plan.manifest.get('transcript_mapping_status'),
        )
    # The receipt is committed only after persist + cursor CAS + MAPPED and
    # the existing last-good success boundary.  It is durable metadata proof.
    target_receipt_committed = True
    if _context_plan_for_receipt(plan) is not None:
        target_receipt_committed = bool(_commit_production_context_receipt(
            plan,
            assistant_message_id=int(assistant_message_id),
        ))
    else:
        _commit_continuity_shadow_receipt(
            plan,
            assistant_message_id=int(assistant_message_id),
        )
    if _is_capacity_context_plan(plan):
        _commit_capacity_source_receipt_supersession(
            plan,
            target_receipt_committed=target_receipt_committed,
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
