"""Daily Soft Window provider history assembly.

Assembles provider-facing context for a daily epoch without legacy cold_once,
DIARY/DAILY/WEEKLY, auto recall, or Relationship Context.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

from chat.daily_context import (
    DEFAULT_CHAT_ID,
    HANDOFF_ABSENT,
    HANDOFF_READY,
    STATUS_PROVISIONAL,
    _USER_AUTHORS,
    _message_display_content,
    _table_columns,
    chat_day_window,
    finalize_zero_for_first_user_message,
    format_formal_handoff_prompt,
    get_daily_context_by_id,
    get_day_handoff,
    get_latest_handoff_for_day,
    get_selected_carryover_messages,
    is_formal_chat_message,
)
from chat.day_handoff import TZ_OFFSET_HOURS


def _connect(db_path: Optional[str]):
    from chat.daily_context import _connect as dc_connect
    return dc_connect(db_path)


def _fetch_current_day_history(
    *,
    boundary_message_id: int,
    local_day: str,
    db_path: Optional[str] = None,
    up_to_message_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    _d, start_at, _end, next_start = chat_day_window(local_day)
    conn = _connect(db_path)
    try:
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        rows = conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id > ? '
            'ORDER BY id ASC' % ', '.join(select_cols),
            (start_at, next_start, int(boundary_message_id or 0)),
        ).fetchall()
        out = []
        for r in rows:
            mid = int(r['id'])
            if up_to_message_id is not None and mid > int(up_to_message_id):
                break
            if not is_formal_chat_message(r):
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


def _resolve_handoff(
    ctx: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], str, str]:
    handoff_status = HANDOFF_ABSENT
    content = None
    prompt = ''
    if ctx.get('handoff_id'):
        h = get_day_handoff(int(ctx['handoff_id']), db_path=db_path)
        if h:
            handoff_status = h.get('status') or HANDOFF_ABSENT
            content = h.get('content')
    else:
        day_dt = datetime.datetime.strptime(ctx['local_day'], '%Y-%m-%d')
        prev = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        h = get_latest_handoff_for_day(ctx['chat_id'], prev, db_path=db_path)
        if h:
            handoff_status = h.get('status') or HANDOFF_ABSENT
            content = h.get('content')
    if handoff_status == HANDOFF_READY and content:
        prompt = format_formal_handoff_prompt(content)
    return content, handoff_status, prompt


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
    last_state_snapshot: Optional[dict[str, str]] = None,
    inject_handoff: bool = True,
    inject_carryover: bool = True,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble provider context layers for one daily epoch turn.

    Order: static → handoff → carryover → state → current-day history.
    """
    _ = TZ_OFFSET_HOURS
    ctx = dict(daily_context)
    context_id = int(ctx['id'])

    if current_user_message_id and not ctx.get('selection_finalized_at'):
        if ctx.get('status') in (
            STATUS_PROVISIONAL, 'ABSENT', 'FAILED_RETRYABLE', 'COMPACTING',
        ):
            finalize_zero_for_first_user_message(
                context_id, int(current_user_message_id), db_path=db_path,
            )
            refreshed = get_daily_context_by_id(context_id, db_path=db_path)
            if refreshed:
                ctx = refreshed

    handoff_content, handoff_status, handoff_prompt = _resolve_handoff(ctx, db_path=db_path)
    handoff_injected = bool(inject_handoff and is_cold and handoff_prompt)

    carryover_messages = get_selected_carryover_messages(context_id, db_path=db_path)
    carryover_ids = [int(m['message_id']) for m in carryover_messages]
    carryover_injected = bool(inject_carryover and is_cold and carryover_messages)

    state_text, state_mode, state_snapshot = _build_state_text(
        is_cold=is_cold, last_snapshot=last_state_snapshot,
    )
    state_injected = bool(state_text)

    current_day_history = _fetch_current_day_history(
        boundary_message_id=int(ctx.get('boundary_message_id') or 0),
        local_day=str(ctx['local_day']),
        db_path=db_path,
        up_to_message_id=current_user_message_id,
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
        'resident_generation': int(ctx.get('resident_generation') or 1),
        'handoff_status': handoff_status,
        'handoff_injected_this_turn': handoff_injected,
        'carryover_count': len(carryover_ids),
        'carryover_message_ids': carryover_ids,
        'carryover_injected_this_turn': carryover_injected,
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
