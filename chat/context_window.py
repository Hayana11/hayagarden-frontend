"""Manual context window contract — P-CONTEXT-MANUAL-WINDOW-R0.

Canonical resolver for explicit user-initiated window switches. Independent of
the legacy daily 04:00 auto-create resolver (``resolve_current_daily_context_for_api``).

Does not call models, generate handoffs, or enable itself (``DAILY_SOFT_WINDOW_ENABLED=0``).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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

logger = logging.getLogger(__name__)

WINDOW_MODE_LEGACY_DAILY = 'legacy_daily'
WINDOW_MODE_MANUAL = 'manual'

CLOSE_REASON_MANUAL = 'manual'
CLOSE_REASON_CAPACITY_RESCUE = 'capacity_rescue'

INTENT_RESERVED = 'reserved'
INTENT_FORGING = 'forging'
INTENT_READY = 'ready'
INTENT_COMMITTING = 'committing'
INTENT_HANDOFF_PENDING = 'handoff_pending'
INTENT_COMMITTED = 'committed'
INTENT_FAILED = 'failed'
INTENT_RELEASED = 'released'

ACTIVE_INTENT_STATUSES = frozenset({
    INTENT_RESERVED,
    INTENT_FORGING,
    INTENT_READY,
    INTENT_COMMITTING,
    INTENT_HANDOFF_PENDING,
})

TERMINAL_FAILURE_STATUSES = frozenset({INTENT_FAILED, INTENT_RELEASED})

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


class SwitchInProgressError(ContextWindowError):
    """Active switch intent blocks formal turns / concurrent switch (HTTP 423)."""


class CarryoverMessageUnforgeableError(ContextWindowError):
    """Locked selected messages cannot be forged (HTTP 409)."""


class SwitchFailedError(ContextWindowError):
    """Switch failed with a terminal error_code."""

    def __init__(self, error_code: str, message: Optional[str] = None):
        super().__init__(message or error_code)
        self.error_code = error_code


class SwitchHooksRequiredError(ContextWindowError):
    """Production switch path requires explicit SwitchHooks (fail-closed)."""

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or 'switch_hooks_required')
        self.error_code = 'switch_hooks_required'


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
    """Latest legacy_daily row; return only when that row is still open.

    Never fall back to an older epoch when the newest legacy is closed.
    """
    latest = _row_to_dict(conn.execute(
        '''SELECT * FROM daily_contexts
           WHERE chat_id=? AND window_mode=? AND is_backfill=0
           ORDER BY context_epoch DESC LIMIT 1''',
        (chat_id, WINDOW_MODE_LEGACY_DAILY),
    ).fetchone())
    if latest is None:
        return None
    if latest.get('closed_at'):
        return None
    return latest


# Backward-compatible alias.
_find_latest_open_legacy_bootstrap_conn = _find_latest_legacy_bootstrap_conn


def resolve_canonical_context_row_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Resolve the single open canonical window.

    Fail-closed recovery:
    - Prefer an open ``manual`` window.
    - Else the **latest** ``legacy_daily`` row only when ``closed_at IS NULL``.
    - Else if any historical rows exist (closed manual/legacy), raise
      ``NoOpenContextWindowError`` — never silently reopen a closed or older window.
    - Else bootstrap one legacy row on a truly empty chat.
    """
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
    if busy is None:
        existing_tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if 'context_switch_intents' in existing_tables:
            active = conn.execute(
                'SELECT 1 FROM context_switch_intents WHERE chat_id=? AND status IN (%s) LIMIT 1'
                % ','.join('?' for _ in ACTIVE_INTENT_STATUSES),
                (str(ctx.get('chat_id') or DEFAULT_CHAT_ID), *tuple(ACTIVE_INTENT_STATUSES)),
            ).fetchone()
            if active is not None:
                busy = 'switch_in_progress'
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



@dataclass
class SwitchHooks:
    """Process-side hooks for staged resident prepare and handoff.

    ``prepare_staged(intent, forge_path)`` must spawn an independent staged
    ResidentSession with ``--resume``, run the no-stdin health window, and
    return an opaque staged handle.

    ``take_handoff(staged, result)`` runs under the caller-provided handoff
    lock semantics: swap formal resident holder to staged, bind target owner,
    and return the previous (old) resident handle without closing it.
    The orchestrator closes the old handle only after ``mark_intent_committed``.

    ``discard_staged(staged)`` kills a staged handle after pre-commit failure.
    """

    prepare_staged: Callable[[dict[str, Any], Path], Any]
    take_handoff: Callable[[Any, dict[str, Any]], Any]
    discard_staged: Callable[[Any], None]
    forge_cwd: str
    claude_home: Path


def _payload_hash(
    *,
    chat_id: str,
    source_id: int,
    source_epoch: int,
    count: int,
    close_reason: str,
) -> str:
    raw = json.dumps(
        {
            'chat_id': chat_id,
            'source_context_id': int(source_id),
            'source_context_epoch': int(source_epoch),
            'count': int(count),
            'close_reason': str(close_reason),
        },
        sort_keys=True,
        separators=(',', ':'),
    )
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _now_s(now_dt: datetime.datetime) -> str:
    return now_dt.strftime('%Y-%m-%d %H:%M:%S')


def _intent_row(conn: sqlite3.Connection, request_id: str) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        'SELECT * FROM context_switch_intents WHERE request_id=?',
        (request_id,),
    ).fetchone())


def _parse_selected_ids(raw: Any) -> list[int]:
    if isinstance(raw, list):
        return [int(x) for x in raw]
    data = json.loads(str(raw or '[]'))
    return [int(x) for x in data]


def has_active_switch_intent(
    chat_id: str = DEFAULT_CHAT_ID,
    *,
    db_path: Optional[str] = None,
) -> bool:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT 1 FROM context_switch_intents WHERE chat_id=? AND status IN (%s) LIMIT 1'
            % ','.join('?' for _ in ACTIVE_INTENT_STATUSES),
            (chat_id, *tuple(ACTIVE_INTENT_STATUSES)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_active_switch_intent(
    chat_id: str = DEFAULT_CHAT_ID,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute(
            'SELECT * FROM context_switch_intents WHERE chat_id=? AND status IN (%s) '
            'ORDER BY created_at DESC LIMIT 1'
            % ','.join('?' for _ in ACTIVE_INTENT_STATUSES),
            (chat_id, *tuple(ACTIVE_INTENT_STATUSES)),
        ).fetchone())
    finally:
        conn.close()


def _update_intent_conn(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    status: Optional[str] = None,
    fields: Optional[dict[str, Any]] = None,
    now_s: str,
) -> None:
    sets = ['updated_at=?']
    params: list[Any] = [now_s]
    if status is not None:
        sets.append('status=?')
        params.append(status)
    for key, val in (fields or {}).items():
        sets.append('%s=?' % key)
        params.append(val)
    params.append(request_id)
    conn.execute(
        'UPDATE context_switch_intents SET %s WHERE request_id=?' % ', '.join(sets),
        tuple(params),
    )


def _fail_intent_conn(
    conn: sqlite3.Connection,
    request_id: str,
    *,
    error_code: str,
    orphan_jsonl_state: Optional[str] = None,
    now_s: str,
    release: bool = True,
) -> None:
    status = INTENT_RELEASED if release else INTENT_FAILED
    fields = {'error_code': error_code}
    if orphan_jsonl_state is not None:
        fields['orphan_jsonl_state'] = orphan_jsonl_state
    _update_intent_conn(
        conn, request_id, status=status, fields=fields, now_s=now_s,
    )


def _select_rounds_locked(
    messages: list[Any],
    count: int,
) -> tuple[list[dict[str, Any]], list[int], int]:
    all_rounds = group_carryover_rounds(messages)
    if count == 0:
        selected_rounds: list[dict[str, Any]] = []
    else:
        selected_rounds = all_rounds[-count:] if len(all_rounds) >= count else list(all_rounds)
    selected_ids: list[int] = []
    for rnd in selected_rounds:
        for mid in rnd['message_ids']:
            selected_ids.append(int(mid))
    return selected_rounds, selected_ids, len(selected_rounds)


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
    out = {
        'source_context_id': int(source['id']),
        'source_context_epoch': int(source['context_epoch']),
        'source_resident_generation': int(source.get('resident_generation') or 1),
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
    if target.get('claude_session_id'):
        out['claude_session_id'] = str(target['claude_session_id'])
    return out


def _mark_orphan_best_effort(path: Optional[Path]) -> str:
    if path is None:
        return 'none'
    try:
        if path.is_file():
            path.unlink()
            return 'deleted'
        return 'none'
    except OSError:
        return 'delete_failed'


def offline_switch_hooks(work_root: str | Path) -> SwitchHooks:
    """Test-only hooks: real DB Forge write, no Claude process.

    Must be passed explicitly. ``hooks=None`` never falls back here.
    """
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    cwd = str(root / 'cwd')
    Path(cwd).mkdir(parents=True, exist_ok=True)
    claude_home = root / 'claude_home'
    claude_home.mkdir(parents=True, exist_ok=True)

    class _OfflineStaged:
        def __init__(self, session_id: str, jsonl_path: Path):
            self.session_id = session_id
            self.jsonl_path = jsonl_path
            self.generation = 1
            self.tool_profile = 'text_only'
            self._alive = True

        def is_alive(self) -> bool:
            return self._alive

        def kill(self) -> None:
            self._alive = False

        def _kill(self, quiet: bool = True) -> None:
            self.kill()

    def prepare_staged(intent: dict[str, Any], forge_path: Path) -> Any:
        before = forge_path.read_bytes()
        # Health window simulation: process "alive", JSONL unchanged.
        staged = _OfflineStaged(str(intent['target_session_id']), forge_path)
        after = forge_path.read_bytes()
        if before != after:
            raise SwitchFailedError('staged_jsonl_mutated_before_handoff')
        if not staged.is_alive():
            raise SwitchFailedError('staged_exited_during_health_window')
        return staged

    def take_handoff(staged: Any, result: dict[str, Any]) -> Any:
        # Offline: no process swap; return None old-handle.
        return None

    def discard_staged(staged: Any) -> None:
        if staged is not None and hasattr(staged, 'kill'):
            staged.kill()

    return SwitchHooks(
        prepare_staged=prepare_staged,
        take_handoff=take_handoff,
        discard_staged=discard_staged,
        forge_cwd=cwd,
        claude_home=claude_home,
    )


def _require_switch_hooks(hooks: Optional[SwitchHooks]) -> SwitchHooks:
    if hooks is None:
        raise SwitchHooksRequiredError('switch_hooks_required')
    return hooks


def _close_old_resident_handle(old: Any) -> None:
    if old is None:
        return
    try:
        kill = getattr(old, '_kill', None)
        if callable(kill):
            kill(quiet=True)
            return
        kill2 = getattr(old, 'kill', None)
        if callable(kill2):
            kill2()
    except Exception:
        logger.exception('old resident close after committed failed')


_PENDING_OLD_RESIDENT_CLOSE: Any = None


def defer_old_resident_close(old: Any) -> None:
    """Keep old resident until mark_intent_committed succeeds (incl. retries)."""
    global _PENDING_OLD_RESIDENT_CLOSE
    if old is not None:
        _PENDING_OLD_RESIDENT_CLOSE = old


def flush_old_resident_close() -> None:
    global _PENDING_OLD_RESIDENT_CLOSE
    old = _PENDING_OLD_RESIDENT_CLOSE
    _PENDING_OLD_RESIDENT_CLOSE = None
    _close_old_resident_handle(old)


def clear_pending_old_resident_close_for_tests() -> None:
    global _PENDING_OLD_RESIDENT_CLOSE
    _PENDING_OLD_RESIDENT_CLOSE = None


def _intent_status(db_path: Optional[str], request_id: str) -> Optional[str]:
    conn = _connect(db_path)
    try:
        row = _intent_row(conn, request_id)
        return str(row['status']) if row else None
    finally:
        conn.close()


def reserve_or_load_intent(
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
    """Create reserved intent or return existing same-payload intent."""
    if close_reason not in (CLOSE_REASON_MANUAL, CLOSE_REASON_CAPACITY_RESCUE):
        raise ValueError('invalid close_reason')
    source_id = parse_strict_json_positive_int('source_context_id', source_context_id)
    source_epoch = parse_strict_json_positive_int('source_context_epoch', source_context_epoch)
    count = parse_strict_json_carryover_count(count)
    req_id = _validate_request_id(request_id)
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    ph = _payload_hash(
        chat_id=chat_id,
        source_id=source_id,
        source_epoch=source_epoch,
        count=count,
        close_reason=close_reason,
    )

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = _intent_row(conn, req_id)
        if existing is not None:
            if str(existing.get('payload_hash') or '') != ph:
                conn.rollback()
                raise IdempotencyMismatchError('idempotency_mismatch')
            conn.commit()
            return existing

        # Another active intent for this chat?
        other = conn.execute(
            'SELECT request_id, status FROM context_switch_intents '
            'WHERE chat_id=? AND status IN (%s) LIMIT 1'
            % ','.join('?' for _ in ACTIVE_INTENT_STATUSES),
            (chat_id, *tuple(ACTIVE_INTENT_STATUSES)),
        ).fetchone()
        if other is not None:
            conn.rollback()
            raise SwitchInProgressError('switch_in_progress')

        current = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        if int(current['id']) != source_id or int(current['context_epoch']) != source_epoch:
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')
        if current.get('closed_at'):
            conn.rollback()
            raise StaleSourceContextError('stale_source_context')

        source_version = int(current.get('version') or 1)
        source_gen = int(current.get('resident_generation') or 1)
        if _is_resident_turn_active_conn(conn, source_id, source_gen, now_dt):
            conn.rollback()
            raise WindowBusyError('window_busy')

        messages = _collect_context_formal_messages(
            conn, context_id=source_id, context_epoch=source_epoch,
        )
        boundary_id = _last_formal_message_id(messages)
        _rounds, selected_ids, selected_round_count = _select_rounds_locked(messages, count)

        conn.execute(
            '''INSERT INTO context_switch_intents (
                request_id, chat_id, payload_hash, status,
                source_context_id, source_context_epoch, source_version,
                source_resident_generation, source_boundary_message_id,
                carryover_count, selected_message_ids_json,
                orphan_jsonl_state, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (
                req_id, chat_id, ph, INTENT_RESERVED,
                source_id, source_epoch, source_version,
                source_gen, boundary_id,
                count, json.dumps(selected_ids),
                'none', now_s, now_s,
            ),
        )
        # selected_round_count stored indirectly via carryover_count request;
        # keep locked ids as authority. Expose round count on row via unused field? skip.
        intent = _intent_row(conn, req_id)
        assert intent is not None
        intent['_selected_round_count'] = selected_round_count
        conn.commit()
        return intent
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        # concurrent same request_id insert
        replay = _connect(db_path)
        try:
            replay.execute('BEGIN IMMEDIATE')
            existing = _intent_row(replay, req_id)
            if existing is not None:
                if str(existing.get('payload_hash') or '') != ph:
                    replay.rollback()
                    raise IdempotencyMismatchError('idempotency_mismatch') from exc
                replay.commit()
                return existing
            replay.rollback()
        finally:
            replay.close()
        raise StaleSourceContextError('stale_source_context') from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _set_intent_status(
    request_id: str,
    status: str,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    fields: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ensure_schema(db_path)
    now_s = _now_s(_shanghai_now(now))
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _update_intent_conn(
            conn, request_id, status=status, fields=fields, now_s=now_s,
        )
        row = _intent_row(conn, request_id)
        conn.commit()
        assert row is not None
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def commit_switch_to_handoff_pending(
    intent: dict[str, Any],
    *,
    close_reason: str = CLOSE_REASON_MANUAL,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Final DB transaction ending in handoff_pending (never committed)."""
    ensure_schema(db_path)
    now_dt = _shanghai_now(now)
    now_s = _now_s(now_dt)
    local_day = shanghai_calendar_day(now_dt)
    req_id = str(intent['request_id'])
    chat_id = str(intent['chat_id'])
    source_id = int(intent['source_context_id'])
    source_epoch = int(intent['source_context_epoch'])
    source_version = int(intent['source_version'])
    source_gen = int(intent['source_resident_generation'])
    boundary_id = int(intent['source_boundary_message_id'])
    count = int(intent['carryover_count'])
    selected_ids = _parse_selected_ids(intent.get('selected_message_ids_json'))
    target_session_id = str(intent.get('target_session_id') or '')
    if not target_session_id:
        raise SwitchFailedError('target_session_missing')

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        live = _intent_row(conn, req_id)
        if live is None or str(live.get('status')) not in (INTENT_READY, INTENT_COMMITTING):
            conn.rollback()
            raise SwitchFailedError('intent_not_ready')
        _update_intent_conn(
            conn, req_id, status=INTENT_COMMITTING, now_s=now_s,
        )

        # Peer may have already committed this request_id.
        existing_target = _find_idempotent_target_conn(
            conn, chat_id=chat_id, request_id=req_id,
        )
        if existing_target is not None:
            source_row = conn.execute(
                'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
            ).fetchone()
            if source_row is None:
                conn.rollback()
                raise StaleSourceContextError('stale_source_context')
            _update_intent_conn(
                conn,
                req_id,
                status=INTENT_HANDOFF_PENDING,
                fields={'target_context_id': int(existing_target['id'])},
                now_s=now_s,
            )
            result = _switch_result_from_target_conn(
                conn, source=dict(source_row), target=existing_target,
            )
            conn.commit()
            return result

        current = resolve_canonical_context_row_conn(conn, chat_id=chat_id, now=now_dt)
        if int(current['id']) != source_id or int(current['context_epoch']) != source_epoch:
            existing_target = _find_idempotent_target_conn(
                conn, chat_id=chat_id, request_id=req_id,
            )
            if existing_target is not None:
                source_row = conn.execute(
                    'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
                ).fetchone()
                assert source_row is not None
                _update_intent_conn(
                    conn,
                    req_id,
                    status=INTENT_HANDOFF_PENDING,
                    fields={'target_context_id': int(existing_target['id'])},
                    now_s=now_s,
                )
                result = _switch_result_from_target_conn(
                    conn, source=dict(source_row), target=existing_target,
                )
                conn.commit()
                return result
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')
        if current.get('closed_at'):
            existing_target = _find_idempotent_target_conn(
                conn, chat_id=chat_id, request_id=req_id,
            )
            if existing_target is not None:
                source_row = conn.execute(
                    'SELECT * FROM daily_contexts WHERE id=?', (source_id,),
                ).fetchone()
                assert source_row is not None
                _update_intent_conn(
                    conn,
                    req_id,
                    status=INTENT_HANDOFF_PENDING,
                    fields={'target_context_id': int(existing_target['id'])},
                    now_s=now_s,
                )
                result = _switch_result_from_target_conn(
                    conn, source=dict(source_row), target=existing_target,
                )
                conn.commit()
                return result
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')
        if int(current.get('version') or 1) != source_version:
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')
        if int(current.get('resident_generation') or 1) != source_gen:
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')
        if _is_resident_turn_active_conn(conn, source_id, source_gen, now_dt):
            # retryable: restore ready
            _update_intent_conn(
                conn, req_id, status=INTENT_READY, now_s=now_s,
            )
            conn.commit()
            raise WindowBusyError('window_busy')

        messages = _collect_context_formal_messages(
            conn, context_id=source_id, context_epoch=source_epoch,
        )
        live_boundary = _last_formal_message_id(messages)
        if live_boundary != boundary_id:
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')
        _rounds, live_ids, selected_round_count = _select_rounds_locked(messages, count)
        if live_ids != selected_ids:
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')

        close_cur = conn.execute(
            '''UPDATE daily_contexts SET closed_at=?, close_reason=?, version=version+1,
               updated_at=? WHERE id=? AND context_epoch=? AND version=? AND closed_at IS NULL''',
            (now_s, close_reason, now_s, source_id, source_epoch, source_version),
        )
        if close_cur.rowcount != 1:
            _fail_intent_conn(
                conn, req_id, error_code='stale_source_context', now_s=now_s,
            )
            conn.commit()
            raise StaleSourceContextError('stale_source_context')

        next_epoch = _active_epoch_high_water(conn, chat_id) + 1
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, carryover_requested_count,
                selection_finalized_at, is_backfill, resident_generation, version,
                created_at, updated_at, window_mode, opened_at, closed_at,
                close_reason, source_context_id, switch_request_id, claude_session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                next_epoch, boundary_id, STATUS_PROVISIONAL,
                selected_round_count, count, now_s,
                now_s, now_s, WINDOW_MODE_MANUAL, now_s,
                source_id, req_id, target_session_id,
            ),
        )
        target_id = int(cur.lastrowid)
        ordinal = 0
        for mid in selected_ids:
            conn.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) '
                'VALUES (?,?,?)',
                (target_id, ordinal, int(mid)),
            )
            ordinal += 1

        _update_intent_conn(
            conn,
            req_id,
            status=INTENT_HANDOFF_PENDING,
            fields={'target_context_id': target_id},
            now_s=now_s,
        )
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
    except (StaleSourceContextError, WindowBusyError, SwitchFailedError, IdempotencyMismatchError):
        raise
    except sqlite3.Error:
        conn.rollback()
        # retryable DB error → restore ready
        fix = _connect(db_path)
        try:
            fix.execute('BEGIN IMMEDIATE')
            live = _intent_row(fix, req_id)
            if live is not None and str(live.get('status')) in (
                INTENT_COMMITTING, INTENT_READY,
            ):
                _update_intent_conn(
                    fix, req_id, status=INTENT_READY, now_s=_now_s(_shanghai_now(now)),
                )
            fix.commit()
        finally:
            fix.close()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_intent_committed(
    request_id: str,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    return _set_intent_status(
        request_id, INTENT_COMMITTED, db_path=db_path, now=now,
    )


def complete_handoff_pending_recovery(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    request_id: Optional[str] = None,
    hooks: Optional[SwitchHooks] = None,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> Optional[dict[str, Any]]:
    """Resume handoff_pending without re-Forge."""
    ensure_schema(db_path)
    hooks = _require_switch_hooks(hooks)
    conn = _connect(db_path)
    try:
        if request_id:
            intent = _intent_row(conn, _validate_request_id(request_id))
        else:
            intent = _row_to_dict(conn.execute(
                'SELECT * FROM context_switch_intents WHERE chat_id=? AND status=? '
                'ORDER BY updated_at DESC LIMIT 1',
                (chat_id, INTENT_HANDOFF_PENDING),
            ).fetchone())
    finally:
        conn.close()
    if intent is None or str(intent.get('status')) != INTENT_HANDOFF_PENDING:
        return None
    target_id = int(intent['target_context_id'])
    c2 = _connect(db_path)
    try:
        target = _row_to_dict(c2.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (target_id,),
        ).fetchone())
        source = _row_to_dict(c2.execute(
            'SELECT * FROM daily_contexts WHERE id=?',
            (int(intent['source_context_id']),),
        ).fetchone())
    finally:
        c2.close()
    if target is None or source is None:
        return None
    c3 = _connect(db_path)
    try:
        result = _switch_result_from_target_conn(c3, source=source, target=target)
    finally:
        c3.close()

    from chat import daily_runtime as daily_rt
    from tools.claude_forge_core import session_jsonl_path_for_cwd

    sid = str(target.get('claude_session_id') or intent.get('target_session_id') or '')
    # Already swapped + bound to target: only patch committed, never re-Forge/respawn.
    if daily_rt.target_resident_binding_matches(
        result, session_id=sid, db_path=db_path,
    ):
        mark_intent_committed(str(intent['request_id']), db_path=db_path, now=now)
        flush_old_resident_close()
        return result

    forge_path = session_jsonl_path_for_cwd(
        hooks.forge_cwd, sid, claude_home=hooks.claude_home,
    )
    intent = dict(intent)
    intent['target_session_id'] = sid
    staged = hooks.prepare_staged(intent, forge_path)
    old_handle = None
    handoff_taken = False
    try:
        old_handle = hooks.take_handoff(staged, result)
        handoff_taken = True
        defer_old_resident_close(old_handle)
        mark_intent_committed(str(intent['request_id']), db_path=db_path, now=now)
        flush_old_resident_close()
    except Exception:
        # Post-commit: keep handoff_pending. Do not discard if swap already done.
        if not handoff_taken:
            hooks.discard_staged(staged)
        raise
    return result


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
    hooks: Optional[SwitchHooks] = None,
) -> dict[str, Any]:
    """Seamless Forge switch: reserve → forge → staged ready → commit → handoff → committed."""
    from chat.context_window_forge import (
        CarryoverUnforgeableError as ForgeUnforgeable,
        forge_target_session_from_db,
    )
    from tools.claude_forge_core import session_jsonl_path_for_cwd, sha256_file

    hooks = _require_switch_hooks(hooks)

    # Idempotent committed replay via target row
    ensure_schema(db_path)
    req_id = _validate_request_id(request_id)

    expected_hash = _payload_hash(
        chat_id=chat_id,
        source_id=parse_strict_json_positive_int('source_context_id', source_context_id),
        source_epoch=parse_strict_json_positive_int('source_context_epoch', source_context_epoch),
        count=parse_strict_json_carryover_count(count),
        close_reason=close_reason,
    )
    conn = _connect(db_path)
    try:
        existing_intent = _intent_row(conn, req_id)
        if existing_intent is not None:
            if str(existing_intent.get('payload_hash') or '') != expected_hash:
                raise IdempotencyMismatchError('idempotency_mismatch')
        if existing_intent and str(existing_intent.get('status')) == INTENT_COMMITTED:
            target = _find_idempotent_target_conn(
                conn, chat_id=chat_id, request_id=req_id,
            )
            if target is not None:
                source = _row_to_dict(conn.execute(
                    'SELECT * FROM daily_contexts WHERE id=?',
                    (int(target['source_context_id']),),
                ).fetchone())
                if source is not None:
                    return _switch_result_from_target_conn(
                        conn, source=source, target=target,
                    )
        if existing_intent and str(existing_intent.get('status')) == INTENT_HANDOFF_PENDING:
            conn.close()
            recovered = complete_handoff_pending_recovery(
                chat_id=chat_id,
                request_id=req_id,
                hooks=hooks,
                db_path=db_path,
                now=now,
            )
            if recovered is not None:
                return recovered
            conn = _connect(db_path)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    intent = reserve_or_load_intent(
        source_context_id=source_context_id,
        source_context_epoch=source_context_epoch,
        count=count,
        request_id=req_id,
        chat_id=chat_id,
        close_reason=close_reason,
        db_path=db_path,
        now=now,
    )
    status = str(intent.get('status'))

    if status == INTENT_COMMITTED:
        c = _connect(db_path)
        try:
            target = _find_idempotent_target_conn(
                c, chat_id=chat_id, request_id=req_id,
            )
            source = _row_to_dict(c.execute(
                'SELECT * FROM daily_contexts WHERE id=?',
                (int(intent['source_context_id']),),
            ).fetchone())
            assert target is not None and source is not None
            return _switch_result_from_target_conn(c, source=source, target=target)
        finally:
            c.close()

    if status == INTENT_HANDOFF_PENDING:
        recovered = complete_handoff_pending_recovery(
            chat_id=chat_id,
            request_id=req_id,
            hooks=hooks,
            db_path=db_path,
            now=now,
        )
        if recovered is not None:
            return recovered

    staged = None
    forge_path: Optional[Path] = None
    post_db_commit = False
    handoff_taken = False
    old_handle = None
    try:
        if status in (INTENT_RESERVED, INTENT_FORGING) or (
            status == INTENT_READY and not intent.get('target_session_id')
        ):
            _set_intent_status(req_id, INTENT_FORGING, db_path=db_path, now=now)
            selected_ids = _parse_selected_ids(intent.get('selected_message_ids_json'))
            # Skip re-Forge when target already written for this intent
            if intent.get('target_session_id') and intent.get('target_jsonl_sha256'):
                sid = str(intent['target_session_id'])
                forge_path = session_jsonl_path_for_cwd(
                    hooks.forge_cwd, sid, claude_home=hooks.claude_home,
                )
                if forge_path.is_file() and sha256_file(forge_path) == str(
                    intent['target_jsonl_sha256']
                ):
                    pass  # reuse
                else:
                    intent['target_session_id'] = None

            if not intent.get('target_session_id'):
                c = _connect(db_path)
                try:
                    forged = forge_target_session_from_db(
                        c,
                        selected_message_ids=selected_ids,
                        cwd=hooks.forge_cwd,
                        claude_home=hooks.claude_home,
                    )
                except ForgeUnforgeable as exc:
                    c2 = _connect(db_path)
                    try:
                        c2.execute('BEGIN IMMEDIATE')
                        _fail_intent_conn(
                            c2,
                            req_id,
                            error_code='carryover_message_unforgeable',
                            orphan_jsonl_state='pending',
                            now_s=_now_s(_shanghai_now(now)),
                        )
                        c2.commit()
                    finally:
                        c2.close()
                    raise CarryoverMessageUnforgeableError(str(exc)) from exc
                finally:
                    c.close()
                forge_path = forged.jsonl_path
                intent = _set_intent_status(
                    req_id,
                    INTENT_FORGING,
                    db_path=db_path,
                    now=now,
                    fields={
                        'target_session_id': forged.target_session_id,
                        'target_jsonl_sha256': forged.sha256,
                        'orphan_jsonl_state': 'none',
                    },
                )
            else:
                forge_path = session_jsonl_path_for_cwd(
                    hooks.forge_cwd,
                    str(intent['target_session_id']),
                    claude_home=hooks.claude_home,
                )

            assert forge_path is not None
            before_sha = sha256_file(forge_path)
            staged = hooks.prepare_staged(intent, forge_path)
            after_sha = sha256_file(forge_path)
            if before_sha != after_sha:
                raise SwitchFailedError('staged_jsonl_mutated_before_handoff')
            intent = _set_intent_status(
                req_id,
                INTENT_READY,
                db_path=db_path,
                now=now,
                fields={'staged_ready_at': _now_s(_shanghai_now(now))},
            )
            status = INTENT_READY

        if status == INTENT_READY:
            # Re-prepare staged if this is a retry after ready
            if staged is None:
                forge_path = session_jsonl_path_for_cwd(
                    hooks.forge_cwd,
                    str(intent['target_session_id']),
                    claude_home=hooks.claude_home,
                )
                staged = hooks.prepare_staged(intent, forge_path)

            result = commit_switch_to_handoff_pending(
                intent,
                close_reason=close_reason,
                db_path=db_path,
                now=now,
            )
            post_db_commit = True
            old_handle = hooks.take_handoff(staged, result)
            handoff_taken = True
            defer_old_resident_close(old_handle)
            mark_intent_committed(req_id, db_path=db_path, now=now)
            flush_old_resident_close()
            return result

        if status == INTENT_COMMITTING:
            # resume commit
            result = commit_switch_to_handoff_pending(
                intent,
                close_reason=close_reason,
                db_path=db_path,
                now=now,
            )
            post_db_commit = True
            if staged is None:
                forge_path = session_jsonl_path_for_cwd(
                    hooks.forge_cwd,
                    str(intent['target_session_id']),
                    claude_home=hooks.claude_home,
                )
                staged = hooks.prepare_staged(intent, forge_path)
            old_handle = hooks.take_handoff(staged, result)
            handoff_taken = True
            defer_old_resident_close(old_handle)
            mark_intent_committed(req_id, db_path=db_path, now=now)
            flush_old_resident_close()
            return result

        if status in TERMINAL_FAILURE_STATUSES:
            raise SwitchFailedError(
                str(intent.get('error_code') or status),
            )

        raise SwitchFailedError('unexpected_intent_status:%s' % status)
    except (
        StaleSourceContextError,
        WindowBusyError,
        IdempotencyMismatchError,
        SwitchInProgressError,
        CarryoverMessageUnforgeableError,
        SwitchHooksRequiredError,
    ):
        if not post_db_commit and staged is not None and not handoff_taken:
            hooks.discard_staged(staged)
        raise
    except SwitchFailedError as exc:
        if post_db_commit:
            # Keep handoff_pending; retain JSONL; do not kill promoted staged.
            if staged is not None and not handoff_taken:
                hooks.discard_staged(staged)
            raise
        if staged is not None:
            hooks.discard_staged(staged)
        orphan = _mark_orphan_best_effort(forge_path)
        c = _connect(db_path)
        try:
            c.execute('BEGIN IMMEDIATE')
            live = _intent_row(c, req_id)
            if live is not None and str(live.get('status')) in ACTIVE_INTENT_STATUSES:
                # Never fail/release once DB reached handoff_pending.
                if str(live.get('status')) != INTENT_HANDOFF_PENDING:
                    _fail_intent_conn(
                        c,
                        req_id,
                        error_code=exc.error_code,
                        orphan_jsonl_state='pending' if orphan != 'deleted' else orphan,
                        now_s=_now_s(_shanghai_now(now)),
                    )
            c.commit()
        finally:
            c.close()
        raise
    except Exception as exc:
        if post_db_commit:
            if staged is not None and not handoff_taken:
                hooks.discard_staged(staged)
            raise
        if staged is not None:
            hooks.discard_staged(staged)
        orphan = _mark_orphan_best_effort(forge_path)
        c = _connect(db_path)
        try:
            c.execute('BEGIN IMMEDIATE')
            live = _intent_row(c, req_id)
            if live is not None and str(live.get('status')) in ACTIVE_INTENT_STATUSES:
                if str(live.get('status')) != INTENT_HANDOFF_PENDING:
                    _fail_intent_conn(
                        c,
                        req_id,
                        error_code='switch_internal_error',
                        orphan_jsonl_state='pending' if orphan != 'deleted' else orphan,
                        now_s=_now_s(_shanghai_now(now)),
                    )
            c.commit()
        finally:
            c.close()
        raise SwitchFailedError('switch_internal_error', str(exc)) from exc
