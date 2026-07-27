"""Daily Soft Window provider history assembly.

Assembles provider-facing context for a daily epoch without legacy cold_once,
DIARY/DAILY/WEEKLY, auto recall, or Relationship Context.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from chat.daily_context import (
    DEFAULT_CHAT_ID,
    HANDOFF_READY,
    HotTurnCursorError,
    _USER_AUTHORS,
    _message_display_content,
    _table_columns,
    chat_day_window,
    ensure_carryover_zero_if_user_messages_exist,
    format_formal_handoff_prompt,
    get_daily_context_by_id,
    get_resident_history_cursor,
    is_formal_chat_message,
    resolve_bound_handoff,
)
from chat.daily_schema import META_SOURCE_KIND_CUTOVER, get_meta_int
from chat.day_handoff import TZ_OFFSET_HOURS


def _connect(db_path: Optional[str]):
    from chat.daily_context import _connect as dc_connect
    return dc_connect(db_path)


def _wake_content_set(conn) -> frozenset[str]:
    from chat.daily_context import _wake_content_set as _wcs
    return _wcs(conn)


def _fetch_current_day_history(
    *,
    boundary_message_id: int,
    local_day: str,
    db_path: Optional[str] = None,
    after_message_id: Optional[int] = None,
    exclude_message_id: Optional[int] = None,
    up_to_message_id: Optional[int] = None,
    context_id: Optional[int] = None,
    context_epoch: Optional[int] = None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        wake_contents = _wake_content_set(conn)
        cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)
        if context_id is not None and context_epoch is not None:
            mapped_count = conn.execute(
                'SELECT COUNT(*) AS c FROM daily_message_contexts '
                'WHERE context_id=? AND context_epoch=?',
                (int(context_id), int(context_epoch)),
            ).fetchone()
            has_mappings = int(mapped_count['c'] if mapped_count else 0) > 0
            if has_mappings:
                rows = conn.execute(
                    'SELECT %s FROM chat_messages m '
                    'INNER JOIN daily_message_contexts dmc ON dmc.message_id = m.id '
                    'WHERE dmc.context_id=? AND dmc.context_epoch=? AND m.id > ? '
                    'ORDER BY m.id ASC' % ', '.join('m.' + c for c in select_cols),
                    (int(context_id), int(context_epoch), int(boundary_message_id or 0)),
                ).fetchall()
            else:
                has_mappings = False
        else:
            has_mappings = False
            rows = []
        if not has_mappings:
            _d, start_at, _end, next_start = chat_day_window(local_day)
            rows = conn.execute(
                'SELECT %s FROM chat_messages '
                'WHERE created_at >= ? AND created_at < ? AND id > ? '
                'ORDER BY id ASC' % ', '.join(select_cols),
                (start_at, next_start, int(boundary_message_id or 0)),
            ).fetchall()
        out = []
        for r in rows:
            mid = int(r['id'])
            if exclude_message_id is not None and mid == int(exclude_message_id):
                continue
            if after_message_id is not None and mid <= int(after_message_id):
                continue
            if up_to_message_id is not None and mid > int(up_to_message_id):
                break
            if not is_formal_chat_message(
                r, wake_contents=wake_contents, cutover_id=cutover,
            ):
                continue
            role = 'user' if str(r['author']).lower() in _USER_AUTHORS else 'assistant'
            out.append({
                'message_id': mid,
                'role': role,
                'author': str(r['author']),
                'content': _message_display_content(r),
                'created_at': str(r['created_at'] or ''),
            })
        return out
    finally:
        conn.close()


def _build_state_text(
    *,
    is_cold: bool,
    last_snapshot: Optional[dict[str, str]] = None,
) -> tuple[str, str, dict[str, str]]:
    from chat.system_builder import build_cc_state, format_state_diff, format_state_snapshot

    raw = build_cc_state(lean=True)
    if not isinstance(raw, dict):
        raw = {}
    snapshot = {str(k): str(v) for k, v in raw.items()}
    if is_cold or not last_snapshot:
        text = format_state_snapshot(snapshot) if snapshot else ''
        mode = 'snapshot' if text else 'none'
        return text, mode, snapshot
    delta = format_state_diff(last_snapshot, snapshot)
    if delta:
        return delta, 'delta', snapshot
    return '', 'none', snapshot


def build_daily_window_context(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    daily_context: dict[str, Any],
    current_user_message_id: Optional[int] = None,
    static_system: str = '',
    is_cold: bool = True,
    is_respawn: bool = False,
    last_state_snapshot: Optional[dict[str, str]] = None,
    inject_handoff: bool = True,
    inject_carryover: bool = True,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble provider context layers for one daily epoch turn.

    Order: static → handoff → carryover → state → current-day history.

    Resident history:
    - cold_like (cold or respawn): replay full epoch history (excluding current user)
    - hot: only messages after resident cursor; fail closed if cursor missing

    Does not persist resident cursor — caller must invoke
    advance_resident_history_cursor() after assistant message lands.
    """
    _ = TZ_OFFSET_HOURS
    ctx = dict(daily_context)
    context_id = int(ctx['id'])
    resident_generation = int(ctx.get('resident_generation') or 1)
    cold_like = bool(is_cold or is_respawn)

    if not ctx.get('selection_finalized_at'):
        ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)
        refreshed = get_daily_context_by_id(context_id, db_path=db_path)
        if refreshed:
            ctx = refreshed

    handoff_content, handoff_status = resolve_bound_handoff(ctx, db_path=db_path)
    handoff_prompt = (
        format_formal_handoff_prompt(handoff_content)
        if handoff_status == HANDOFF_READY and handoff_content else ''
    )
    handoff_injected = bool(inject_handoff and cold_like and handoff_prompt)

    from chat.daily_context import get_selected_carryover_messages
    carryover_messages = get_selected_carryover_messages(context_id, db_path=db_path)
    carryover_ids = [int(m['message_id']) for m in carryover_messages]
    carryover_injected = bool(inject_carryover and cold_like and carryover_messages)

    state_text, state_mode, state_snapshot = _build_state_text(
        is_cold=cold_like, last_snapshot=last_state_snapshot,
    )
    state_injected = bool(state_text)

    cursor_before = get_resident_history_cursor(
        context_id, resident_generation, db_path=db_path,
    )
    after_cursor: Optional[int] = None
    if not cold_like:
        after_cursor = cursor_before
        if after_cursor is None:
            raise HotTurnCursorError('hot turn requires resident history cursor')

    current_day_history = _fetch_current_day_history(
        boundary_message_id=int(ctx.get('boundary_message_id') or 0),
        local_day=str(ctx['local_day']),
        db_path=db_path,
        after_message_id=after_cursor,
        exclude_message_id=current_user_message_id,
        up_to_message_id=current_user_message_id,
        context_id=context_id,
        context_epoch=int(ctx.get('context_epoch') or 0),
    )

    if current_day_history:
        replayed_through_message_id = int(current_day_history[-1]['message_id'])
    elif cursor_before is not None:
        replayed_through_message_id = int(cursor_before)
    else:
        replayed_through_message_id = int(ctx.get('boundary_message_id') or 0)

    cursor_advance_required = bool(
        current_day_history or current_user_message_id is not None,
    )

    layers: list[dict[str, Any]] = []
    if static_system:
        layers.append({'kind': 'static', 'text': static_system})
    if handoff_injected:
        layers.append({'kind': 'day_handoff', 'text': handoff_prompt})
    if carryover_injected:
        layers.append({
            'kind': 'carryover',
            'messages': [
                {'role': m['role'], 'content': m['content'], 'message_id': m['message_id']}
                for m in carryover_messages
            ],
        })
    if state_injected:
        layers.append({'kind': 'state', 'text': state_text, 'mode': state_mode})
    if current_day_history:
        layers.append({
            'kind': 'current_day_history',
            'messages': [
                {'role': m['role'], 'content': m['content'], 'message_id': m['message_id']}
                for m in current_day_history
            ],
        })

    manifest = {
        'chat_id': ctx.get('chat_id') or chat_id,
        'local_day': ctx.get('local_day'),
        'context_epoch': int(ctx.get('context_epoch') or 0),
        'boundary_message_id': int(ctx.get('boundary_message_id') or 0),
        'daily_context_status': ctx.get('status'),
        'resident_generation': resident_generation,
        'cursor_before': cursor_before,
        'replayed_through_message_id': replayed_through_message_id,
        'cursor_advance_required': cursor_advance_required,
        'resident_history_cursor_id': cursor_before,
        'handoff_status': handoff_status,
        'handoff_injected_this_turn': handoff_injected,
        'carryover_count': len(carryover_ids),
        'carryover_message_ids': carryover_ids,
        'carryover_injected_this_turn': carryover_injected,
        'selection_finalized': bool(ctx.get('selection_finalized_at')),
        'state_injected_this_turn': state_injected,
        'state_mode': state_mode,
        'legacy_cold_once_injected': False,
        'auto_recall_injected': False,
        'relationship_context_injected': False,
        'pre_boundary_history_injected': False,
        'diary_summary_injected': False,
        'daily_summary_injected': False,
        'weekly_summary_injected': False,
    }

    return {
        'static': static_system,
        'day_handoff': handoff_prompt if handoff_injected else '',
        'day_handoff_content': handoff_content if handoff_status == HANDOFF_READY else None,
        'carryover_messages': carryover_messages if carryover_injected else [],
        'state': state_text,
        'state_snapshot': state_snapshot,
        'current_day_history': current_day_history,
        'layers': layers,
        'manifest': manifest,
    }
