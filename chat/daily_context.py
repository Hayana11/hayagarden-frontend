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
from typing import Any, Callable, Optional

from chat.day_handoff import (
    CHAT_DAY_START_HOUR,
    MAX_FILE_BYTES,
    MAX_ITEM_CHARS,
    MAX_LAST_TOPIC_CHARS,
    MAX_LIST_ITEMS,
    TZ_OFFSET_HOURS,
    chat_day_for_timestamp,
    chat_day_window,
    validate_day_string,
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

_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ABSENT: frozenset({STATUS_COMPACTING, STATUS_PROVISIONAL}),
    STATUS_COMPACTING: frozenset({STATUS_PROVISIONAL, STATUS_FAILED_RETRYABLE}),
    STATUS_FAILED_RETRYABLE: frozenset({STATUS_PROVISIONAL, STATUS_COMPACTING}),
    # Late / retry compaction after chat already opened.
    STATUS_PROVISIONAL: frozenset({STATUS_FINALIZED, STATUS_COMPACTING}),
    STATUS_FINALIZED: frozenset(),
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

_FORBIDDEN_HANDOFF_MARKERS = (
    '助手回应', '助手：', '助手:',
    '我轻轻', '我抱着', '我揽着', '我低声', '我轻声',
    '你应该', '怎么回复', '怎么哄', '语气',
    '（抱', '（揽', '（亲', '【动作',
)

_SCHEMA_READY: set[str] = set()


class DailyContextError(Exception):
    """Base error for daily soft window."""


class ConflictError(DailyContextError):
    """Selection locked or CAS conflict (HTTP 409)."""


class InvalidTransitionError(DailyContextError):
    """Illegal status machine transition."""


class DeferredError(DailyContextError):
    """Provider request in flight; rollover deferred (retryable)."""


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
                UNIQUE(chat_id, local_day)
            );
            CREATE INDEX IF NOT EXISTS idx_daily_contexts_chat_epoch
                ON daily_contexts(chat_id, context_epoch);

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
            """
        )
        # Authoritative origin for formal vs wake/workspace/tool rows.
        cols = _table_columns(conn, 'chat_messages')
        if cols and 'source_kind' not in cols:
            conn.execute(
                "ALTER TABLE chat_messages ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'chat'"
            )
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
    """Last formal chat_messages.id before local_day 04:00; 0 if none.

    R0: chat_id is accepted for API symmetry but message table is global.
    """
    _ = chat_id
    _day, start_at, _end, _next = chat_day_window(local_day)
    row = conn.execute(
        'SELECT id FROM chat_messages WHERE created_at < ? ORDER BY id DESC LIMIT 1',
        (start_at,),
    ).fetchone()
    return int(row['id'] if row else 0)


def _max_epoch(conn: sqlite3.Connection, chat_id: str) -> int:
    row = conn.execute(
        'SELECT MAX(context_epoch) AS m FROM daily_contexts WHERE chat_id=?',
        (chat_id,),
    ).fetchone()
    return int(row['m'] or 0) if row else 0


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


def get_or_create_daily_context(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    local_day: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
    db_path: Optional[str] = None,
    provider_busy: bool = False,
    skip_compaction: bool = True,
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
    if local_day is None:
        base = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET_HOURS))
        local_day = chat_day_for_timestamp(base)
    else:
        local_day = validate_day_string(local_day)
    validate_timezone(DEFAULT_TIMEZONE)

    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = get_daily_context(conn, chat_id=chat_id, local_day=local_day)
        if existing is not None:
            conn.commit()
            return existing

        boundary_id = get_boundary_message_id(conn, local_day=local_day, chat_id=chat_id)
        epoch = _max_epoch(conn, chat_id) + 1
        now_s = _now_local_str()
        status = STATUS_ABSENT
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count,
                resident_generation, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                epoch, boundary_id, status, now_s, now_s,
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


def _tool_calls_nonempty(row: Any) -> bool:
    if not (hasattr(row, 'keys') and 'tool_calls' in row.keys()):
        return False
    tc = row['tool_calls']
    if tc is None:
        return False
    s = str(tc).strip()
    return s not in ('', 'null', 'None', '[]', '{}')


def is_formal_chat_message(row: Any) -> bool:
    """Authoritative formal-message predicate for carryover and current-day history."""
    author = str(row['author'] or '').strip().lower()
    if author in _EXCLUDED_AUTHORS:
        return False
    if author not in _ELIGIBLE_AUTHORS:
        return False

    kind = _row_source_kind(row)
    if kind and kind not in (SOURCE_KIND_CHAT,):
        return False

    if _tool_calls_nonempty(row):
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


def list_carryover_candidates(
    context_id: int,
    *,
    limit: int = 10,
    db_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    local_day = ctx['local_day']
    day_dt = datetime.datetime.strptime(local_day, '%Y-%m-%d')
    prev_day = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    _d, start_at, _end, next_start = chat_day_window(prev_day)
    boundary = int(ctx['boundary_message_id'] or 0)

    conn = _connect(db_path)
    try:
        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        rows = conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id <= ? '
            'ORDER BY id ASC' % ', '.join(select_cols),
            (start_at, next_start, boundary if boundary > 0 else 10**18),
        ).fetchall()
        eligible = [r for r in rows if is_formal_chat_message(r)]
        tail = eligible[-limit:] if limit else []
        out = []
        for r in tail:
            content = _message_display_content(r)
            preview = content if len(content) <= 160 else content[:157] + '…'
            role = 'user' if str(r['author']).lower() in _USER_AUTHORS else 'assistant'
            out.append({
                'message_id': int(r['id']),
                'role': role,
                'author': str(r['author']),
                'content_preview': preview,
                'created_at': str(r['created_at'] or ''),
            })
        return out
    finally:
        conn.close()


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
    for r in rows:
        if str(r['author'] or '').lower() not in _USER_AUTHORS:
            continue
        if not is_formal_chat_message(r):
            continue
        return int(r['id'])
    return None


def _selection_locked_conn(conn: sqlite3.Connection, ctx: dict[str, Any]) -> bool:
    if ctx.get('selection_finalized_at'):
        return True
    if ctx.get('status') == STATUS_FINALIZED:
        return True
    return _first_user_message_id_for_day(conn, local_day=ctx['local_day']) is not None


def _selection_locked(ctx: dict[str, Any], db_path: Optional[str] = None) -> bool:
    conn = _connect(db_path)
    try:
        return _selection_locked_conn(conn, ctx)
    finally:
        conn.close()


def select_carryover(
    context_id: int,
    count: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    if count not in ALLOWED_CARRYOVER_COUNTS:
        raise ValueError('count must be one of %s' % sorted(ALLOWED_CARRYOVER_COUNTS))

    candidates = list_carryover_candidates(context_id, limit=10, db_path=db_path)
    if count == 0:
        selected = []
    else:
        selected = candidates[-count:] if len(candidates) >= count else list(candidates)

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
        assert_transition(current['status'], STATUS_FINALIZED)

        conn.execute('DELETE FROM daily_carryover_messages WHERE context_id=?', (context_id,))
        ids = []
        for i, item in enumerate(selected):
            mid = int(item['message_id'])
            conn.execute(
                'INSERT INTO daily_carryover_messages (context_id, ordinal, message_id) VALUES (?,?,?)',
                (context_id, i, mid),
            )
            ids.append(mid)
        conn.execute(
            '''UPDATE daily_contexts SET status=?, carryover_count=?,
               selection_finalized_at=?, version=version+1,
               updated_at=datetime('now','+8 hours') WHERE id=?''',
            (STATUS_FINALIZED, len(ids), now_s, context_id),
        )
        conn.commit()
        return {
            'context_id': context_id,
            'context_epoch': int(current['context_epoch']),
            'selected_message_ids': ids,
            'carryover_count': len(ids),
            'finalized_at': now_s,
        }
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


def finalize_zero_for_first_user_message(
    context_id: int,
    user_message_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Atomically finalize carryover=0 when user_message_id is the day's first formal user msg.

    Intended for the formal chat path: user row is already committed, then assembly
    calls this with that message id. Does not use the generic select_carryover lock.
    """
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

        if current.get('selection_finalized_at') or current['status'] == STATUS_FINALIZED:
            if int(current.get('carryover_count') or 0) == 0:
                conn.commit()
                return {
                    'context_id': context_id,
                    'context_epoch': int(current['context_epoch']),
                    'selected_message_ids': [],
                    'carryover_count': 0,
                    'finalized_at': current.get('selection_finalized_at') or now_s,
                    'already_finalized': True,
                }
            raise ConflictError('carryover selection is locked')

        if current['status'] not in (STATUS_PROVISIONAL, STATUS_ABSENT, STATUS_FAILED_RETRYABLE):
            if current['status'] != STATUS_COMPACTING:
                raise ConflictError('cannot auto-finalize from status %s' % current['status'])

        cols = _table_columns(conn, 'chat_messages')
        select_cols = ['id', 'author', 'content', 'created_at']
        for optional in ('tool_calls', 'source_kind', 'image_url'):
            if optional in cols:
                select_cols.append(optional)
        msg = conn.execute(
            'SELECT %s FROM chat_messages WHERE id=?' % ', '.join(select_cols),
            (int(user_message_id),),
        ).fetchone()
        if msg is None:
            raise DailyContextError('user message not found: %s' % user_message_id)
        if str(msg['author'] or '').lower() not in _USER_AUTHORS:
            raise ConflictError('message is not a user message')
        if not is_formal_chat_message(msg):
            raise ConflictError('message is not a formal chat message')

        _d, start_at, _end, next_start = chat_day_window(current['local_day'])
        created = str(msg['created_at'] or '')
        if not (start_at <= created < next_start):
            raise ConflictError('message is outside current chat day')

        first_id = _first_user_message_id_for_day(conn, local_day=current['local_day'])
        if first_id is None or int(first_id) != int(user_message_id):
            raise ConflictError('message is not the first formal user message of the day')

        if current['status'] != STATUS_FINALIZED:
            # PROVISIONAL → FINALIZED (or from COMPACTING/ABSENT/FAILED via provisional first)
            if current['status'] == STATUS_PROVISIONAL:
                assert_transition(STATUS_PROVISIONAL, STATUS_FINALIZED)
            elif current['status'] in (STATUS_ABSENT, STATUS_FAILED_RETRYABLE, STATUS_COMPACTING):
                # Promote to provisional then finalize in one txn without separate public calls.
                if current['status'] != STATUS_PROVISIONAL:
                    # Direct jump not in table for ABSENT→FINALIZED; do two asserts.
                    if current['status'] == STATUS_ABSENT:
                        assert_transition(STATUS_ABSENT, STATUS_PROVISIONAL)
                    elif current['status'] == STATUS_FAILED_RETRYABLE:
                        assert_transition(STATUS_FAILED_RETRYABLE, STATUS_PROVISIONAL)
                    elif current['status'] == STATUS_COMPACTING:
                        assert_transition(STATUS_COMPACTING, STATUS_PROVISIONAL)
                    assert_transition(STATUS_PROVISIONAL, STATUS_FINALIZED)

        conn.execute('DELETE FROM daily_carryover_messages WHERE context_id=?', (context_id,))
        conn.execute(
            '''UPDATE daily_contexts SET status=?, carryover_count=0,
               selection_finalized_at=?, lease_owner=NULL, lease_expires_at=NULL,
               version=version+1, updated_at=datetime('now','+8 hours') WHERE id=?''',
            (STATUS_FINALIZED, now_s, context_id),
        )
        conn.commit()
        return {
            'context_id': context_id,
            'context_epoch': int(current['context_epoch']),
            'selected_message_ids': [],
            'carryover_count': 0,
            'finalized_at': now_s,
            'already_finalized': False,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def maybe_auto_finalize_zero_on_first_user_message(
    context_id: int,
    *,
    user_message_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Prefer finalize_zero_for_first_user_message when id is known."""
    if user_message_id is None:
        return None
    try:
        return finalize_zero_for_first_user_message(
            context_id, int(user_message_id), db_path=db_path,
        )
    except ConflictError:
        ctx = get_daily_context_by_id(context_id, db_path=db_path)
        if ctx and ctx.get('selection_finalized_at') and int(ctx.get('carryover_count') or 0) == 0:
            return {
                'context_id': context_id,
                'context_epoch': int(ctx['context_epoch']),
                'selected_message_ids': [],
                'carryover_count': 0,
                'finalized_at': ctx.get('selection_finalized_at'),
                'already_finalized': True,
            }
        raise


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
    if expected_source_epoch is not None:
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
            for marker in _FORBIDDEN_HANDOFF_MARKERS:
                if marker in item:
                    errors.append('%s[%d]: forbidden marker %r' % (key, i, marker))
                    break
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
        for marker in _FORBIDDEN_HANDOFF_MARKERS:
            if marker in last:
                errors.append('last_topic: forbidden marker %r' % marker)
                break
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

    if status == HANDOFF_READY:
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


def make_epoch_token(
    *,
    chat_id: str,
    context_epoch: int,
    resident_generation: int,
) -> dict[str, Any]:
    return {
        'chat_id': chat_id,
        'context_epoch': int(context_epoch),
        'resident_generation': int(resident_generation),
    }


def is_epoch_current(
    token: dict[str, Any],
    *,
    db_path: Optional[str] = None,
) -> bool:
    chat_id = str(token.get('chat_id') or DEFAULT_CHAT_ID)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            'SELECT context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? ORDER BY context_epoch DESC LIMIT 1',
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
    writer: Callable[[sqlite3.Connection], Any],
    *,
    db_path: Optional[str] = None,
) -> tuple[bool, Any]:
    """Run writer(conn) inside one BEGIN IMMEDIATE txn with epoch/generation CAS.

    writer MUST use the provided connection. A second CAS check runs after writer
    returns and before commit, so respawn mid-write still rolls back.
    """
    chat_id = str(token.get('chat_id') or DEFAULT_CHAT_ID)
    want_epoch = int(token.get('context_epoch') or -1)
    want_gen = int(token.get('resident_generation') or -1)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            'SELECT id, context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? ORDER BY context_epoch DESC LIMIT 1',
            (chat_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False, None
        if int(row['context_epoch']) != want_epoch or int(row['resident_generation']) != want_gen:
            conn.rollback()
            return False, None
        result = writer(conn)
        row2 = conn.execute(
            'SELECT context_epoch, resident_generation FROM daily_contexts WHERE id=?',
            (int(row['id']),),
        ).fetchone()
        if (
            row2 is None
            or int(row2['context_epoch']) != want_epoch
            or int(row2['resident_generation']) != want_gen
        ):
            conn.rollback()
            return False, None
        conn.commit()
        return True, result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def retire_resident_for_rollover(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
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
        conn.execute(
            '''UPDATE daily_contexts SET resident_generation=resident_generation+1,
               version=version+1, updated_at=datetime('now','+8 hours') WHERE id=?''',
            (context_id,),
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


def current_summary(
    *,
    chat_id: str = DEFAULT_CHAT_ID,
    db_path: Optional[str] = None,
    now: Optional[datetime.datetime] = None,
) -> dict[str, Any]:
    ctx = get_or_create_daily_context(chat_id=chat_id, db_path=db_path, now=now)
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
        'carryover_count': int(ctx['carryover_count'] or 0),
        'selection_finalized': bool(ctx.get('selection_finalized_at')),
        'handoff_status': handoff_status,
        'resident_generation': int(ctx['resident_generation'] or 1),
        'context_id': int(ctx['id']),
    }
