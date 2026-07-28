"""Manual context window contract — P-CONTEXT-MANUAL-WINDOW-R0.

Canonical resolver for explicit user-initiated window switches. Independent of
the legacy daily 04:00 auto-create resolver (``resolve_current_daily_context_for_api``).

Does not call models, generate handoffs, or enable itself (``DAILY_SOFT_WINDOW_ENABLED=0``).
"""
from __future__ import annotations

import datetime
import re
import sqlite3
from typing import Any, Optional

from chat.daily_context import (
    ALLOWED_CARRYOVER_COUNTS,
    CARRYOVER_UNIT,
    CHAT_DAY_START_HOUR,
    DEFAULT_CHAT_ID,
    DEFAULT_TIMEZONE,
    STATUS_PROVISIONAL,
    TZ_OFFSET_HOURS,
    _active_epoch_high_water,
    _connect,
    _parse_local_dt,
    _row_to_dict,
    _table_columns,
    _wake_content_set,
    ensure_schema,
    get_boundary_message_id,
    group_carryover_rounds,
    is_formal_chat_message,
)
from chat.daily_schema import META_SOURCE_KIND_CUTOVER, get_meta_int

WINDOW_MODE_LEGACY_DAILY = 'legacy_daily'
WINDOW_MODE_MANUAL = 'manual'

CLOSE_REASON_MANUAL = 'manual'
CLOSE_REASON_CAPACITY_RESCUE = 'capacity_rescue'

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)
_POSITIVE_QUERY_INT_RE = re.compile(r'^[1-9]\d*$')


class ContextWindowError(Exception):
    """Base error for manual context window."""


class NoOpenContextWindowError(ContextWindowError):
    """No open manual/legacy window; closed-only history (fail closed)."""


class StaleSourceContextError(ContextWindowError):
    """Source context is no longer the open canonical window (HTTP 409)."""


class WindowBusyError(ContextWindowError):
    """Active provider turn lease blocks switch (HTTP 423)."""


class IdempotencyMismatchError(ContextWindowError):
    """Same request_id with different payload (HTTP 409)."""


def enabled() -> bool:
    from chat.daily_context import enabled as daily_enabled
    return daily_enabled()


def _shanghai_now(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    return now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))


def shanghai_calendar_day(now: Optional[datetime.datetime] = None) -> str:
    """Shanghai wall-clock natural date — no 04:00 chat-day boundary."""
    return _shanghai_now(now).strftime('%Y-%m-%d')


def parse_strict_json_positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('%s must be a JSON integer' % name)
    if value <= 0:
        raise ValueError('%s must be a positive integer' % name)
    return value


def parse_strict_json_carryover_count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError('count must be a JSON integer')
    if value not in ALLOWED_CARRYOVER_COUNTS:
        raise ValueError('count must be one of %s' % sorted(ALLOWED_CARRYOVER_COUNTS))
    return value


def parse_strict_query_positive_int(name: str, raw: Any) -> int:
    if raw is None or raw == '':
        raise ValueError('%s is required' % name)
    if isinstance(raw, bool):
        raise ValueError('%s must be a positive integer' % name)
    if isinstance(raw, float):
        raise ValueError('%s must be a positive integer' % name)
    if isinstance(raw, int):
        if raw <= 0:
            raise ValueError('%s must be a positive integer' % name)
        return raw
    text = str(raw).strip()
    if not _POSITIVE_QUERY_INT_RE.match(text):
        raise ValueError('%s must be a positive integer' % name)
    return int(text)


def _validate_request_id(request_id: str) -> str:
    value = str(request_id or '').strip()
    if not value or not _UUID_RE.match(value):
        raise ValueError('request_id must be a valid UUID')
    return value.lower()


def _collect_context_formal_messages(
    conn: sqlite3.Connection,
    *,
    context_id: int,
    context_epoch: int,
) -> list[Any]:
    cols = _table_columns(conn, 'chat_messages')
    if not cols:
        return []
    select_cols = ['m.id', 'm.author', 'm.content', 'm.created_at']
    for optional in ('tool_calls', 'source_kind', 'image_url'):
        if optional in cols:
            select_cols.append('m.' + optional)
    wake_contents = _wake_content_set(conn)
    cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)
    rows = conn.execute(
        'SELECT %s FROM chat_messages m '
        'INNER JOIN daily_message_contexts dmc ON dmc.message_id = m.id '
        'WHERE dmc.context_id=? AND dmc.context_epoch=? '
        'ORDER BY m.id ASC' % ', '.join(select_cols),
        (int(context_id), int(context_epoch)),
    ).fetchall()
    return [
        r for r in rows
        if is_formal_chat_message(r, wake_contents=wake_contents, cutover_id=cutover)
    ]


def _carryover_ids_conn(conn: sqlite3.Connection, context_id: int) -> list[int]:
    return [
        int(r['message_id'])
        for r in conn.execute(
            'SELECT message_id FROM daily_carryover_messages '
            'WHERE context_id=? ORDER BY ordinal ASC',
            (int(context_id),),
        ).fetchall()
    ]


def _is_resident_turn_active_conn(
    conn: sqlite3.Connection,
    context_id: int,
    resident_generation: int,
    now_dt: datetime.datetime,
) -> bool:
    row = conn.execute(
        'SELECT expires_at FROM daily_resident_turn_leases '
        'WHERE context_id=? AND resident_generation=?',
        (int(context_id), int(resident_generation)),
    ).fetchone()
    if row is None:
        return False
    try:
        exp_dt = _parse_local_dt(str(row['expires_at']))
    except ValueError:
        return False
    return exp_dt > now_dt


def _last_formal_message_id(messages: list[Any]) -> int:
    if not messages:
        return 0
    return int(messages[-1]['id'])


def _count_formal_rounds(messages: list[Any]) -> int:
    return len(group_carryover_rounds(messages))


def _bootstrap_legacy_context_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    now_dt: datetime.datetime,
) -> dict[str, Any]:
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM daily_contexts WHERE chat_id=?',
        (chat_id,),
    ).fetchone()
    if int(row['c']) > 0:
        return {}

    local_day = shanghai_calendar_day(now_dt)
    boundary_id = get_boundary_message_id(conn, local_day=local_day, chat_id=chat_id)
    now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    cur = conn.execute(
        '''INSERT INTO daily_contexts (
            chat_id, local_day, timezone, boundary_hour, context_epoch,
            boundary_message_id, status, carryover_count, is_backfill,
            resident_generation, version, created_at, updated_at,
            window_mode, opened_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, 0, 0, 1, 1, ?, ?,
                  ?, ?)''',
        (
            chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
            boundary_id, STATUS_PROVISIONAL, now_s, now_s,
            WINDOW_MODE_LEGACY_DAILY, now_s,
        ),
    )
    context_id = int(cur.lastrowid)
    created = _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
    ).fetchone())
    assert created is not None
    return created


def _find_open_manual_window_conn(
    conn: sqlite3.Connection,
    chat_id: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        '''SELECT * FROM daily_contexts
           WHERE chat_id=? AND window_mode=? AND closed_at IS NULL
             AND is_backfill=0
           ORDER BY context_epoch DESC LIMIT 1''',
        (chat_id, WINDOW_MODE_MANUAL),
    ).fetchone())


def _find_latest_legacy_bootstrap_conn(
    conn: sqlite3.Connection,
    chat_id: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        '''SELECT * FROM daily_contexts
           WHERE chat_id=? AND window_mode=? AND is_backfill=0
           ORDER BY context_epoch DESC LIMIT 1''',
        (chat_id, WINDOW_MODE_LEGACY_DAILY),
    ).fetchone())


def resolve_canonical_context_row_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    now_dt = _shanghai_now(now)
    manual = _find_open_manual_window_conn(conn, chat_id)
    if manual is not None:
        return manual
    legacy = _find_latest_legacy_bootstrap_conn(conn, chat_id)
    if legacy is not None:
        return legacy
    any_count = int(conn.execute(
        'SELECT COUNT(*) FROM daily_contexts WHERE chat_id=?', (chat_id,),
    ).fetchone()[0])
    if any_count > 0:
        raise NoOpenContextWindowError('no_open_context_window')
    boot = _bootstrap_legacy_context_conn(conn, chat_id=chat_id, now_dt=now_dt)
    if boot:
        return boot
    legacy = _find_latest_legacy_bootstrap_conn(conn, chat_id)
    if legacy is None:
        raise ContextWindowError('bootstrap failed')
    return legacy


def _summary_from_row_conn(
    conn: sqlite3.Connection,
    ctx: dict[str, Any],
    *,
    now_dt: datetime.datetime,
) -> dict[str, Any]:
    context_id = int(ctx['id'])
    context_epoch = int(ctx['context_epoch'])
    messages = _collect_context_formal_messages(
        conn, context_id=context_id, context_epoch=context_epoch,
    )
    formal_round_count = _count_formal_rounds(messages)
    selected_round_count = int(ctx.get('carryover_count') or 0)
    raw_requested = ctx.get('carryover_requested_count')
    if raw_requested is None and ctx.get('selection_finalized_at'):
        requested_round_count = selected_round_count
    elif raw_requested is None:
        requested_round_count = None
    else:
        requested_round_count = int(raw_requested)
    if ctx.get('selection_finalized_at'):
        selected_message_ids = _carryover_ids_conn(conn, context_id)
    else:
        selected_message_ids = []
    busy = None
    if _is_resident_turn_active_conn(
        conn, context_id, int(ctx.get('resident_generation') or 1), now_dt,
    ):
        busy = 'window_busy'
    can_switch = busy is None and ctx.get('closed_at') is None
    out: dict[str, Any] = {
        'context_id': context_id,
        'context_epoch': context_epoch,
        'window_mode': str(ctx.get('window_mode') or WINDOW_MODE_LEGACY_DAILY),
        'opened_local_day': str(ctx['local_day']),
        'opened_at': str(ctx.get('opened_at') or ctx.get('created_at') or ''),
        'boundary_message_id': int(ctx.get('boundary_message_id') or 0),
        'source_context_id': ctx.get('source_context_id'),
        'resident_generation': int(ctx.get('resident_generation') or 1),
        'formal_round_count': formal_round_count,
        'can_switch': can_switch,
        'requested_round_count': requested_round_count,
        'selected_round_count': selected_round_count,
        'selected_message_count': len(selected_message_ids),
        'selected_message_ids': selected_message_ids,
        'version': int(ctx.get('version') or 1),
    }
    if not can_switch and busy:
        out['can_switch_reason'] = busy
    return out


def get_current_context_window(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        ctx = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        conn.commit()
        return dict(ctx)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_window_summary(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        ctx = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        summary = _summary_from_row_conn(conn, ctx, now_dt=now_dt)
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_context_window_carryover_rounds(
    source_context_id: int,
    source_context_epoch: int,
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    limit_rounds: int = 10,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    ensure_schema(db_path)
    source_id = parse_strict_json_positive_int('source_context_id', source_context_id)
    source_epoch = parse_strict_json_positive_int('source_context_epoch', source_context_epoch)
    now_dt = _shanghai_now(now)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        if int(current['id']) != source_id or int(current['context_epoch']) != source_epoch:
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')
        if current.get('closed_at'):
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')
        if _is_resident_turn_active_conn(
            conn, source_id, int(current.get('resident_generation') or 1), now_dt,
        ):
            conn.rollback()
            raise WindowBusyError('window_busy')
        messages = _collect_context_formal_messages(
            conn, context_id=source_id, context_epoch=source_epoch,
        )
        conn.commit()
    except (StaleSourceContextError, WindowBusyError):
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    all_rounds = group_carryover_rounds(messages)
    available_round_count = len(all_rounds)
    rounds = all_rounds[-limit_rounds:] if limit_rounds else list(all_rounds)
    return {
        'source_context_id': source_id,
        'source_context_epoch': source_epoch,
        'carryover_unit': CARRYOVER_UNIT,
        'available_round_count': available_round_count,
        'rounds': rounds,
    }


def _find_idempotent_target_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    request_id: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        '''SELECT * FROM daily_contexts
           WHERE chat_id=? AND switch_request_id=? LIMIT 1''',
        (chat_id, request_id),
    ).fetchone())


def _validate_idempotent_payload_conn(
    conn: sqlite3.Connection,
    existing_target: dict[str, Any],
    *,
    chat_id: str,
    source_id: int,
    source_epoch: int,
    count: int,
    close_reason: str,
) -> dict[str, Any]:
    if str(existing_target.get('chat_id') or '') != str(chat_id):
        raise IdempotencyMismatchError('idempotency_mismatch')
    if int(existing_target.get('source_context_id') or 0) != source_id:
        raise IdempotencyMismatchError('idempotency_mismatch')
    if int(existing_target.get('carryover_requested_count') or 0) != count:
        raise IdempotencyMismatchError('idempotency_mismatch')
    stored_source = int(existing_target.get('source_context_id') or 0)
    src_row = conn.execute(
        'SELECT context_epoch, close_reason FROM daily_contexts WHERE id=?',
        (stored_source,),
    ).fetchone()
    if src_row is None:
        raise IdempotencyMismatchError('idempotency_mismatch')
    if int(src_row['context_epoch']) != source_epoch:
        raise IdempotencyMismatchError('idempotency_mismatch')
    if str(src_row['close_reason'] or '') != str(close_reason):
        raise IdempotencyMismatchError('idempotency_mismatch')
    source_full = conn.execute(
        'SELECT * FROM daily_contexts WHERE id=?', (stored_source,),
    ).fetchone()
    if source_full is None:
        raise IdempotencyMismatchError('idempotency_mismatch')
    return dict(source_full)


def _switch_result_from_target_conn(
    conn: sqlite3.Connection,
    *,
    source: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    target_id = int(target['id'])
    selected_message_ids = _carryover_ids_conn(conn, target_id)
    requested = int(target.get('carryover_requested_count') or 0)
    selected_round_count = int(target.get('carryover_count') or 0)
    return {
        'source_context_id': int(source['id']),
        'source_context_epoch': int(source['context_epoch']),
        'target_context_id': target_id,
        'target_context_epoch': int(target['context_epoch']),
        'window_mode': WINDOW_MODE_MANUAL,
        'requested_round_count': requested,
        'selected_round_count': selected_round_count,
        'selected_message_count': len(selected_message_ids),
        'selected_message_ids': selected_message_ids,
        'boundary_message_id': int(target.get('boundary_message_id') or 0),
        'resident_generation': int(target.get('resident_generation') or 1),
        'switched_at': str(target.get('opened_at') or target.get('created_at') or ''),
    }


def switch_context_window(
    *,
    source_context_id: int,
    source_context_epoch: int,
    count: int,
    request_id: str,
    chat_id: str = DEFAULT_CHAT_ID,
    close_reason: str = CLOSE_REASON_MANUAL,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    if close_reason not in (CLOSE_REASON_MANUAL, CLOSE_REASON_CAPACITY_RESCUE):
        raise ValueError('invalid close_reason')

    source_id = parse_strict_json_positive_int('source_context_id', source_context_id)
    source_epoch = parse_strict_json_positive_int('source_context_epoch', source_context_epoch)
    count = parse_strict_json_carryover_count(count)
    req_id = _validate_request_id(request_id)

    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    local_day = shanghai_calendar_day(now_dt)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')

        existing_target = _find_idempotent_target_conn(
            conn, chat_id=chat_id, request_id=req_id,
        )
        if existing_target is not None:
            source = _validate_idempotent_payload_conn(
                conn, existing_target,
                chat_id=chat_id,
                source_id=source_id,
                source_epoch=source_epoch,
                count=count,
                close_reason=close_reason,
            )
            result = _switch_result_from_target_conn(
                conn, source=source, target=existing_target,
            )
            conn.commit()
            return result

        current = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        if int(current['id']) != source_id or int(current['context_epoch']) != source_epoch:
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')
        if current.get('closed_at'):
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')

        source_version = int(current.get('version') or 1)
        if _is_resident_turn_active_conn(
            conn, source_id, int(current.get('resident_generation') or 1), now_dt,
        ):
            conn.rollback()
            raise WindowBusyError('window_busy')

        messages = _collect_context_formal_messages(
            conn, context_id=source_id, context_epoch=source_epoch,
        )
        boundary_id = _last_formal_message_id(messages)
        all_rounds = group_carryover_rounds(messages)
        if count == 0:
            selected_rounds: list[dict[str, Any]] = []
        else:
            selected_rounds = (
                all_rounds[-count:] if len(all_rounds) >= count else list(all_rounds)
            )
        selected_round_count = len(selected_rounds)
        next_epoch = _active_epoch_high_water(conn, chat_id) + 1

        close_cur = conn.execute(
            '''UPDATE daily_contexts SET closed_at=?, close_reason=?, version=version+1,
               updated_at=? WHERE id=? AND context_epoch=? AND version=? AND closed_at IS NULL''',
            (now_s, close_reason, now_s, source_id, source_epoch, source_version),
        )
        if close_cur.rowcount != 1:
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')

        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, carryover_requested_count,
                selection_finalized_at, is_backfill, resident_generation, version,
                created_at, updated_at, window_mode, opened_at, closed_at,
                close_reason, source_context_id, switch_request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, NULL, NULL, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                next_epoch, boundary_id, STATUS_PROVISIONAL,
                selected_round_count, count, now_s,
                now_s, now_s, WINDOW_MODE_MANUAL, now_s,
                source_id, req_id,
            ),
        )
        target_id = int(cur.lastrowid)

        ordinal = 0
        for rnd in selected_rounds:
            for mid in rnd['message_ids']:
                conn.execute(
                    'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                    'VALUES (?,?,?)',
                    (target_id, ordinal, int(mid)),
                )
                ordinal += 1

        source_row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
        ).fetchone()
        target_row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (target_id,),
        ).fetchone()
        assert source_row is not None and target_row is not None
        result = _switch_result_from_target_conn(
            conn, source=dict(source_row), target=dict(target_row),
        )
        conn.commit()
        return result
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        replay = _connect(db_path)
        try:
            replay.execute('BEGIN IMMEDIATE')
            existing = _find_idempotent_target_conn(
                replay, chat_id=chat_id, request_id=req_id,
            )
            if existing is not None:
                source = _validate_idempotent_payload_conn(
                    replay, existing,
                    chat_id=chat_id,
                    source_id=source_id,
                    source_epoch=source_epoch,
                    count=count,
                    close_reason=close_reason,
                )
                result = _switch_result_from_target_conn(
                    replay, source=source, target=existing,
                )
                replay.commit()
                return result
            replay.rollback()
        finally:
            replay.close()
        raise StaleSourceContextError('stale_source_context') from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
