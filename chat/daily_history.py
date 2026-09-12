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
    HANDOFF_PROVIDER_DURABLE_FIELDS,
    HANDOFF_PROVIDER_SCENE_FIELDS,
    HotTurnCursorError,
    _USER_AUTHORS,
    _message_display_content,
    _table_columns,
    chat_day_window,
    ensure_carryover_zero_if_user_messages_exist,
    format_formal_handoff_prompt,
    get_daily_context_by_id,
    get_resident_history_cursor,
    is_canonical_conversation_message,
    resolve_bound_handoff,
)
from chat.daily_schema import META_SOURCE_KIND_CUTOVER, get_meta_int
from chat.day_handoff import TZ_OFFSET_HOURS

# Manual Forge targets only — never decide from window_mode alone.
_FORGE_TARGET_WINDOW_MODES = frozenset({'manual', 'manual_staged'})

COLD_RECENT_OWNER_FORGE = 'forge_transcript'
COLD_RECENT_OWNER_CARRYOVER = 'daily_carryover'
COLD_RECENT_OWNER_NONE = 'none'
CARRYOVER_SUPPRESSED_FORGE_OWNS = 'forge_transcript_owns_selected_rounds'
CARRYOVER_SUPPRESSED_NONE = 'none'

HANDOFF_PROJECTION_DURABLE_ONLY = 'durable_only'
HANDOFF_PROJECTION_DURABLE_PLUS_SCENE = 'durable_plus_scene'
HANDOFF_PROJECTION_NONE = 'none'

HANDOFF_SCENE_OWNER_FORGE = 'forge_transcript'
HANDOFF_SCENE_OWNER_CARRYOVER = 'daily_carryover'
HANDOFF_SCENE_OWNER_HANDOFF = 'handoff'
HANDOFF_SCENE_OWNER_NONE = 'none'


def provider_handoff_fields_for_recent_owner(cold_recent_owner: str) -> tuple[str, ...]:
    """C2: durable always; scene only when no recent-conversation owner."""
    if cold_recent_owner in (COLD_RECENT_OWNER_FORGE, COLD_RECENT_OWNER_CARRYOVER):
        return tuple(HANDOFF_PROVIDER_DURABLE_FIELDS)
    return tuple(HANDOFF_PROVIDER_DURABLE_FIELDS) + tuple(HANDOFF_PROVIDER_SCENE_FIELDS)


def forge_transcript_owns_selected_carryover(
    daily_context: dict[str, Any],
    *,
    provider_claude_session_id: Optional[str] = None,
) -> bool:
    """True only when the *current provider* still holds the Forge transcript.

    Target-window Forge identity alone is not enough. Suppress carryover only when:

    1. context is a Manual Forge target (mode + source + switch + forge session), AND
    2. ``provider_claude_session_id`` equals the bound forge ``claude_session_id``
       (i.e. live process is still on that transcript, typically via ``--resume``).

    Fresh ``_spawn`` / missing live session / mismatched session → False (KEEP carryover).
    Continuity over token savings when ownership cannot be proved.
    """
    ctx = daily_context or {}
    mode = str(ctx.get('window_mode') or '').strip()
    if mode not in _FORGE_TARGET_WINDOW_MODES:
        return False
    try:
        source_id = int(ctx.get('source_context_id') or 0)
    except (TypeError, ValueError):
        return False
    if source_id <= 0:
        return False
    if not str(ctx.get('switch_request_id') or '').strip():
        return False
    forge_sid = str(ctx.get('claude_session_id') or '').strip()
    if not forge_sid:
        return False
    live_sid = str(provider_claude_session_id or '').strip()
    if not live_sid or live_sid != forge_sid:
        return False
    return True


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
    include_assistant_wake: bool = False,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url', 'file_url', 'file_name', 'attachments', 'cache_info'):
            if optional in cols:
                select_cols.append(optional)
        wake_contents = _wake_content_set(conn)
        cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)

        def _to_item(r: Any) -> Optional[dict[str, Any]]:
            if not is_canonical_conversation_message(
                r,
                include_assistant_wake=include_assistant_wake,
                wake_contents=wake_contents,
                cutover_id=cutover,
            ):
                return None
            mid = int(r['id'])
            role = 'user' if str(r['author']).lower() in _USER_AUTHORS else 'assistant'
            return {
                'message_id': mid,
                'role': role,
                'author': str(r['author']),
                'content': _message_display_content(r),
                'image_url': str(r['image_url'] or '') if 'image_url' in r.keys() else '',
                'file_url': str(r['file_url'] or '') if 'file_url' in r.keys() else '',
                'file_name': str(r['file_name'] or '') if 'file_name' in r.keys() else '',
                'attachments': r['attachments'] if 'attachments' in r.keys() else '',
                'created_at': str(r['created_at'] or ''),
            }

        mapped_items: dict[int, dict[str, Any]] = {}
        if context_id is not None and context_epoch is not None:
            for r in conn.execute(
                'SELECT %s FROM chat_messages m '
                'INNER JOIN daily_message_contexts dmc ON dmc.message_id = m.id '
                'WHERE dmc.context_id=? AND dmc.context_epoch=? '
                'ORDER BY m.id ASC' % ', '.join('m.' + c for c in select_cols),
                (int(context_id), int(context_epoch)),
            ).fetchall():
                mid = int(r['id'])
                if exclude_message_id is not None and mid == int(exclude_message_id):
                    continue
                if after_message_id is not None and mid <= int(after_message_id):
                    continue
                item = _to_item(r)
                if item is not None:
                    mapped_items[mid] = item

        _d, start_at, _end, next_start = chat_day_window(local_day)
        other_mapped: frozenset[int] = frozenset()
        if context_id is not None:
            other_rows = conn.execute(
                'SELECT message_id FROM daily_message_contexts WHERE context_id != ?',
                (int(context_id),),
            ).fetchall()
            other_mapped = frozenset(int(r[0]) for r in other_rows)

        legacy_items: dict[int, dict[str, Any]] = {}
        for r in conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id > ? '
            'ORDER BY id ASC' % ', '.join(select_cols),
            (start_at, next_start, int(boundary_message_id or 0)),
        ).fetchall():
            mid = int(r['id'])
            if mid in mapped_items or mid in other_mapped:
                continue
            if exclude_message_id is not None and mid == int(exclude_message_id):
                continue
            if after_message_id is not None and mid <= int(after_message_id):
                continue
            if up_to_message_id is not None and mid > int(up_to_message_id):
                continue
            item = _to_item(r)
            if item is not None:
                legacy_items[mid] = item

        rows_by_id = dict(mapped_items)
        for mid, item in legacy_items.items():
            rows_by_id.setdefault(mid, item)
        return [rows_by_id[k] for k in sorted(rows_by_id.keys())]
    finally:
        conn.close()


def _build_state_text(
    *,
    is_cold: bool,
    last_snapshot: Optional[dict[str, str]] = None,
) -> tuple[str, str, dict[str, str]]:
    from chat.persona_state_semantic import (
        format_persona_semantic_diff,
        format_persona_semantic_snapshot,
        translate_raw_state_to_persona_semantic,
    )
    from chat.system_builder import build_cc_state

    raw = build_cc_state(lean=True)
    if not isinstance(raw, dict):
        raw = {}
    snapshot = {str(k): str(v) for k, v in raw.items()}
    semantic_current = translate_raw_state_to_persona_semantic(snapshot)
    if is_cold or not last_snapshot:
        text = format_persona_semantic_snapshot(semantic_current)
        mode = 'snapshot' if text else 'none'
        return text, mode, snapshot
    semantic_previous = translate_raw_state_to_persona_semantic(last_snapshot)
    delta = format_persona_semantic_diff(semantic_previous, semantic_current)
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
    history_token_budget: Optional[int] = None,
    provider_claude_session_id: Optional[str] = None,
    history_override: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Assemble provider context layers for one daily epoch turn.

    Order: static → handoff → carryover → state → current-day history.

    Resident history:
    - cold_like (cold or respawn): fetch full epoch history (excluding current
      user), then select a newest complete-round suffix under
      ``HISTORY_TOKEN_BUDGET`` / ``history_token_budget`` for the resident
      bootstrap. DB history and membership are never mutated.
    - hot: only messages after resident cursor; fail closed if cursor missing

    Does not persist resident cursor — caller must invoke
    advance_resident_history_cursor() after assistant message lands.

    ``provider_claude_session_id``: live Claude session id when the provider
    process is already holding a transcript (e.g. Forge ``--resume``). Required
    to suppress Manual Forge carryover replay (C1 Exact-Once).
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

    from chat.daily_context import get_selected_carryover_messages
    carryover_messages = get_selected_carryover_messages(context_id, db_path=db_path)
    carryover_ids = [int(m['message_id']) for m in carryover_messages]
    forge_owns_recent = forge_transcript_owns_selected_carryover(
        ctx,
        provider_claude_session_id=provider_claude_session_id,
    )
    would_inject_carryover = bool(inject_carryover and cold_like and carryover_messages)
    # C1: suppress only when live provider still holds the Forge transcript.
    # Fresh spawn / unproven ownership → KEEP carryover (exact-once for selected rounds).
    carryover_injected = bool(would_inject_carryover and not forge_owns_recent)
    if would_inject_carryover and forge_owns_recent:
        cold_recent_owner = COLD_RECENT_OWNER_FORGE
        carryover_suppressed_reason = CARRYOVER_SUPPRESSED_FORGE_OWNS
    elif carryover_injected:
        cold_recent_owner = COLD_RECENT_OWNER_CARRYOVER
        carryover_suppressed_reason = CARRYOVER_SUPPRESSED_NONE
    else:
        cold_recent_owner = COLD_RECENT_OWNER_NONE
        carryover_suppressed_reason = CARRYOVER_SUPPRESSED_NONE

    # C2: project Handoff after C1 recent-scene ownership is known.
    handoff_ready = bool(handoff_status == HANDOFF_READY and handoff_content)
    if handoff_ready and inject_handoff and cold_like:
        projection_fields = provider_handoff_fields_for_recent_owner(cold_recent_owner)
        handoff_prompt = format_formal_handoff_prompt(
            handoff_content, fields=projection_fields,
        )
        handoff_injected = bool(handoff_prompt)
        if cold_recent_owner == COLD_RECENT_OWNER_FORGE:
            handoff_projection = HANDOFF_PROJECTION_DURABLE_ONLY
            handoff_scene_owner = HANDOFF_SCENE_OWNER_FORGE
        elif cold_recent_owner == COLD_RECENT_OWNER_CARRYOVER:
            handoff_projection = HANDOFF_PROJECTION_DURABLE_ONLY
            handoff_scene_owner = HANDOFF_SCENE_OWNER_CARRYOVER
        else:
            handoff_projection = HANDOFF_PROJECTION_DURABLE_PLUS_SCENE
            handoff_scene_owner = HANDOFF_SCENE_OWNER_HANDOFF
    else:
        handoff_prompt = ''
        handoff_injected = False
        handoff_projection = HANDOFF_PROJECTION_NONE
        handoff_scene_owner = HANDOFF_SCENE_OWNER_NONE

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

    cold_history_stats: dict[str, Any] = {}
    if history_override is None:
        current_day_history = _fetch_current_day_history(
            boundary_message_id=int(ctx.get('boundary_message_id') or 0),
            local_day=str(ctx['local_day']),
            db_path=db_path,
            after_message_id=after_cursor,
            exclude_message_id=current_user_message_id,
            up_to_message_id=current_user_message_id,
            context_id=context_id,
            context_epoch=int(ctx.get('context_epoch') or 0),
            include_assistant_wake=cold_like,
        )
        if cold_like:
            from chat.daily_cold_history import select_newest_complete_rounds_under_budget
            current_day_history, cold_history_stats = select_newest_complete_rounds_under_budget(
                current_day_history,
                history_token_budget=history_token_budget,
                allow_assistant_only=cold_like,
            )
    else:
        # Production ContextPlan projection supplies the complete selected
        # history. This branch deliberately performs no legacy selection.
        current_day_history = [dict(item) for item in history_override]

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

    carryover_round_count = int(ctx.get('carryover_count') or 0)
    carryover_message_count = len(carryover_ids)

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
        'handoff_projection': handoff_projection,
        'handoff_scene_owner': handoff_scene_owner,
        'carryover_unit': 'round',
        'carryover_count': carryover_round_count,
        'carryover_round_count': carryover_round_count,
        'carryover_message_count': carryover_message_count,
        'carryover_message_ids': carryover_ids,
        'carryover_injected_this_turn': carryover_injected,
        'cold_recent_owner': cold_recent_owner,
        'carryover_suppressed_reason': carryover_suppressed_reason,
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
    if cold_history_stats:
        manifest.update(cold_history_stats)

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
