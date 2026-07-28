"""Daily Soft Window — backend boundary, epoch, carryover, and handoff storage.

P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0: schema + pure logic only.
Does not call models, does not generate handoffs, does not enable itself.

R0 scope: single global formal chat table (chat_messages). The chat_id column
on daily_contexts is reserved for future multi-chat mapping; message queries
do not pretend per-tenant isolation yet.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
from typing import Any, Optional, Union

_CURSOR_CAS_OMITTED = object()

from chat.day_handoff import (
    CHAT_DAY_START_HOUR,
    MAX_FILE_BYTES,
    MAX_ITEM_CHARS,
    MAX_LAST_TOPIC_CHARS,
    MAX_LIST_ITEMS,
    TZ_OFFSET_HOURS,
    chat_day_for_timestamp,
    chat_day_window,
    contains_assistant_voice_in_text,
    contains_behavior_instruction_in_text,
    previous_chat_day,
    validate_day_string,
)
from chat.daily_schema import (
    META_SOURCE_KIND_CUTOVER,
    ensure_chat_messages_source_kind,
    ensure_daily_meta_table,
    get_meta_int,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.environ.get('HAYA_DB_PATH', '/opt/frontend/memories.db')
DEFAULT_CHAT_ID = 'default'
DEFAULT_TIMEZONE = 'Asia/Shanghai'

STATUS_ABSENT = 'ABSENT'
STATUS_COMPACTING = 'COMPACTING'
STATUS_PROVISIONAL = 'PROVISIONAL'
STATUS_FINALIZED = 'FINALIZED'
STATUS_FAILED_RETRYABLE = 'FAILED_RETRYABLE'

HANDOFF_ABSENT = 'ABSENT'
HANDOFF_READY = 'READY'
HANDOFF_FAILED_RETRYABLE = 'FAILED_RETRYABLE'

SOURCE_KIND_CHAT = 'chat'
SOURCE_KIND_WAKE = 'wake'
SOURCE_KIND_WORKSPACE_JOB = 'workspace_job'
SOURCE_KIND_SYSTEM = 'system'
SOURCE_KIND_TOOL = 'tool'

FORMAL_SOURCE_KINDS = frozenset({SOURCE_KIND_CHAT, '', None})

ALLOWED_CARRYOVER_COUNTS = frozenset({0, 3, 5, 10})
CARRYOVER_UNIT = 'round'

_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ABSENT: frozenset({STATUS_COMPACTING, STATUS_PROVISIONAL}),
    STATUS_COMPACTING: frozenset({STATUS_PROVISIONAL, STATUS_FAILED_RETRYABLE}),
    STATUS_FAILED_RETRYABLE: frozenset({STATUS_PROVISIONAL, STATUS_COMPACTING}),
    # Handoff compaction axis — independent of carryover selection_finalized_at.
    STATUS_PROVISIONAL: frozenset({STATUS_COMPACTING}),
    STATUS_FINALIZED: frozenset({STATUS_COMPACTING}),
}

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})
_ASSISTANT_AUTHORS = frozenset({'fyodor', 'claude', 'assistant'})
_ELIGIBLE_AUTHORS = _USER_AUTHORS | _ASSISTANT_AUTHORS
_EXCLUDED_AUTHORS = frozenset({'system', 'tool', 'tool_result'})

_SAVE_RE = re.compile(r'\[\[SAVE(?::[^\]]+)?\]\]', re.IGNORECASE)
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

HANDOFF_CONTENT_KEYS = (
    'source_day',
    'source_epoch',
    'boundary_message_id',
    'topics',
    'confirmed_facts',
    'decisions',
    'open_loops',
    'explicit_user_requests',
    'last_topic',
)

_SCHEMA_READY: set[str] = set()


class DailyContextError(Exception):
    """Base error for daily soft window."""


class HotTurnCursorError(DailyContextError):
    """Hot resident turn missing history cursor (fail closed)."""


class ConflictError(DailyContextError):
    """Selection locked or CAS conflict (HTTP 409)."""


class InvalidTransitionError(DailyContextError):
    """Illegal status machine transition."""


class DeferredError(DailyContextError):
    """Provider request in flight; rollover deferred (retryable)."""


class StaleOriginDayError(DailyContextError):
    """Origin local day is older than latest active context; cannot create."""


def enabled() -> bool:
    import config_store
    return config_store.get_bool('DAILY_SOFT_WINDOW_ENABLED', False)


def validate_timezone(tz: str) -> str:
    value = str(tz or '').strip()
    if value != DEFAULT_TIMEZONE:
        raise ValueError('timezone must be Asia/Shanghai (got %r)' % value)
    return value


def _now_local_str() -> str:
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    return now.strftime('%Y-%m-%d %H:%M:%S')


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute('PRAGMA table_info(%s)' % table)}


_MIGRATION_INJECT_FAULT_AT: Optional[str] = None

_RELATED_CONTEXT_ID_TABLES = (
    'daily_carryover_messages',
    'daily_resident_cursors',
    'daily_resident_turn_leases',
    'daily_message_contexts',
    'daily_resident_owners',
)


def _snapshot_related_context_ids(conn: sqlite3.Connection) -> dict[str, set[int]]:
    snap: dict[str, set[int]] = {}
    tables = {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for table in _RELATED_CONTEXT_ID_TABLES:
        if table not in tables:
            snap[table] = set()
            continue
        snap[table] = {
            int(r[0])
            for r in conn.execute('SELECT DISTINCT context_id FROM %s' % table).fetchall()
        }
    return snap


def _verify_related_context_ids(
    conn: sqlite3.Connection,
    before: dict[str, set[int]],
    valid_ids: set[int],
) -> None:
    after = _snapshot_related_context_ids(conn)
    if after != before:
        raise DailyContextError('related context_id mapping changed during migration')
    for table, ids in after.items():
        if ids and not ids.issubset(valid_ids):
            raise DailyContextError(
                '%s references missing context_id after migration' % table
            )


def _read_sqlite_sequence(conn: sqlite3.Connection, table: str) -> Optional[int]:
    try:
        row = conn.execute(
            'SELECT seq FROM sqlite_sequence WHERE name=?', (table,),
        ).fetchone()
        return int(row[0]) if row is not None else None
    except sqlite3.OperationalError:
        return None


def _sync_sqlite_sequence(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute('SELECT MAX(id) AS m FROM %s' % table).fetchone()
    max_id = int(row['m'] or 0) if row is not None else 0
    if max_id <= 0:
        conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
        return
    conn.execute('DELETE FROM sqlite_sequence WHERE name=?', (table,))
    conn.execute(
        'INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)',
        (table, max_id),
    )


def _migration_checkpoint(stage: str) -> None:
    if _MIGRATION_INJECT_FAULT_AT == stage:
        raise DailyContextError('migration fault injection: %s' % stage)


def _create_manual_window_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        'CREATE INDEX IF NOT EXISTS idx_daily_contexts_chat_epoch '
        'ON daily_contexts(chat_id, context_epoch)'
    )
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_contexts_legacy_day_unique '
        "ON daily_contexts(chat_id, local_day) WHERE window_mode='legacy_daily'"
    )
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_contexts_open_manual_unique '
        "ON daily_contexts(chat_id) WHERE window_mode='manual' "
        'AND closed_at IS NULL AND is_backfill=0'
    )
    conn.execute(
        'CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_contexts_switch_idem_unique '
        'ON daily_contexts(chat_id, switch_request_id) '
        'WHERE switch_request_id IS NOT NULL'
    )


def _assert_daily_contexts_autoincrement(conn: sqlite3.Connection, table: str) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not row[0]:
        raise DailyContextError('missing table sql for %s' % table)
    sql = str(row[0]).upper()
    if 'AUTOINCREMENT' not in sql:
        raise DailyContextError(
            '%s must keep INTEGER PRIMARY KEY AUTOINCREMENT' % table
        )


def _drop_migration_temp_table(conn: sqlite3.Connection) -> None:
    """Best-effort cleanup so a failed migrate never leaves a half schema."""
    try:
        conn.execute('DROP TABLE IF EXISTS daily_contexts__mw_new')
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _migrate_manual_window_schema(conn: sqlite3.Connection) -> None:
    """Idempotent rebuild: drop table-level (chat_id, local_day) unique; add manual-window columns.

    Uses a single ``BEGIN IMMEDIATE`` transaction with ``conn.execute`` only —
    never ``executescript`` (which implicitly commits and breaks atomicity).
    """
    dcols = _table_columns(conn, 'daily_contexts')
    if not dcols:
        return
    if 'window_mode' in dcols:
        return

    # Clear any outer implicit transaction so BEGIN IMMEDIATE owns the migrate.
    conn.commit()
    conn.execute('BEGIN IMMEDIATE')
    try:
        old_count = int(conn.execute('SELECT COUNT(*) FROM daily_contexts').fetchone()[0])
        old_ids = {
            int(r[0]) for r in conn.execute('SELECT id FROM daily_contexts').fetchall()
        }
        old_max_id = int(
            conn.execute('SELECT MAX(id) FROM daily_contexts').fetchone()[0] or 0
        )
        old_seq = _read_sqlite_sequence(conn, 'daily_contexts')
        related_before = _snapshot_related_context_ids(conn)

        conn.execute(
            '''
            CREATE TABLE daily_contexts__mw_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                local_day TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                boundary_hour INTEGER NOT NULL DEFAULT 4,
                context_epoch INTEGER NOT NULL,
                boundary_message_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                handoff_id INTEGER NULL,
                carryover_count INTEGER NOT NULL DEFAULT 0,
                selection_finalized_at DATETIME NULL,
                resident_generation INTEGER NOT NULL DEFAULT 1,
                morning_greeting_message_id INTEGER NULL,
                version INTEGER NOT NULL DEFAULT 1,
                lease_owner TEXT NULL,
                lease_expires_at DATETIME NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                is_backfill INTEGER NOT NULL DEFAULT 0,
                carryover_requested_count INTEGER NULL,
                window_mode TEXT NOT NULL DEFAULT 'legacy_daily',
                opened_at DATETIME NOT NULL,
                closed_at DATETIME NULL,
                close_reason TEXT NULL,
                source_context_id INTEGER NULL,
                switch_request_id TEXT NULL
            )
            '''
        )
        _assert_daily_contexts_autoincrement(conn, 'daily_contexts__mw_new')
        _migration_checkpoint('after_create')

        has_backfill = 'is_backfill' in dcols
        has_requested = 'carryover_requested_count' in dcols
        backfill_expr = 'COALESCE(is_backfill, 0)' if has_backfill else '0'
        requested_expr = (
            'carryover_requested_count' if has_requested else 'NULL'
        )
        conn.execute(
            '''
            INSERT INTO daily_contexts__mw_new (
                id, chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, handoff_id, carryover_count,
                selection_finalized_at, resident_generation, morning_greeting_message_id,
                version, lease_owner, lease_expires_at, created_at, updated_at,
                is_backfill, carryover_requested_count,
                window_mode, opened_at, closed_at, close_reason,
                source_context_id, switch_request_id
            )
            SELECT
                id, chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, handoff_id, carryover_count,
                selection_finalized_at, resident_generation, morning_greeting_message_id,
                version, lease_owner, lease_expires_at, created_at, updated_at,
                %s, %s,
                'legacy_daily',
                COALESCE(created_at, updated_at, datetime('now', '+8 hours')),
                NULL, NULL, NULL, NULL
            FROM daily_contexts
            ''' % (backfill_expr, requested_expr)
        )
        _migration_checkpoint('after_copy')

        new_count = int(conn.execute('SELECT COUNT(*) FROM daily_contexts__mw_new').fetchone()[0])
        new_ids = {
            int(r[0]) for r in conn.execute('SELECT id FROM daily_contexts__mw_new').fetchall()
        }
        new_max_id = int(
            conn.execute('SELECT MAX(id) FROM daily_contexts__mw_new').fetchone()[0] or 0
        )
        if new_count != old_count or new_ids != old_ids or new_max_id != old_max_id:
            raise DailyContextError(
                'manual window migration row/id mismatch: %s vs %s'
                % (old_count, new_count)
            )
        _verify_related_context_ids(conn, related_before, new_ids)

        _migration_checkpoint('before_drop')
        conn.execute('DROP TABLE daily_contexts')
        conn.execute('ALTER TABLE daily_contexts__mw_new RENAME TO daily_contexts')
        _migration_checkpoint('after_rename')

        _sync_sqlite_sequence(conn, 'daily_contexts')
        _create_manual_window_indexes(conn)
        _migration_checkpoint('during_index')

        final_ids = {
            int(r[0]) for r in conn.execute('SELECT id FROM daily_contexts').fetchall()
        }
        if final_ids != old_ids:
            raise DailyContextError('context id set changed after migration finalize')
        _verify_related_context_ids(conn, related_before, final_ids)
        _assert_daily_contexts_autoincrement(conn, 'daily_contexts')

        new_seq = _read_sqlite_sequence(conn, 'daily_contexts')
        if old_seq is not None and new_seq is not None and new_seq < old_seq:
            raise DailyContextError('sqlite_sequence regressed during migration')
        if old_max_id > 0 and (new_seq is None or new_seq < old_max_id):
            raise DailyContextError('sqlite_sequence below MAX(id) after migration')

        conn.commit()
    except Exception:
        conn.rollback()
        _drop_migration_temp_table(conn)
        raise


def _ensure_manual_window_indexes(conn: sqlite3.Connection) -> None:
    dcols = _table_columns(conn, 'daily_contexts')
    if not dcols or 'window_mode' not in dcols:
        return
    _create_manual_window_indexes(conn)


def ensure_schema(db_path: Optional[str] = None) -> None:
    path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    if path in _SCHEMA_READY and os.path.isfile(path):
        return
    conn = _connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_contexts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                local_day TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                boundary_hour INTEGER NOT NULL DEFAULT 4,
                context_epoch INTEGER NOT NULL,
                boundary_message_id INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                handoff_id INTEGER NULL,
                carryover_count INTEGER NOT NULL DEFAULT 0,
                selection_finalized_at DATETIME NULL,
                resident_generation INTEGER NOT NULL DEFAULT 1,
                morning_greeting_message_id INTEGER NULL,
                version INTEGER NOT NULL DEFAULT 1,
                lease_owner TEXT NULL,
                lease_expires_at DATETIME NULL,
                created_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                is_backfill INTEGER NOT NULL DEFAULT 0,
                carryover_requested_count INTEGER NULL,
                window_mode TEXT NOT NULL DEFAULT 'legacy_daily',
                opened_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                closed_at DATETIME NULL,
                close_reason TEXT NULL,
                source_context_id INTEGER NULL,
                switch_request_id TEXT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_carryover_messages (
                context_id INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY(context_id, ordinal),
                UNIQUE(context_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS day_handoffs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                source_day TEXT NOT NULL,
                source_epoch INTEGER NULL,
                boundary_message_id INTEGER NOT NULL,
                source_first_message_id INTEGER NOT NULL,
                source_last_message_id INTEGER NOT NULL,
                source_message_count INTEGER NOT NULL,
                source_sha256 TEXT NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                content_json TEXT NULL,
                error_code TEXT NULL,
                created_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                updated_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(chat_id, source_day, source_sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_day_handoffs_chat_day
                ON day_handoffs(chat_id, source_day);

            CREATE TABLE IF NOT EXISTS daily_soft_window_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS daily_resident_cursors (
                context_id INTEGER NOT NULL,
                resident_generation INTEGER NOT NULL,
                history_cursor_message_id INTEGER NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (context_id, resident_generation)
            );

            CREATE TABLE IF NOT EXISTS daily_resident_turn_leases (
                context_id INTEGER NOT NULL,
                resident_generation INTEGER NOT NULL,
                lease_owner TEXT NOT NULL,
                request_message_id INTEGER NOT NULL,
                acquired_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (context_id, resident_generation)
            );

            CREATE TABLE IF NOT EXISTS daily_message_contexts (
                message_id INTEGER PRIMARY KEY,
                context_id INTEGER NOT NULL,
                context_epoch INTEGER NOT NULL,
                resident_generation INTEGER NOT NULL,
                role TEXT NOT NULL,
                created_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE INDEX IF NOT EXISTS idx_daily_message_contexts_ctx
                ON daily_message_contexts(context_id, context_epoch);

            CREATE TABLE IF NOT EXISTS daily_resident_owners (
                context_id INTEGER NOT NULL,
                resident_generation INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                resident_key TEXT NOT NULL,
                bound_cursor_message_id INTEGER NULL,
                process_generation INTEGER NULL,
                updated_at DATETIME NOT NULL DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (context_id, resident_generation)
            );
            """
        )
        ensure_daily_meta_table(conn)
        dcols = _table_columns(conn, 'daily_contexts')
        if dcols and 'is_backfill' not in dcols:
            conn.execute(
                'ALTER TABLE daily_contexts ADD COLUMN is_backfill INTEGER NOT NULL DEFAULT 0'
            )
        if dcols and 'carryover_requested_count' not in dcols:
            conn.execute(
                'ALTER TABLE daily_contexts ADD COLUMN carryover_requested_count INTEGER NULL'
            )
        _migrate_manual_window_schema(conn)
        _ensure_manual_window_indexes(conn)
        if _table_columns(conn, 'chat_messages'):
            ensure_chat_messages_source_kind(conn, record_cutover=True)
        conn.commit()
        _SCHEMA_READY.add(path)
    finally:
        conn.close()


def ensure_schema_logged(db_path: Optional[str] = None) -> None:
    try:
        ensure_schema(db_path)
    except Exception:
        logger.exception('daily_context ensure_schema failed for %s', db_path or DEFAULT_DB_PATH)
        raise


def assert_transition(from_status: str, to_status: str) -> None:
    allowed = _TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        raise InvalidTransitionError(
            'illegal transition %s → %s' % (from_status, to_status)
        )


def get_boundary_message_id(
    conn: sqlite3.Connection,
    *,
    local_day: str,
    chat_id: str = DEFAULT_CHAT_ID,
) -> int:
    """Last formal chat_messages.id before local_day 04:00; 0 if none."""
    _ = chat_id
    _day, start_at, _end, _next = chat_day_window(local_day)
    cols = _table_columns(conn, 'chat_messages')
    if not cols:
        return 0
    select_cols = ['id', 'author', 'content', 'created_at']
    for optional in ('tool_calls', 'source_kind', 'image_url'):
        if optional in cols:
            select_cols.append(optional)
    rows = conn.execute(
        'SELECT %s FROM chat_messages WHERE created_at < ? ORDER BY id DESC'
        % ', '.join(select_cols),
        (start_at,),
    ).fetchall()
    wake_contents = _wake_content_set(conn)
    cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)
    for row in rows:
        if is_formal_chat_message(
            row, wake_contents=wake_contents, cutover_id=cutover,
        ):
            return int(row['id'])
    return 0


def _max_epoch(conn: sqlite3.Connection, chat_id: str) -> int:
    row = conn.execute(
        'SELECT MAX(context_epoch) AS m FROM daily_contexts WHERE chat_id=?',
        (chat_id,),
    ).fetchone()
    return int(row['m'] or 0) if row else 0


def _active_epoch_high_water(conn: sqlite3.Connection, chat_id: str) -> int:
    row = conn.execute(
        'SELECT MAX(context_epoch) AS m FROM daily_contexts '
        'WHERE chat_id=? AND is_backfill=0',
        (chat_id,),
    ).fetchone()
    return int(row['m'] or 0) if row else 0


def _current_chat_day(now: Optional[datetime.datetime] = None) -> str:
    base = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
    return chat_day_for_timestamp(base)


def validate_local_day_for_create(
    local_day: str,
    *,
    now: Optional[datetime.datetime] = None,
    allow_backfill: bool = False,
) -> str:
    local_day = validate_day_string(local_day)
    current = _current_chat_day(now)
    if local_day > current:
        raise ValueError('future local_day not allowed')
    if local_day < current and not allow_backfill:
        raise ValueError('historical local_day requires allow_backfill=True')
    return local_day


def _assign_context_epoch(
    conn: sqlite3.Connection,
    chat_id: str,
    local_day: str,
    *,
    current_chat_day: str,
) -> tuple[int, int]:
    """Return (context_epoch, is_backfill). Active days bump epoch; backfill does not."""
    active_hwm = _active_epoch_high_water(conn, chat_id)
    if local_day >= current_chat_day:
        return max(_max_epoch(conn, chat_id), active_hwm) + 1, 0
    # Historical backfill — must not receive the latest active epoch.
    row = conn.execute(
        'SELECT MAX(context_epoch) AS m FROM daily_contexts '
        'WHERE chat_id=? AND local_day<?',
        (chat_id, local_day),
    ).fetchone()
    prior = int(row['m'] or 0) if row else 0
    if active_hwm > 0:
        epoch = min(prior + 1 if prior else 1, active_hwm - 1) if active_hwm > 1 else 1
        if epoch >= active_hwm:
            epoch = active_hwm - 1 if active_hwm > 1 else 1
    else:
        epoch = prior + 1 if prior else 1
    return epoch, 1


def _wake_content_set(conn: sqlite3.Connection) -> frozenset[str]:
    tables = {
        str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if 'wake_log' not in tables:
        return frozenset()
    rows = conn.execute(
        'SELECT content FROM wake_log WHERE content IS NOT NULL AND content != ""'
    ).fetchall()
    return frozenset(str(r[0] or '') for r in rows)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def get_daily_context(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    local_day: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE chat_id=? AND local_day=?',
        (chat_id, local_day),
    ).fetchone())


def _latest_active_context_row(
    conn: sqlite3.Connection,
    chat_id: str,
) -> Optional[dict[str, Any]]:
    return _row_to_dict(conn.execute(
        'SELECT * FROM daily_contexts WHERE chat_id=? AND is_backfill=0 '
        'ORDER BY context_epoch DESC LIMIT 1',
        (str(chat_id),),
    ).fetchone())


def _assign_origin_closing_epoch(
    conn: sqlite3.Connection,
    chat_id: str,
) -> int:
    """Assign next active epoch for a late origin-day turn (never backfill)."""
    active_hwm = _active_epoch_high_water(conn, chat_id)
    max_epoch = _max_epoch(conn, chat_id)
    return max(active_hwm, max_epoch) + 1


def _origin_day_is_stale(
    *,
    origin_local_day: str,
    latest: Optional[dict[str, Any]],
    newest_local_day: Optional[str],
) -> bool:
    if latest is not None and str(latest['local_day']) > origin_local_day:
        return True
    if newest_local_day and newest_local_day > origin_local_day:
        return True
    return False


def _rollover_lease_blocks_origin_conn(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    origin_local_day: str,
    now_dt: datetime.datetime,
) -> None:
    """Defer cross-day rollover while another context day holds an active turn lease."""
    rows = conn.execute(
        '''SELECT c.local_day, l.expires_at FROM daily_resident_turn_leases l
           INNER JOIN daily_contexts c ON c.id = l.context_id
           WHERE c.chat_id=? AND c.is_backfill=0''',
        (str(chat_id),),
    ).fetchall()
    for row in rows:
        try:
            exp_dt = _parse_local_dt(str(row['expires_at']))
        except ValueError:
            continue
        if exp_dt <= now_dt:
            continue
        if str(row['local_day']) != origin_local_day:
            raise DeferredError('provider request in flight; rollover deferred')


def resolve_or_create_daily_context_for_origin(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    origin_local_day: str,
    actual_wall_now: Optional[datetime.datetime] = None,
    db_path: Optional[str] = None,
    provider_busy: bool = False,
    skip_compaction: bool = True,
) -> dict[str, Any]:
    """Resolve origin-day context without treating message time as current wall clock."""
    ensure_schema(db_path)
    origin_local_day = validate_day_string(origin_local_day)
    validate_timezone(DEFAULT_TIMEZONE)
    wall_now = actual_wall_now or (
        datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    )

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = get_daily_context(conn, chat_id=chat_id, local_day=origin_local_day)
        latest = _latest_active_context_row(conn, chat_id)
        _rollover_lease_blocks_origin_conn(
            conn,
            chat_id=chat_id,
            origin_local_day=origin_local_day,
            now_dt=wall_now,
        )
        latest_day_row = conn.execute(
            'SELECT local_day FROM daily_contexts WHERE chat_id=? ORDER BY local_day DESC LIMIT 1',
            (str(chat_id),),
        ).fetchone()
        newest_local_day = str(latest_day_row['local_day']) if latest_day_row else None
        if _origin_day_is_stale(
            origin_local_day=origin_local_day,
            latest=latest,
            newest_local_day=newest_local_day,
        ):
            conn.rollback()
            stale_ref = (
                str(latest['local_day']) if latest is not None
                else newest_local_day
            )
            raise StaleOriginDayError(
                'origin day %s stale; latest context day is %s'
                % (origin_local_day, stale_ref),
            )
        if existing is not None:
            conn.commit()
            return existing
        boundary_id = get_boundary_message_id(
            conn, local_day=origin_local_day, chat_id=chat_id,
        )
        epoch = _assign_origin_closing_epoch(conn, chat_id)
        is_backfill = 0
        now_s = wall_now.strftime('%Y-%m-%d %H:%M:%S')
        status = STATUS_ABSENT
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, is_backfill,
                resident_generation, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, 1, ?, ?)''',
            (
                chat_id, origin_local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                epoch, boundary_id, status, is_backfill, now_s, now_s,
            ),
        )
        context_id = int(cur.lastrowid)
        if skip_compaction:
            assert_transition(STATUS_ABSENT, STATUS_PROVISIONAL)
            conn.execute(
                '''UPDATE daily_contexts SET status=?, version=version+1,
                   updated_at=datetime('now','+8 hours') WHERE id=?''',
                (STATUS_PROVISIONAL, context_id),
            )
        conn.commit()
        created = get_daily_context(conn, chat_id=chat_id, local_day=origin_local_day)
        assert created is not None
        return created
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = get_daily_context(conn, chat_id=chat_id, local_day=origin_local_day)
        if existing is None:
            raise
        conn2 = _connect(db_path)
        try:
            conn2.execute('BEGIN IMMEDIATE')
            latest = _latest_active_context_row(conn2, chat_id)
            latest_day_row = conn2.execute(
                'SELECT local_day FROM daily_contexts WHERE chat_id=? ORDER BY local_day DESC LIMIT 1',
                (str(chat_id),),
            ).fetchone()
            newest_local_day = str(latest_day_row['local_day']) if latest_day_row else None
            _rollover_lease_blocks_origin_conn(
                conn2,
                chat_id=chat_id,
                origin_local_day=origin_local_day,
                now_dt=wall_now,
            )
            if _origin_day_is_stale(
                origin_local_day=origin_local_day,
                latest=latest,
                newest_local_day=newest_local_day,
            ):
                conn2.rollback()
                raise StaleOriginDayError(
                    'origin day %s stale after concurrent create' % origin_local_day,
                )
            conn2.commit()
        finally:
            conn2.close()
        return existing
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_current_daily_context_for_api(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Fenced current-day resolver for HTTP routes.

    Blocks new-day context creation while another chat-day holds an active
    provider turn lease (cross-midnight rollover fence).
    """
    wall_now = now or (
        datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)
    )
    origin_day = _current_chat_day(wall_now)
    return resolve_or_create_daily_context_for_origin(
        chat_id=chat_id,
        origin_local_day=origin_day,
        actual_wall_now=wall_now,
        db_path=db_path,
    )


def get_or_create_daily_context(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    local_day: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    db_path: Optional[str] = None,
    provider_busy: bool = False,
    skip_compaction: bool = True,
    allow_backfill: bool = False,
) -> dict[str, Any]:
    """Idempotent create for (chat_id, local_day). Uses BEGIN IMMEDIATE.

    New rows are inserted as ABSENT. When skip_compaction=True (R0 default),
    they immediately transition ABSENT → PROVISIONAL in the same transaction
    so chat can open without a handoff generator. Pass skip_compaction=False
    to leave ABSENT for acquire_compaction_lease().
    """
    if provider_busy:
        raise DeferredError('provider request in flight; rollover deferred')

    ensure_schema(db_path)
    current_chat_day = _current_chat_day(now)
    if local_day is None:
        local_day = current_chat_day
    else:
        local_day = validate_local_day_for_create(
            local_day, now=now, allow_backfill=allow_backfill,
        )
    validate_timezone(DEFAULT_TIMEZONE)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = get_daily_context(conn, chat_id=chat_id, local_day=local_day)
        if existing is not None:
            conn.commit()
            return existing

        boundary_id = get_boundary_message_id(conn, local_day=local_day, chat_id=chat_id)
        epoch, is_backfill = _assign_context_epoch(
            conn, chat_id, local_day, current_chat_day=current_chat_day,
        )
        now_s = _now_local_str()
        status = STATUS_ABSENT
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count, is_backfill,
                resident_generation, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, 1, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                epoch, boundary_id, status, is_backfill, now_s, now_s,
            ),
        )
        context_id = int(cur.lastrowid)
        if skip_compaction:
            assert_transition(STATUS_ABSENT, STATUS_PROVISIONAL)
            conn.execute(
                '''UPDATE daily_contexts SET status=?, version=version+1,
                   updated_at=datetime('now','+8 hours') WHERE id=?''',
                (STATUS_PROVISIONAL, context_id),
            )
        conn.commit()
        created = get_daily_context(conn, chat_id=chat_id, local_day=local_day)
        assert created is not None
        return created
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = get_daily_context(conn, chat_id=chat_id, local_day=local_day)
        if existing is None:
            raise
        return existing
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def transition_status(
    context_id: int,
    to_status: str,
    *,
    expected_version: Optional[int] = None,
    db_path: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found: %s' % context_id)
        current = dict(row)
        if expected_version is not None and int(current['version']) != int(expected_version):
            raise ConflictError('version mismatch')
        assert_transition(current['status'], to_status)
        sets = ['status=?', 'version=version+1', "updated_at=datetime('now','+8 hours')"]
        params: list[Any] = [to_status]
        if extra:
            for key, value in extra.items():
                if key in (
                    'handoff_id', 'carryover_count', 'selection_finalized_at',
                    'resident_generation', 'morning_greeting_message_id',
                    'lease_owner', 'lease_expires_at',
                ):
                    sets.append('%s=?' % key)
                    params.append(value)
        params.append(context_id)
        conn.execute(
            'UPDATE daily_contexts SET %s WHERE id=?' % ', '.join(sets),
            params,
        )
        conn.commit()
        return get_daily_context_by_id(context_id, db_path=db_path) or current
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_daily_context_by_id(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone())
    finally:
        conn.close()


def acquire_compaction_lease(
    context_id: int,
    owner: str,
    *,
    ttl_seconds: int = 120,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        current = dict(row)
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS)
        expires = current.get('lease_expires_at')
        if (
            current['status'] == STATUS_COMPACTING
            and current.get('lease_owner')
            and expires
        ):
            try:
                exp_dt = datetime.datetime.strptime(str(expires), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                exp_dt = now
            if exp_dt > now and current['lease_owner'] != owner:
                raise ConflictError('lease held by %s' % current['lease_owner'])

        if current['status'] == STATUS_COMPACTING:
            pass  # reclaim expired / same owner
        else:
            assert_transition(current['status'], STATUS_COMPACTING)

        exp_s = (now + datetime.timedelta(seconds=ttl_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            '''UPDATE daily_contexts SET status=?, lease_owner=?, lease_expires_at=?,
               version=version+1, updated_at=datetime('now','+8 hours') WHERE id=?''',
            (STATUS_COMPACTING, owner, exp_s, context_id),
        )
        conn.commit()
        return get_daily_context_by_id(context_id, db_path=db_path) or {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_compaction_to_provisional(
    context_id: int,
    *,
    handoff_id: Optional[int] = None,
    failed: bool = False,
    error_code: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    _ = error_code
    if failed:
        return transition_status(
            context_id, STATUS_FAILED_RETRYABLE, db_path=db_path,
            extra={'lease_owner': None, 'lease_expires_at': None},
        )
    return transition_status(
        context_id, STATUS_PROVISIONAL, db_path=db_path,
        extra={
            'handoff_id': handoff_id,
            'lease_owner': None,
            'lease_expires_at': None,
        },
    )


def _row_source_kind(row: Any) -> str:
    if hasattr(row, 'keys') and 'source_kind' in row.keys():
        return str(row['source_kind'] or SOURCE_KIND_CHAT).strip().lower()
    return SOURCE_KIND_CHAT


def _is_legacy_workspace_job(row: Any) -> bool:
    """Detect pre-source_kind workspace completion rows (not formal ws_job tool use).

    Old workspace hook rows carry a top-level ``job`` dict and
    ``args.action == 'status'``. Formal chat tool_calls_acc entries use ws_job
    for start/status/etc. but never include that completion-only ``job`` field.
    """
    if not (hasattr(row, 'keys') and 'tool_calls' in row.keys()):
        return False
    tc = row['tool_calls']
    if tc is None:
        return False
    s = str(tc).strip()
    if s in ('', 'null', 'None', '[]', '{}'):
        return False
    try:
        parsed = json.loads(tc) if isinstance(tc, str) else tc
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(parsed, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get('name') == 'ws_job'
        and isinstance(item.get('job'), dict)
        and (item.get('args') or {}).get('action') == 'status'
        for item in parsed
    )


def is_formal_chat_message(
    row: Any,
    *,
    wake_contents: Optional[frozenset[str]] = None,
    cutover_id: Optional[int] = None,
) -> bool:
    """Authoritative formal-message predicate for carryover and current-day history."""
    author = str(row['author'] or '').strip().lower()
    if author in _EXCLUDED_AUTHORS:
        return False
    if author not in _ELIGIBLE_AUTHORS:
        return False

    kind = _row_source_kind(row)
    if kind not in ('', SOURCE_KIND_CHAT):
        return False

    if _is_legacy_workspace_job(row):
        return False

    mid = int(row['id']) if hasattr(row, 'keys') and 'id' in row.keys() else 0
    if (
        cutover_id is not None
        and mid > 0
        and mid <= int(cutover_id)
        and wake_contents is not None
        and str(row['content'] or '') in wake_contents
        and author in _ASSISTANT_AUTHORS
    ):
        return False

    content = str(row['content'] or '')
    image_url = ''
    if hasattr(row, 'keys') and 'image_url' in row.keys():
        image_url = str(row['image_url'] or '').strip()
    if not content.strip() and not image_url:
        return False
    if _SAVE_RE.search(content):
        return False
    return True


def _message_display_content(row: Any) -> str:
    content = str(row['content'] or '').strip()
    if content:
        return content
    image_url = ''
    if hasattr(row, 'keys') and 'image_url' in row.keys():
        image_url = str(row['image_url'] or '').strip()
    if image_url:
        return '[image]'
    return ''


def _carryover_message_preview(row: Any) -> dict[str, Any]:
    content = _message_display_content(row)
    preview = content if len(content) <= 160 else content[:157] + '…'
    author = str(row['author'] or '')
    role = 'user' if author.lower() in _USER_AUTHORS else 'assistant'
    return {
        'message_id': int(row['id']),
        'role': role,
        'author': author,
        'content_preview': preview,
        'created_at': str(row['created_at'] or ''),
    }


def group_carryover_rounds(messages: list[Any]) -> list[dict[str, Any]]:
    """Group eligible formal messages into conversation rounds (sorted by message_id)."""
    rounds: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for row in messages:
        preview = _carryover_message_preview(row)
        if preview['role'] == 'user':
            if current is not None:
                rounds.append(current)
            current = {
                'round_id': int(preview['message_id']),
                'message_ids': [int(preview['message_id'])],
                'messages': [preview],
            }
        elif preview['role'] == 'assistant' and current is not None:
            current['message_ids'].append(int(preview['message_id']))
            current['messages'].append(preview)
    if current is not None:
        rounds.append(current)
    return rounds


def _collect_prev_day_eligible_messages(
    ctx: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> list[Any]:
    local_day = ctx['local_day']
    chat_id = str(ctx.get('chat_id') or DEFAULT_CHAT_ID)
    day_dt = datetime.datetime.strptime(local_day, '%Y-%m-%d')
    prev_day = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    _d, start_at, _end, next_start = chat_day_window(prev_day)
    boundary = int(ctx['boundary_message_id'] or 0)
    source_ctx = get_context_for_local_day(chat_id, prev_day, db_path=db_path)

    conn = _connect(db_path)
    try:
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        wake_contents = _wake_content_set(conn)
        cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)
        rows_by_id: dict[int, Any] = {}

        if source_ctx is not None:
            src_id = int(source_ctx['id'])
            src_epoch = int(source_ctx['context_epoch'])
            for r in conn.execute(
                'SELECT %s FROM chat_messages m '
                'INNER JOIN daily_message_contexts dmc ON dmc.message_id = m.id '
                'WHERE dmc.context_id=? AND dmc.context_epoch=? '
                'ORDER BY m.id ASC' % ', '.join('m.' + c for c in select_cols),
                (src_id, src_epoch),
            ).fetchall():
                rows_by_id[int(r['id'])] = r
            other_mapped = {
                int(r[0]) for r in conn.execute(
                    'SELECT message_id FROM daily_message_contexts WHERE context_id != ?',
                    (src_id,),
                ).fetchall()
            }
        else:
            other_mapped = {
                int(r[0]) for r in conn.execute(
                    'SELECT message_id FROM daily_message_contexts',
                ).fetchall()
            }

        for r in conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id <= ? '
            'ORDER BY id ASC' % ', '.join(select_cols),
            (start_at, next_start, boundary if boundary > 0 else 10**18),
        ).fetchall():
            mid = int(r['id'])
            if mid in rows_by_id or mid in other_mapped:
                continue
            rows_by_id[mid] = r

        return [
            r for r in (rows_by_id[k] for k in sorted(rows_by_id.keys()))
            if is_formal_chat_message(
                r, wake_contents=wake_contents, cutover_id=cutover,
            )
        ]
    finally:
        conn.close()


def list_carryover_rounds(
    context_id: int,
    *,
    limit_rounds: int = 10,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    all_rounds = group_carryover_rounds(
        _collect_prev_day_eligible_messages(ctx, db_path=db_path),
    )
    available_round_count = len(all_rounds)
    rounds = all_rounds[-limit_rounds:] if limit_rounds else list(all_rounds)
    return {
        'carryover_unit': CARRYOVER_UNIT,
        'available_round_count': available_round_count,
        'rounds': rounds,
    }


def list_carryover_candidates(
    context_id: int,
    *,
    limit: int = 10,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Flattened carryover previews — last ``limit`` rounds, in round order."""
    data = list_carryover_rounds(context_id, limit_rounds=limit, db_path=db_path)
    out: list[dict[str, Any]] = []
    for rnd in data['rounds']:
        out.extend(rnd['messages'])
    return out


def _first_user_message_id_for_day(
    conn: sqlite3.Connection,
    *,
    local_day: str,
) -> Optional[int]:
    _d, start_at, _end, next_start = chat_day_window(local_day)
    cols = _table_columns(conn, 'chat_messages')
    select_cols = ['id', 'author', 'content', 'created_at']
    for optional in ('tool_calls', 'source_kind', 'image_url'):
        if optional in cols:
            select_cols.append(optional)
    # Authors only — do not LIMIT; must find the true first formal user row.
    rows = conn.execute(
        'SELECT %s FROM chat_messages '
        'WHERE created_at >= ? AND created_at < ? ORDER BY id ASC'
        % ', '.join(select_cols),
        (start_at, next_start),
    ).fetchall()
    wake_contents = _wake_content_set(conn)
    cutover = get_meta_int(conn, META_SOURCE_KIND_CUTOVER)
    for r in rows:
        if str(r['author'] or '').lower() not in _USER_AUTHORS:
            continue
        if not is_formal_chat_message(
            r, wake_contents=wake_contents, cutover_id=cutover,
        ):
            continue
        return int(r['id'])
    return None


def _selection_locked_conn(conn: sqlite3.Connection, ctx: dict[str, Any]) -> bool:
    return bool(ctx.get('selection_finalized_at'))


def _selection_locked(ctx: dict[str, Any], db_path: Optional[str] = None) -> bool:
    conn = _connect(db_path)
    try:
        return _selection_locked_conn(conn, ctx)
    finally:
        conn.close()


def _carryover_selection_result(
    *,
    context_id: int,
    current: dict[str, Any],
    selected_ids: list[int],
    selected_round_count: int,
    requested_round_count: int,
    finalized_at: Any,
) -> dict[str, Any]:
    return {
        'context_id': context_id,
        'context_epoch': int(current['context_epoch']),
        'carryover_unit': CARRYOVER_UNIT,
        'requested_round_count': int(requested_round_count),
        'selected_message_ids': selected_ids,
        'carryover_count': int(selected_round_count),
        'selected_round_count': int(selected_round_count),
        'selected_message_count': len(selected_ids),
        'finalized_at': finalized_at,
    }


def _stored_carryover_requested_count(current: dict[str, Any]) -> int:
    raw = current.get('carryover_requested_count')
    if raw is not None:
        return int(raw)
    if current.get('selection_finalized_at'):
        return int(current.get('carryover_count') or 0)
    return -1


def select_carryover(
    context_id: int,
    count: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    if count not in ALLOWED_CARRYOVER_COUNTS:
        raise ValueError('count must be one of %s' % sorted(ALLOWED_CARRYOVER_COUNTS))

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        current = dict(row)
        if current.get('selection_finalized_at'):
            prev_requested = _stored_carryover_requested_count(current)
            prev_selected = int(current.get('carryover_count') or 0)
            if prev_requested == 0 and count > 0:
                raise ConflictError('carryover locked at 0; cannot select %d' % count)
            if prev_requested == count:
                selected_ids = [
                    int(r['message_id']) for r in conn.execute(
                        'SELECT message_id FROM daily_carryover_messages '
                        'WHERE context_id=? ORDER BY ordinal ASC',
                        (context_id,),
                    ).fetchall()
                ]
                conn.commit()
                return _carryover_selection_result(
                    context_id=context_id,
                    current=current,
                    selected_ids=selected_ids,
                    selected_round_count=prev_selected,
                    requested_round_count=prev_requested,
                    finalized_at=current.get('selection_finalized_at'),
                )
            raise ConflictError('carryover selection is locked')
    finally:
        conn.close()

    round_data = list_carryover_rounds(context_id, limit_rounds=10, db_path=db_path)
    all_rounds = round_data['rounds']
    if count == 0:
        selected_rounds: list[dict[str, Any]] = []
    else:
        selected_rounds = (
            all_rounds[-count:] if len(all_rounds) >= count else list(all_rounds)
        )

    now_s = _now_local_str()
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        current = dict(row)
        if _selection_locked_conn(conn, current):
            raise ConflictError('carryover selection is locked')
        if count > 0 and _first_user_message_id_for_day(conn, local_day=current['local_day']):
            raise ConflictError(
                'carryover selection closed; formal user message already exists',
            )

        conn.execute('DELETE FROM daily_carryover_messages WHERE context_id=?', (context_id,))
        ids: list[int] = []
        ordinal = 0
        for rnd in selected_rounds:
            for mid in rnd['message_ids']:
                conn.execute(
                    'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) VALUES (?,?,?)',
                    (context_id, ordinal, int(mid)),
                )
                ids.append(int(mid))
                ordinal += 1
        round_count = len(selected_rounds)
        conn.execute(
            '''UPDATE daily_contexts SET carryover_count=?,
               carryover_requested_count=?, selection_finalized_at=?,
               version=version+1,
               updated_at=datetime('now','+8 hours') WHERE id=?''',
            (round_count, count, now_s, context_id),
        )
        conn.commit()
        return _carryover_selection_result(
            context_id=context_id,
            current=current,
            selected_ids=ids,
            selected_round_count=round_count,
            requested_round_count=count,
            finalized_at=now_s,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_zero_carryover(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    return select_carryover(context_id, 0, db_path=db_path)


def ensure_carryover_zero_if_user_messages_exist(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Atomically finalize carryover=0 when any formal user message exists for the day."""
    now_s = _now_local_str()
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        current = dict(row)
        if current.get('selection_finalized_at'):
            conn.commit()
            return {
                'context_id': context_id,
                'context_epoch': int(current['context_epoch']),
                'selected_message_ids': [],
                'carryover_count': int(current.get('carryover_count') or 0),
                'finalized_at': current.get('selection_finalized_at'),
                'already_finalized': True,
            }
        first_id = _first_user_message_id_for_day(conn, local_day=current['local_day'])
        if first_id is None:
            conn.commit()
            return None
        conn.execute('DELETE FROM daily_carryover_messages WHERE context_id=?', (context_id,))
        conn.execute(
            '''UPDATE daily_contexts SET carryover_count=0,
               carryover_requested_count=0, selection_finalized_at=?,
               version=version+1,
               updated_at=datetime('now','+8 hours') WHERE id=?''',
            (now_s, context_id),
        )
        conn.commit()
        return {
            'context_id': context_id,
            'context_epoch': int(current['context_epoch']),
            'selected_message_ids': [],
            'carryover_count': 0,
            'finalized_at': now_s,
            'first_user_message_id': int(first_id),
            'already_finalized': False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalize_zero_for_first_user_message(
    context_id: int,
    user_message_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Legacy entry: auto-zero when the day's first formal user message is known."""
    _ = user_message_id
    out = ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)
    if out is None:
        raise ConflictError('no formal user message for auto-zero')
    return out


def maybe_auto_finalize_zero_on_first_user_message(
    context_id: int,
    *,
    user_message_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Auto-zero when any formal user message exists (idempotent)."""
    return ensure_carryover_zero_if_user_messages_exist(context_id, db_path=db_path)


def get_selected_carryover_messages(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        links = conn.execute(
            'SELECT message_id, ordinal FROM daily_carryover_messages '
            'WHERE context_id=? ORDER BY ordinal ASC',
            (context_id,),
        ).fetchall()
        if not links:
            return []
        ids = [int(r['message_id']) for r in links]
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('image_url', 'source_kind', 'tool_calls'):
            if optional in cols:
                select_cols.append(optional)
        placeholders = ','.join('?' * len(ids))
        rows = conn.execute(
            'SELECT %s FROM chat_messages WHERE id IN (%s)'
            % (', '.join(select_cols), placeholders),
            ids,
        ).fetchall()
        by_id = {int(r['id']): r for r in rows}
        out = []
        for mid in ids:
            r = by_id.get(mid)
            if not r:
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


def validate_formal_handoff_content(
    data: dict[str, Any],
    *,
    expected_source_day: Optional[str] = None,
    expected_source_epoch: Optional[int] = None,
    expected_boundary_message_id: Optional[int] = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ['handoff content must be a mapping']
    encoded = json.dumps(data, ensure_ascii=False).encode('utf-8')
    if len(encoded) > MAX_FILE_BYTES:
        errors.append('handoff content exceeds max bytes (%d)' % MAX_FILE_BYTES)

    for key in HANDOFF_CONTENT_KEYS:
        if key not in data:
            errors.append('missing key: %s' % key)
    unknown = set(data.keys()) - set(HANDOFF_CONTENT_KEYS)
    if unknown:
        errors.append('unknown keys: %s' % ', '.join(sorted(unknown)))

    if expected_source_day is not None:
        if str(data.get('source_day') or '') != str(expected_source_day):
            errors.append('content.source_day must match column source_day')
    if expected_source_epoch is None:
        errors.append('source_epoch is required for READY handoff')
    else:
        try:
            if int(data.get('source_epoch')) != int(expected_source_epoch):
                errors.append('content.source_epoch must match column source_epoch')
        except (TypeError, ValueError):
            errors.append('content.source_epoch must be an integer')
    if expected_boundary_message_id is not None:
        try:
            if int(data.get('boundary_message_id')) != int(expected_boundary_message_id):
                errors.append('content.boundary_message_id must match column')
        except (TypeError, ValueError):
            errors.append('content.boundary_message_id must be an integer')

    for key in (
        'topics', 'confirmed_facts', 'decisions',
        'open_loops', 'explicit_user_requests',
    ):
        val = data.get(key)
        if key not in data:
            continue
        if not isinstance(val, list):
            errors.append('%s must be a list' % key)
            continue
        if len(val) > MAX_LIST_ITEMS:
            errors.append('%s: too many items (%d)' % (key, len(val)))
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append('%s[%d] must be string' % (key, i))
                continue
            if len(item) > MAX_ITEM_CHARS:
                errors.append('%s[%d]: exceeds max item length' % (key, i))
            if contains_assistant_voice_in_text(item):
                errors.append('%s[%d]: contains assistant voice' % (key, i))
            if contains_behavior_instruction_in_text(item):
                errors.append('%s[%d]: contains behavior instruction' % (key, i))
            if re.search(r'[「『""].{8,}?[」』""]', item):
                errors.append('%s[%d]: contains long quoted speech' % (key, i))

    last = data.get('last_topic')
    if last is None:
        errors.append('last_topic must be a string')
    elif not isinstance(last, str):
        errors.append('last_topic must be a string')
    else:
        if len(last) > MAX_LAST_TOPIC_CHARS:
            errors.append('last_topic: exceeds max length')
        if contains_assistant_voice_in_text(last):
            errors.append('last_topic: contains assistant voice')
        if contains_behavior_instruction_in_text(last):
            errors.append('last_topic: contains behavior instruction')
    return errors


def format_formal_handoff_prompt(data: dict[str, Any]) -> str:
    lines = ['【昨日交接·事实记录】', '以下字段记录昨日已经确认的信息，用于保持话题连续性。', '']
    for key in HANDOFF_CONTENT_KEYS:
        val = data.get(key)
        if isinstance(val, list):
            lines.append('%s:' % key)
            for item in val:
                lines.append('  - %s' % item)
        else:
            lines.append('%s: %s' % (key, val))
    return '\n'.join(lines)


def store_day_handoff(
    *,
    chat_id: str,
    source_day: str,
    content: dict[str, Any],
    boundary_message_id: int,
    source_first_message_id: int,
    source_last_message_id: int,
    source_message_count: int,
    source_sha256: str,
    source_epoch: Optional[int] = None,
    status: str = HANDOFF_READY,
    error_code: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db_path)
    source_day = validate_day_string(source_day)
    sha = str(source_sha256 or '').strip().lower()
    if not _SHA256_RE.match(sha):
        raise ValueError('source_sha256 must be 64 lowercase hex characters')
    if int(source_message_count) < 0:
        raise ValueError('source_message_count must be non-negative')
    if int(source_message_count) == 0:
        if int(source_first_message_id) != 0 or int(source_last_message_id) != 0:
            raise ValueError('source message ids must be 0 when count is 0')
    elif int(source_first_message_id) <= 0 or int(source_last_message_id) <= 0:
        raise ValueError('source message ids must be positive when count > 0')
    elif int(source_first_message_id) > int(source_last_message_id):
        raise ValueError('source_first_message_id must be <= source_last_message_id')
    elif int(source_message_count) > (
        int(source_last_message_id) - int(source_first_message_id) + 1
    ):
        raise ValueError('source_message_count exceeds message id span')
    if int(source_last_message_id) > int(boundary_message_id):
        raise ValueError('source_last_message_id must be <= boundary_message_id')

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        src_ctx = get_daily_context(conn, chat_id=chat_id, local_day=source_day)
        if src_ctx is None:
            raise ValueError('source_day daily_context must exist')
        if source_epoch is not None and int(source_epoch) != int(src_ctx['context_epoch']):
            raise ValueError('source_epoch must match source_day context epoch')
        day_dt = datetime.datetime.strptime(source_day, '%Y-%m-%d')
        target_day = (day_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        tgt_ctx = get_daily_context(conn, chat_id=chat_id, local_day=target_day)
        if tgt_ctx is None:
            raise ValueError('target daily_context for handoff consumer day must exist')
        if int(boundary_message_id) != int(tgt_ctx['boundary_message_id']):
            raise ValueError('boundary_message_id must match target daily_context')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if status == HANDOFF_READY:
        if source_epoch is None:
            raise ValueError('source_epoch is required for READY handoff')
        errors = validate_formal_handoff_content(
            content,
            expected_source_day=source_day,
            expected_source_epoch=source_epoch,
            expected_boundary_message_id=boundary_message_id,
        )
        if errors:
            raise ValueError('invalid handoff content: ' + '; '.join(errors))
    payload = json.dumps(content, ensure_ascii=False) if content is not None else None
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT * FROM day_handoffs WHERE chat_id=? AND source_day=? AND source_sha256=?',
            (chat_id, source_day, sha),
        ).fetchone()
        if existing:
            prev = dict(existing)
            # Allow FAILED_RETRYABLE → READY upgrade for the same source SHA.
            if (
                prev.get('status') == HANDOFF_FAILED_RETRYABLE
                and status == HANDOFF_READY
            ):
                errors = validate_formal_handoff_content(
                    content,
                    expected_source_day=source_day,
                    expected_source_epoch=source_epoch,
                    expected_boundary_message_id=boundary_message_id,
                )
                if errors:
                    raise ValueError('invalid handoff content: ' + '; '.join(errors))
                conn.execute(
                    '''UPDATE day_handoffs SET status=?, content_json=?, error_code=NULL,
                       source_epoch=?, boundary_message_id=?,
                       source_first_message_id=?, source_last_message_id=?,
                       source_message_count=?,
                       updated_at=datetime('now','+8 hours') WHERE id=?''',
                    (
                        HANDOFF_READY, payload, source_epoch, boundary_message_id,
                        source_first_message_id, source_last_message_id,
                        source_message_count, prev['id'],
                    ),
                )
                conn.commit()
                return _row_to_dict(conn.execute(
                    'SELECT * FROM day_handoffs WHERE id=?', (prev['id'],),
                ).fetchone()) or {}
            conn.commit()
            return prev
        cur = conn.execute(
            '''INSERT INTO day_handoffs (
                chat_id, source_day, source_epoch, boundary_message_id,
                source_first_message_id, source_last_message_id, source_message_count,
                source_sha256, schema_version, status, content_json, error_code
            ) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)''',
            (
                chat_id, source_day, source_epoch, boundary_message_id,
                source_first_message_id, source_last_message_id, source_message_count,
                sha, status, payload, error_code,
            ),
        )
        conn.commit()
        row = conn.execute(
            'SELECT * FROM day_handoffs WHERE id=?', (cur.lastrowid,),
        ).fetchone()
        return _row_to_dict(row) or {}
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            'SELECT * FROM day_handoffs WHERE chat_id=? AND source_day=? AND source_sha256=?',
            (chat_id, source_day, sha),
        ).fetchone()
        return _row_to_dict(row) or {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_day_handoff(
    handoff_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        row = _row_to_dict(conn.execute(
            'SELECT * FROM day_handoffs WHERE id=?', (handoff_id,),
        ).fetchone())
        if row and row.get('content_json'):
            try:
                row['content'] = json.loads(row['content_json'])
            except json.JSONDecodeError:
                row['content'] = None
        return row
    finally:
        conn.close()


def get_latest_handoff_for_day(
    chat_id: str,
    source_day: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        row = _row_to_dict(conn.execute(
            'SELECT * FROM day_handoffs WHERE chat_id=? AND source_day=? '
            'ORDER BY id DESC LIMIT 1',
            (chat_id, source_day),
        ).fetchone())
        if row and row.get('content_json'):
            try:
                row['content'] = json.loads(row['content_json'])
            except json.JSONDecodeError:
                row['content'] = None
        return row
    finally:
        conn.close()


def resolve_bound_handoff(
    ctx: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], str]:
    """Resolve handoff with strict binding; never inject on mismatch."""
    handoff_status = HANDOFF_ABSENT
    content = None
    h = None
    if ctx.get('handoff_id'):
        h = get_day_handoff(int(ctx['handoff_id']), db_path=db_path)
    else:
        day_dt = datetime.datetime.strptime(ctx['local_day'], '%Y-%m-%d')
        prev = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        h = get_latest_handoff_for_day(ctx['chat_id'], prev, db_path=db_path)
    if not h:
        return None, HANDOFF_ABSENT
    if str(h.get('chat_id') or '') != str(ctx.get('chat_id') or DEFAULT_CHAT_ID):
        return None, HANDOFF_FAILED_RETRYABLE
    expected_prev_day = previous_chat_day(str(ctx['local_day']))
    if str(h.get('source_day') or '') != expected_prev_day:
        return None, HANDOFF_FAILED_RETRYABLE
    handoff_status = str(h.get('status') or HANDOFF_ABSENT)
    if handoff_status != HANDOFF_READY:
        return None, handoff_status
    try:
        content = h.get('content')
        if content is None and h.get('content_json'):
            content = json.loads(h['content_json'])
    except json.JSONDecodeError:
        return None, HANDOFF_FAILED_RETRYABLE
    if not isinstance(content, dict):
        return None, HANDOFF_FAILED_RETRYABLE
    errors = validate_formal_handoff_content(
        content,
        expected_source_day=str(h.get('source_day') or ''),
        expected_source_epoch=h.get('source_epoch'),
        expected_boundary_message_id=int(ctx.get('boundary_message_id') or 0),
    )
    if errors:
        return None, HANDOFF_FAILED_RETRYABLE
    conn = _connect(db_path)
    try:
        source_row = get_daily_context(
            conn, chat_id=str(ctx['chat_id']), local_day=str(h.get('source_day') or ''),
        )
    finally:
        conn.close()
    if source_row is None:
        return None, HANDOFF_ABSENT
    if int(h.get('source_epoch') or -1) != int(source_row['context_epoch']):
        return None, HANDOFF_FAILED_RETRYABLE
    if int(h.get('boundary_message_id') or -1) != int(ctx.get('boundary_message_id') or 0):
        return None, HANDOFF_FAILED_RETRYABLE
    return content, HANDOFF_READY


def get_resident_history_cursor(
    context_id: int,
    resident_generation: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[int]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT history_cursor_message_id FROM daily_resident_cursors '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        return int(row['history_cursor_message_id']) if row else None
    finally:
        conn.close()


def set_resident_history_cursor(
    context_id: int,
    resident_generation: int,
    message_id: int,
    *,
    db_path: Optional[str] = None,
) -> None:
    """Test/setup helper only — production must use advance_resident_history_cursor."""
    advance_resident_history_cursor(
        context_id,
        resident_generation,
        int(message_id),
        db_path=db_path,
    )


def advance_resident_history_cursor(
    context_id: int,
    resident_generation: int,
    processed_through_message_id: int,
    *,
    expected_cursor: Union[int, None, object] = _CURSOR_CAS_OMITTED,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Advance resident history cursor after assistant message is persisted."""
    new_id = int(processed_through_message_id)
    if new_id <= 0:
        raise ValueError('processed_through_message_id must be positive')

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT history_cursor_message_id FROM daily_resident_cursors '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        current = int(row['history_cursor_message_id']) if row else None
        if expected_cursor is not _CURSOR_CAS_OMITTED:
            if expected_cursor is None:
                if row is not None:
                    conn.rollback()
                    raise ConflictError('resident cursor CAS failed')
            else:
                exp = int(expected_cursor)
                if current != exp:
                    conn.rollback()
                    raise ConflictError('resident cursor CAS failed')
        if current is not None and new_id < current:
            conn.rollback()
            raise ConflictError('resident cursor cannot move backward')
        if current is not None and new_id == current:
            conn.commit()
            return {
                'context_id': int(context_id),
                'resident_generation': int(resident_generation),
                'history_cursor_message_id': current,
                'advanced': False,
            }
        conn.execute(
            'INSERT INTO daily_resident_cursors '
            '(context_id, resident_generation, history_cursor_message_id) VALUES (?,?,?) '
            'ON CONFLICT(context_id, resident_generation) DO UPDATE SET '
            'history_cursor_message_id=excluded.history_cursor_message_id, '
            "updated_at=datetime('now','+8 hours')",
            (int(context_id), int(resident_generation), new_id),
        )
        conn.commit()
        return {
            'context_id': int(context_id),
            'resident_generation': int(resident_generation),
            'history_cursor_message_id': new_id,
            'advanced': True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def make_epoch_token(
    *,
    chat_id: str,
    context_epoch: int,
    resident_generation: int,
    is_backfill: bool = False,
) -> dict[str, Any]:
    return {
        'chat_id': chat_id,
        'context_epoch': int(context_epoch),
        'resident_generation': int(resident_generation),
        'is_backfill': int(bool(is_backfill)),
    }


def is_epoch_current(
    token: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> bool:
    if int(token.get('is_backfill') or 0):
        return False
    chat_id = str(token.get('chat_id') or DEFAULT_CHAT_ID)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? AND is_backfill=0 ORDER BY context_epoch DESC LIMIT 1',
            (chat_id,),
        ).fetchone()
        if row is None:
            return False
        return (
            int(row['context_epoch']) == int(token.get('context_epoch') or -1)
            and int(row['resident_generation']) == int(token.get('resident_generation') or -1)
        )
    finally:
        conn.close()


def commit_if_epoch_current(
    token: dict[str, Any],
    operations: list[tuple[str, tuple | list]],
    *,
    db_path: Optional[str] = None,
) -> tuple[bool, int]:
    """Epoch-fenced write via validated structured SQL operations."""
    from chat.daily_fence import commit_if_epoch_current as _fence_commit
    return _fence_commit(token, operations, connect_fn=_connect, db_path=db_path)


def retire_resident_for_rollover(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    if int(ctx.get('is_backfill') or 0):
        raise DailyContextError('backfill context cannot retire resident')
    return {
        'retired_chat_id': ctx['chat_id'],
        'retired_epoch': int(ctx['context_epoch']),
        'retired_generation': int(ctx['resident_generation']),
    }


def respawn_daily_resident(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        if int(dict(row).get('is_backfill') or 0):
            raise DailyContextError('backfill context cannot respawn resident')
        conn.execute(
            '''UPDATE daily_contexts SET resident_generation=resident_generation+1,
               version=version+1, updated_at=datetime('now','+8 hours') WHERE id=?''',
            (context_id,),
        )
        new_gen = int(dict(row)['resident_generation']) + 1
        conn.execute(
            'DELETE FROM daily_resident_cursors WHERE context_id=? AND resident_generation=?',
            (context_id, new_gen),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_daily_context_by_id(context_id, db_path=db_path) or {}


def set_morning_greeting_message_id(
    context_id: int,
    message_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT morning_greeting_message_id, version FROM daily_contexts WHERE id=?',
            (context_id,),
        ).fetchone()
        if row is None:
            raise DailyContextError('daily_context not found')
        if row['morning_greeting_message_id'] is not None:
            raise ConflictError('morning greeting already set')
        cur = conn.execute(
            '''UPDATE daily_contexts SET morning_greeting_message_id=?,
               version=version+1, updated_at=datetime('now','+8 hours')
               WHERE id=? AND morning_greeting_message_id IS NULL''',
            (int(message_id), context_id),
        )
        if cur.rowcount != 1:
            raise ConflictError('morning greeting CAS failed')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_daily_context_by_id(context_id, db_path=db_path) or {}


def _parse_local_dt(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(str(value), '%Y-%m-%d %H:%M:%S')


def get_latest_active_context(
    chat_id: str = DEFAULT_CHAT_ID,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Highest active (non-backfill) daily context for chat_id."""
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute(
            'SELECT * FROM daily_contexts WHERE chat_id=? AND is_backfill=0 '
            'ORDER BY context_epoch DESC LIMIT 1',
            (chat_id,),
        ).fetchone())
    finally:
        conn.close()


def get_context_for_local_day(
    chat_id: str,
    local_day: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return get_daily_context(conn, chat_id=chat_id, local_day=local_day)
    finally:
        conn.close()


def _lease_row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return _row_to_dict(row)


def is_resident_turn_active(
    context_id: int,
    resident_generation: int,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> bool:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT expires_at FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if row is None:
            return False
        now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
        try:
            exp_dt = _parse_local_dt(str(row['expires_at']))
        except ValueError:
            return False
        return exp_dt > now_dt
    finally:
        conn.close()


def has_active_provider_turn_lease(
    chat_id: str = DEFAULT_CHAT_ID,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> bool:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            'SELECT l.context_id, l.resident_generation, l.expires_at '
            'FROM daily_resident_turn_leases l '
            'JOIN daily_contexts c ON c.id=l.context_id '
            'WHERE c.chat_id=? AND c.is_backfill=0',
            (chat_id,),
        ).fetchall()
        now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
        for row in rows:
            try:
                exp_dt = _parse_local_dt(str(row['expires_at']))
            except ValueError:
                continue
            if exp_dt > now_dt:
                return True
        return False
    finally:
        conn.close()


def claim_daily_resident_turn(
    *,
    chat_id: str,
    context_id: int,
    expected_context_epoch: int,
    worker_id: str,
    request_message_id: int,
    lease_owner: str,
    resident_key: str,
    bound_cursor_message_id: Optional[int] = None,
    process_generation: Optional[int] = None,
    ttl_seconds: int = 480,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Atomically claim worker owner + turn lease for one resident generation."""
    ensure_schema(db_path)
    owner = str(lease_owner or '').strip()
    wid = str(worker_id or '').strip()
    if not owner:
        raise ValueError('lease_owner required')
    if not wid:
        raise ValueError('worker_id required')
    req_id = int(request_message_id)
    if req_id <= 0:
        raise ValueError('request_message_id must be positive')
    now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
    now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    exp_s = (now_dt + datetime.timedelta(seconds=int(ttl_seconds))).strftime('%Y-%m-%d %H:%M:%S')
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        ctx_row = conn.execute(
            'SELECT * FROM daily_contexts WHERE id=?', (int(context_id),),
        ).fetchone()
        if ctx_row is None:
            conn.rollback()
            raise ConflictError('daily_context not found')
        ctx = dict(ctx_row)
        if int(ctx.get('is_backfill') or 0):
            conn.rollback()
            raise ConflictError('backfill context cannot claim turn')
        if str(ctx.get('chat_id') or '') != str(chat_id):
            conn.rollback()
            raise ConflictError('chat_id mismatch')
        if int(ctx['context_epoch']) != int(expected_context_epoch):
            conn.rollback()
            raise ConflictError('context epoch mismatch')
        latest = conn.execute(
            'SELECT id FROM daily_contexts WHERE chat_id=? AND is_backfill=0 '
            'ORDER BY context_epoch DESC LIMIT 1',
            (str(chat_id),),
        ).fetchone()
        if latest is None or int(latest['id']) != int(context_id):
            conn.rollback()
            raise ConflictError('context is not latest active')

        idempotent_lease: Optional[tuple[int, dict[str, Any]]] = None
        for lr in conn.execute(
            'SELECT * FROM daily_resident_turn_leases WHERE context_id=?',
            (int(context_id),),
        ).fetchall():
            lr_d = dict(lr)
            try:
                exp_dt = _parse_local_dt(str(lr_d['expires_at']))
            except ValueError:
                continue
            if exp_dt <= now_dt:
                continue
            lease_gen = int(lr_d['resident_generation'])
            if (
                str(lr_d['lease_owner']) == owner
                and int(lr_d['request_message_id']) == req_id
            ):
                owner_row = conn.execute(
                    'SELECT * FROM daily_resident_owners '
                    'WHERE context_id=? AND resident_generation=?',
                    (int(context_id), lease_gen),
                ).fetchone()
                if (
                    owner_row is not None
                    and str(dict(owner_row).get('worker_id') or '') == wid
                    and int(ctx['resident_generation']) == lease_gen
                ):
                    if idempotent_lease is not None:
                        conn.rollback()
                        raise ConflictError('multiple active idempotent leases')
                    idempotent_lease = (lease_gen, lr_d)
                else:
                    conn.rollback()
                    raise ConflictError('active lease owner mismatch for idempotent claim')
            else:
                conn.rollback()
                raise ConflictError('resident turn lease held by another claim')

        if idempotent_lease is not None:
            resident_generation, held = idempotent_lease
            context_epoch = int(dict(ctx_row)['context_epoch'])
            resident_key = make_resident_key(
                chat_id=str(chat_id),
                context_epoch=context_epoch,
                resident_generation=resident_generation,
            )
            conn.commit()
            return {
                'status': 'idempotent',
                'context_id': int(context_id),
                'context_epoch': context_epoch,
                'resident_generation': resident_generation,
                'resident_key': str(resident_key),
                'lease': dict(held),
            }

        resident_generation = int(ctx['resident_generation'])
        context_epoch = int(ctx['context_epoch'])
        status = 'owned'
        owner_row = conn.execute(
            'SELECT * FROM daily_resident_owners WHERE context_id=? AND resident_generation=?',
            (int(context_id), resident_generation),
        ).fetchone()
        if owner_row is not None and str(dict(owner_row).get('worker_id') or '') != wid:
            conn.execute(
                '''UPDATE daily_contexts SET resident_generation=resident_generation+1,
                   version=version+1, updated_at=? WHERE id=?''',
                (now_s, int(context_id)),
            )
            resident_generation += 1
            status = 'takeover'
            bound_cursor_message_id = None
            process_generation = None
            conn.execute(
                'DELETE FROM daily_resident_cursors WHERE context_id=? AND resident_generation=?',
                (int(context_id), resident_generation),
            )
            resident_key = make_resident_key(
                chat_id=str(chat_id),
                context_epoch=context_epoch,
                resident_generation=resident_generation,
            )

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
                int(context_id), resident_generation, wid, str(resident_key),
                bound_cursor_message_id, process_generation, now_s,
            ),
        )

        lease_row = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), resident_generation),
        ).fetchone()
        if lease_row is not None:
            conn.execute(
                '''UPDATE daily_resident_turn_leases SET lease_owner=?, request_message_id=?,
                   acquired_at=?, expires_at=?, updated_at=?
                   WHERE context_id=? AND resident_generation=?''',
                (
                    owner, req_id, now_s, exp_s, now_s,
                    int(context_id), resident_generation,
                ),
            )
        else:
            conn.execute(
                '''INSERT INTO daily_resident_turn_leases (
                    context_id, resident_generation, lease_owner, request_message_id,
                    acquired_at, expires_at, updated_at
                ) VALUES (?,?,?,?,?,?,?)''',
                (
                    int(context_id), resident_generation, owner, req_id,
                    now_s, exp_s, now_s,
                ),
            )
        held = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), resident_generation),
        ).fetchone()
        if held is None or str(dict(held)['lease_owner']) != owner:
            conn.rollback()
            raise ConflictError('resident turn lease claim failed')
        conn.commit()
        return {
            'status': status,
            'context_id': int(context_id),
            'context_epoch': context_epoch,
            'resident_generation': resident_generation,
            'resident_key': str(resident_key),
            'lease': dict(held),
        }
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def acquire_resident_turn_lease(
    context_id: int,
    resident_generation: int,
    *,
    lease_owner: str,
    request_message_id: int,
    ttl_seconds: int = 480,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    """Acquire exclusive turn lease for one resident generation."""
    ensure_schema(db_path)
    owner = str(lease_owner or '').strip()
    if not owner:
        raise ValueError('lease_owner required')
    req_id = int(request_message_id)
    if req_id <= 0:
        raise ValueError('request_message_id must be positive')
    now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
    now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    exp_s = (now_dt + datetime.timedelta(seconds=int(ttl_seconds))).strftime('%Y-%m-%d %H:%M:%S')
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if row is not None:
            current = dict(row)
            try:
                exp_dt = _parse_local_dt(str(current['expires_at']))
            except ValueError:
                exp_dt = now_dt
            if exp_dt > now_dt and str(current['lease_owner']) != owner:
                conn.rollback()
                raise ConflictError('resident turn lease held by another owner')
            conn.execute(
                '''UPDATE daily_resident_turn_leases SET lease_owner=?, request_message_id=?,
                   acquired_at=?, expires_at=?, updated_at=?
                   WHERE context_id=? AND resident_generation=?''',
                (
                    owner, req_id, now_s, exp_s, now_s,
                    int(context_id), int(resident_generation),
                ),
            )
        else:
            conn.execute(
                '''INSERT INTO daily_resident_turn_leases (
                    context_id, resident_generation, lease_owner, request_message_id,
                    acquired_at, expires_at, updated_at
                ) VALUES (?,?,?,?,?,?,?)''',
                (
                    int(context_id), int(resident_generation), owner, req_id,
                    now_s, exp_s, now_s,
                ),
            )
        held = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if held is None or str(dict(held)['lease_owner']) != owner:
            conn.rollback()
            raise ConflictError('resident turn lease acquire failed')
        conn.commit()
        return dict(held)
    except ConflictError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew_resident_turn_lease(
    context_id: int,
    resident_generation: int,
    *,
    lease_owner: str,
    chat_id: Optional[str] = None,
    worker_id: Optional[str] = None,
    resident_key: Optional[str] = None,
    ttl_seconds: int = 480,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    epoch_token: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    owner = str(lease_owner or '').strip()
    now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
    now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    exp_s = (now_dt + datetime.timedelta(seconds=int(ttl_seconds))).strftime('%Y-%m-%d %H:%M:%S')
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row_ctx = conn.execute(
            'SELECT id, chat_id, context_epoch, resident_generation, is_backfill FROM daily_contexts WHERE id=?',
            (int(context_id),),
        ).fetchone()
        if row_ctx is None or int(dict(row_ctx).get('is_backfill') or 0):
            conn.rollback()
            raise ConflictError('epoch not current for renew')
        cur = dict(row_ctx)
        cid = str(chat_id or epoch_token.get('chat_id') if epoch_token else '') or str(cur.get('chat_id') or '')
        latest = conn.execute(
            'SELECT id, context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? AND is_backfill=0 ORDER BY context_epoch DESC LIMIT 1',
            (cid,),
        ).fetchone()
        if latest is None or int(latest['id']) != int(context_id):
            conn.rollback()
            raise ConflictError('context is not latest active for renew')
        if int(cur['resident_generation']) != int(resident_generation):
            conn.rollback()
            raise ConflictError('resident generation stale for renew')
        if epoch_token is not None:
            if (
                int(cur['context_epoch']) != int(epoch_token.get('context_epoch') or -1)
                or int(cur['resident_generation']) != int(epoch_token.get('resident_generation') or -1)
            ):
                conn.rollback()
                raise ConflictError('epoch token stale for renew')
        owner_row = conn.execute(
            'SELECT * FROM daily_resident_owners WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if owner_row is None:
            conn.rollback()
            raise ConflictError('resident owner missing for renew')
        o = dict(owner_row)
        if worker_id is not None and str(o.get('worker_id') or '') != str(worker_id):
            conn.rollback()
            raise ConflictError('resident owner worker mismatch for renew')
        if resident_key is not None and str(o.get('resident_key') or '') != str(resident_key):
            conn.rollback()
            raise ConflictError('resident owner key mismatch for renew')
        row = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if row is None or str(dict(row)['lease_owner']) != owner:
            conn.rollback()
            raise ConflictError('resident turn lease owner mismatch')
        try:
            exp_dt = _parse_local_dt(str(dict(row)['expires_at']))
        except ValueError:
            exp_dt = now_dt
        if exp_dt <= now_dt:
            conn.rollback()
            raise ConflictError('resident turn lease expired')
        conn.execute(
            '''UPDATE daily_resident_turn_leases SET expires_at=?, updated_at=?
               WHERE context_id=? AND resident_generation=? AND lease_owner=?''',
            (exp_s, now_s, int(context_id), int(resident_generation), owner),
        )
        conn.commit()
        held = conn.execute(
            'SELECT * FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        return dict(held) if held else {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_resident_turn_lease(
    context_id: int,
    resident_generation: int,
    *,
    lease_owner: str,
    db_path: Optional[str] = None,
) -> bool:
    owner = str(lease_owner or '').strip()
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        cur = conn.execute(
            'DELETE FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=? AND lease_owner=?',
            (int(context_id), int(resident_generation), owner),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_daily_message_context(
    message_id: int,
    *,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    role: str,
    db_path: Optional[str] = None,
) -> None:
    ensure_schema(db_path)
    now_s = _now_local_str()
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT context_id, context_epoch, resident_generation FROM daily_message_contexts '
            'WHERE message_id=?',
            (int(message_id),),
        ).fetchone()
        if existing is not None:
            ex = dict(existing)
            if (
                int(ex['context_id']) == int(context_id)
                and int(ex['context_epoch']) == int(context_epoch)
            ):
                conn.execute(
                    'UPDATE daily_message_contexts SET resident_generation=?, role=?, created_at=? '
                    'WHERE message_id=?',
                    (int(resident_generation), str(role), now_s, int(message_id)),
                )
            elif int(ex['context_id']) != int(context_id) or int(ex['context_epoch']) != int(context_epoch):
                conn.rollback()
                raise ConflictError('message already mapped to another context')
            else:
                conn.execute(
                    'UPDATE daily_message_contexts SET resident_generation=?, role=?, created_at=? '
                    'WHERE message_id=?',
                    (int(resident_generation), str(role), now_s, int(message_id)),
                )
        else:
            conn.execute(
                'INSERT INTO daily_message_contexts '
                '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
                'VALUES (?,?,?,?,?,?)',
                (
                    int(message_id), int(context_id), int(context_epoch),
                    int(resident_generation), str(role), now_s,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_message_context(
    message_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute(
            'SELECT * FROM daily_message_contexts WHERE message_id=?',
            (int(message_id),),
        ).fetchone())
    finally:
        conn.close()


def persist_daily_assistant_if_current(
    *,
    chat_id: str,
    context_id: int,
    context_epoch: int,
    resident_generation: int,
    lease_owner: str,
    content: str,
    thinking: str = '',
    tool_calls: str = '',
    cache_info: str = '',
    choices: str = '',
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> int:
    """Atomically verify epoch+lease and INSERT assistant with context membership."""
    ensure_schema(db_path)
    owner = str(lease_owner or '').strip()
    now_dt = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT id, context_epoch, resident_generation, is_backfill FROM daily_contexts WHERE id=?',
            (int(context_id),),
        ).fetchone()
        if row is None or int(dict(row).get('is_backfill') or 0):
            conn.rollback()
            raise ConflictError('daily_context not active')
        current = dict(row)
        if (
            int(current['context_epoch']) != int(context_epoch)
            or int(current['resident_generation']) != int(resident_generation)
        ):
            conn.rollback()
            raise ConflictError('epoch/generation stale at persist')
        active = conn.execute(
            'SELECT context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? AND is_backfill=0 ORDER BY context_epoch DESC LIMIT 1',
            (str(chat_id),),
        ).fetchone()
        if active is None or int(active['context_epoch']) != int(context_epoch):
            conn.rollback()
            raise ConflictError('context epoch no longer active')
        lease = conn.execute(
            'SELECT lease_owner, expires_at FROM daily_resident_turn_leases '
            'WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone()
        if lease is None or str(dict(lease)['lease_owner']) != owner:
            conn.rollback()
            raise ConflictError('lease owner mismatch at persist')
        try:
            exp_dt = _parse_local_dt(str(dict(lease)['expires_at']))
        except ValueError:
            exp_dt = now_dt
        if exp_dt <= now_dt:
            conn.rollback()
            raise ConflictError('lease expired at persist')
        cur = conn.execute(
            "INSERT INTO chat_messages (author, content, thinking, tool_calls, cache_info, choices) "
            "VALUES ('assistant', ?, ?, ?, ?, ?)",
            (content, thinking, tool_calls, cache_info, choices),
        )
        assistant_id = int(cur.lastrowid)
        now_s = now_dt.strftime('%Y-%m-%d %H:%M:%S')
        conn.execute(
            'INSERT INTO daily_message_contexts '
            '(message_id, context_id, context_epoch, resident_generation, role, created_at) '
            'VALUES (?,?,?,?,?,?)',
            (
                assistant_id, int(context_id), int(context_epoch),
                int(resident_generation), 'assistant', now_s,
            ),
        )
        conn.commit()
        return assistant_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_resident_owner(
    context_id: int,
    resident_generation: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    conn = _connect(db_path)
    try:
        return _row_to_dict(conn.execute(
            'SELECT * FROM daily_resident_owners WHERE context_id=? AND resident_generation=?',
            (int(context_id), int(resident_generation)),
        ).fetchone())
    finally:
        conn.close()


def upsert_resident_owner(
    context_id: int,
    resident_generation: int,
    *,
    worker_id: str,
    resident_key: str,
    bound_cursor_message_id: Optional[int] = None,
    process_generation: Optional[int] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    now_s = _now_local_str()
    conn = _connect(db_path)
    try:
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
                int(context_id), int(resident_generation), str(worker_id), str(resident_key),
                bound_cursor_message_id, process_generation, now_s,
            ),
        )
        conn.commit()
        return get_resident_owner(context_id, resident_generation, db_path=db_path) or {}
    finally:
        conn.close()


def ensure_worker_resident_owner(
    context_id: int,
    resident_generation: int,
    *,
    worker_id: str,
    resident_key: str,
    bound_cursor_message_id: Optional[int],
    process_generation: Optional[int],
    db_path: Optional[str] = None,
) -> tuple[str, int, str]:
    """Return (status, generation, resident_key). status is owned|takeover."""
    owner = get_resident_owner(context_id, resident_generation, db_path=db_path)
    if owner is None or str(owner.get('worker_id') or '') == str(worker_id):
        upsert_resident_owner(
            context_id, resident_generation,
            worker_id=worker_id,
            resident_key=resident_key,
            bound_cursor_message_id=bound_cursor_message_id,
            process_generation=process_generation,
            db_path=db_path,
        )
        return 'owned', int(resident_generation), resident_key
    new_ctx = respawn_daily_resident(context_id, db_path=db_path)
    new_gen = int(new_ctx['resident_generation'])
    new_key = make_resident_key(
        chat_id=str(new_ctx.get('chat_id') or DEFAULT_CHAT_ID),
        context_epoch=int(new_ctx['context_epoch']),
        resident_generation=new_gen,
    )
    upsert_resident_owner(
        context_id, new_gen,
        worker_id=worker_id,
        resident_key=new_key,
        bound_cursor_message_id=None,
        process_generation=None,
        db_path=db_path,
    )
    return 'takeover', new_gen, new_key


def make_resident_key(
    *,
    chat_id: str,
    context_epoch: int,
    resident_generation: int,
) -> str:
    return 'daily:%s:%s:%s' % (chat_id, int(context_epoch), int(resident_generation))


def current_summary(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    ctx = resolve_current_daily_context_for_api(
        chat_id=chat_id, db_path=db_path, now=now,
    )
    context_id = int(ctx['id'])
    selection_finalized = bool(ctx.get('selection_finalized_at'))
    if selection_finalized:
        selected_round_count = int(ctx.get('carryover_count') or 0)
        raw_requested = ctx.get('carryover_requested_count')
        if raw_requested is None:
            requested_round_count: Optional[int] = selected_round_count
        else:
            requested_round_count = int(raw_requested)
        selected_messages = get_selected_carryover_messages(context_id, db_path=db_path)
        selected_message_ids = [int(m['message_id']) for m in selected_messages]
    else:
        requested_round_count = None
        selected_round_count = 0
        selected_message_ids = []
    handoff_status = HANDOFF_ABSENT
    if ctx.get('handoff_id'):
        h = get_day_handoff(int(ctx['handoff_id']), db_path=db_path)
        handoff_status = (h or {}).get('status') or HANDOFF_ABSENT
    else:
        day_dt = datetime.datetime.strptime(ctx['local_day'], '%Y-%m-%d')
        prev = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        h = get_latest_handoff_for_day(chat_id, prev, db_path=db_path)
        if h:
            handoff_status = h.get('status') or HANDOFF_ABSENT
    return {
        'local_day': ctx['local_day'],
        'context_epoch': int(ctx['context_epoch']),
        'status': ctx['status'],
        'boundary_message_id': int(ctx['boundary_message_id'] or 0),
        'carryover_unit': CARRYOVER_UNIT,
        'requested_round_count': requested_round_count,
        'selected_round_count': selected_round_count,
        'selected_message_count': len(selected_message_ids),
        'selected_message_ids': selected_message_ids,
        'carryover_count': selected_round_count,
        'selection_finalized': selection_finalized,
        'handoff_status': handoff_status,
        'resident_generation': int(ctx['resident_generation'] or 1),
        'context_id': context_id,
    }
