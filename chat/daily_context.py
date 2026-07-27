"""Daily Soft Window — backend boundary, epoch, carryover, and handoff storage.

P-CONTEXT-DAILY-SOFT-WINDOW-BE-R0: schema + pure logic only.
Does not call models, does not generate handoffs, does not enable itself.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
from typing import Any, Callable, Optional

# Reuse 04:00 chat-day math from day_handoff (same Asia/Shanghai contract).
from chat.day_handoff import (
    CHAT_DAY_START_HOUR,
    TZ_OFFSET_HOURS,
    chat_day_for_timestamp,
    chat_day_window,
    validate_day_string,
)

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

ALLOWED_CARRYOVER_COUNTS = frozenset({0, 3, 5, 10})

# Centralized legal transitions.
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_ABSENT: frozenset({STATUS_COMPACTING, STATUS_PROVISIONAL}),
    STATUS_COMPACTING: frozenset({STATUS_PROVISIONAL, STATUS_FAILED_RETRYABLE}),
    STATUS_FAILED_RETRYABLE: frozenset({STATUS_PROVISIONAL, STATUS_COMPACTING}),
    STATUS_PROVISIONAL: frozenset({STATUS_FINALIZED}),
    STATUS_FINALIZED: frozenset(),
}

_USER_AUTHORS = frozenset({'hayana', 'haya', 'user'})
_ASSISTANT_AUTHORS = frozenset({'fyodor', 'claude', 'assistant'})
_ELIGIBLE_AUTHORS = _USER_AUTHORS | _ASSISTANT_AUTHORS
_EXCLUDED_AUTHORS = frozenset({'system', 'tool', 'tool_result'})

_SAVE_RE = re.compile(r'\[\[SAVE(?::[^\]]+)?\]\]', re.IGNORECASE)
_WAKE_MARKERS = (
    '【唤醒】', '【梦境】', '【反馈】', '[WAKE]', '[DREAM]', '[FEEDBACK]',
)

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


def ensure_schema(db_path: Optional[str] = None) -> None:
    conn = _connect(db_path)
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
        conn.commit()
    finally:
        conn.close()


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
) -> dict[str, Any]:
    """Idempotent create for (chat_id, local_day). Uses BEGIN IMMEDIATE."""
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
        cur = conn.execute(
            '''INSERT INTO daily_contexts (
                chat_id, local_day, timezone, boundary_hour, context_epoch,
                boundary_message_id, status, carryover_count,
                resident_generation, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, 1, ?, ?)''',
            (
                chat_id, local_day, DEFAULT_TIMEZONE, CHAT_DAY_START_HOUR,
                epoch, boundary_id, STATUS_PROVISIONAL, now_s, now_s,
            ),
        )
        conn.commit()
        created = get_daily_context(conn, chat_id=chat_id, local_day=local_day)
        assert created is not None
        created['_inserted_id'] = cur.lastrowid
        return created
    except sqlite3.IntegrityError:
        conn.rollback()
        # Concurrent insert won the race — return the winner.
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

        if current['status'] == STATUS_ABSENT:
            assert_transition(STATUS_ABSENT, STATUS_COMPACTING)
        elif current['status'] == STATUS_FAILED_RETRYABLE:
            assert_transition(STATUS_FAILED_RETRYABLE, STATUS_COMPACTING)
        elif current['status'] == STATUS_COMPACTING:
            pass  # reclaim expired / same owner
        else:
            raise InvalidTransitionError(
                'cannot acquire lease from status %s' % current['status']
            )

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
    if failed:
        return transition_status(
            context_id, STATUS_FAILED_RETRYABLE, db_path=db_path,
            extra={'lease_owner': None, 'lease_expires_at': None},
        )
    # FAILED_RETRYABLE or COMPACTING → PROVISIONAL
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    if ctx['status'] == STATUS_FAILED_RETRYABLE:
        return transition_status(
            context_id, STATUS_PROVISIONAL, db_path=db_path,
            extra={
                'handoff_id': handoff_id,
                'lease_owner': None,
                'lease_expires_at': None,
            },
        )
    return transition_status(
        context_id, STATUS_PROVISIONAL, db_path=db_path,
        extra={
            'handoff_id': handoff_id,
            'lease_owner': None,
            'lease_expires_at': None,
        },
    )


def _is_eligible_row(row: Any) -> bool:
    author = str(row['author'] or '').strip().lower()
    if author in _EXCLUDED_AUTHORS:
        return False
    if author not in _ELIGIBLE_AUTHORS:
        return False
    content = str(row['content'] or '')
    if not content.strip():
        return False
    if _SAVE_RE.search(content):
        return False
    if any(m in content for m in _WAKE_MARKERS):
        return False
    # Tool payloads often live in tool_calls column when present.
    if hasattr(row, 'keys') and 'tool_calls' in row.keys():
        tc = row['tool_calls']
        if tc and str(tc).strip() not in ('', 'null', '[]', '{}'):
            # Assistant text with tool_calls metadata is still eligible if content is normal text;
            # pure tool-result rows typically have empty-ish content — already filtered.
            pass
    return True


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
    # Previous chat day window.
    day_dt = datetime.datetime.strptime(local_day, '%Y-%m-%d')
    prev_day = (day_dt - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    _d, start_at, _end, next_start = chat_day_window(prev_day)
    boundary = int(ctx['boundary_message_id'] or 0)

    conn = _connect(db_path)
    try:
        # Prefer tool_calls column if present.
        cols = {r[1] for r in conn.execute('PRAGMA table_info(chat_messages)').fetchall()}
        select_cols = 'id, author, content, created_at'
        if 'tool_calls' in cols:
            select_cols += ', tool_calls'
        rows = conn.execute(
            'SELECT %s FROM chat_messages '
            'WHERE created_at >= ? AND created_at < ? AND id <= ? '
            'ORDER BY id ASC' % select_cols,
            (start_at, next_start, boundary if boundary > 0 else 10**18),
        ).fetchall()
        eligible = [r for r in rows if _is_eligible_row(r)]
        # Last `limit` eligible, but return in chronological order.
        tail = eligible[-limit:] if limit else []
        out = []
        for r in tail:
            content = str(r['content'] or '')
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


def _selection_locked(ctx: dict[str, Any], db_path: Optional[str] = None) -> bool:
    if ctx.get('selection_finalized_at'):
        return True
    if ctx.get('status') == STATUS_FINALIZED:
        return True
    # First user formal message of current day locks selection.
    local_day = ctx['local_day']
    _d, start_at, _end, next_start = chat_day_window(local_day)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, author FROM chat_messages "
            "WHERE created_at >= ? AND created_at < ? ORDER BY id ASC LIMIT 20",
            (start_at, next_start),
        ).fetchall()
        for r in row:
            if str(r['author'] or '').lower() in _USER_AUTHORS:
                return True
        return False
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

    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    if _selection_locked(ctx, db_path=db_path):
        raise ConflictError('carryover selection is locked')

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
        if current.get('selection_finalized_at') or current['status'] == STATUS_FINALIZED:
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


def maybe_auto_finalize_zero_on_first_user_message(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """If provisional and unlocked when first user message arrives, finalize 0."""
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        return None
    if ctx.get('selection_finalized_at') or ctx['status'] == STATUS_FINALIZED:
        return None
    if ctx['status'] != STATUS_PROVISIONAL:
        return None
    return finalize_zero_carryover(context_id, db_path=db_path)


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
        placeholders = ','.join('?' * len(ids))
        rows = conn.execute(
            'SELECT id, author, content, created_at FROM chat_messages '
            'WHERE id IN (%s)' % placeholders,
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
                'content': str(r['content'] or ''),
                'created_at': str(r['created_at'] or ''),
            })
        return out
    finally:
        conn.close()


# ── day_handoff storage / validation (no LLM generation) ──────────────


def validate_formal_handoff_content(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ['handoff content must be a mapping']
    for key in HANDOFF_CONTENT_KEYS:
        if key not in data:
            errors.append('missing key: %s' % key)
    unknown = set(data.keys()) - set(HANDOFF_CONTENT_KEYS)
    if unknown:
        errors.append('unknown keys: %s' % ', '.join(sorted(unknown)))

    for key in (
        'topics', 'confirmed_facts', 'decisions',
        'open_loops', 'explicit_user_requests',
    ):
        val = data.get(key)
        if val is None:
            continue
        if not isinstance(val, list):
            errors.append('%s must be a list' % key)
            continue
        for i, item in enumerate(val):
            if not isinstance(item, str):
                errors.append('%s[%d] must be string' % (key, i))
                continue
            for marker in _FORBIDDEN_HANDOFF_MARKERS:
                if marker in item:
                    errors.append('%s[%d]: forbidden marker %r' % (key, i, marker))
                    break
            if re.search(r'[「『""].{8,}?[」』""]', item):
                errors.append('%s[%d]: contains long quoted speech' % (key, i))

    last = str(data.get('last_topic') or '')
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
    if status == HANDOFF_READY:
        errors = validate_formal_handoff_content(content)
        if errors:
            raise ValueError('invalid handoff content: ' + '; '.join(errors))
    payload = json.dumps(content, ensure_ascii=False) if content is not None else None
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT * FROM day_handoffs WHERE chat_id=? AND source_day=? AND source_sha256=?',
            (chat_id, source_day, source_sha256),
        ).fetchone()
        if existing:
            conn.commit()
            return _row_to_dict(existing) or {}
        cur = conn.execute(
            '''INSERT INTO day_handoffs (
                chat_id, source_day, source_epoch, boundary_message_id,
                source_first_message_id, source_last_message_id, source_message_count,
                source_sha256, schema_version, status, content_json, error_code
            ) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)''',
            (
                chat_id, source_day, source_epoch, boundary_message_id,
                source_first_message_id, source_last_message_id, source_message_count,
                source_sha256, status, payload, error_code,
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
            (chat_id, source_day, source_sha256),
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


# ── epoch / generation fence ───────────────────────────────────────────


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
    writer: Callable[[], Any],
    *,
    db_path: Optional[str] = None,
) -> tuple[bool, Any]:
    """Run writer only if token still matches current daily_context."""
    if not is_epoch_current(token, db_path=db_path):
        return False, None
    return True, writer()


def retire_resident_for_rollover(
    context_id: int,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Mark rollover by bumping generation on a new epoch context (storage only)."""
    ctx = get_daily_context_by_id(context_id, db_path=db_path)
    if not ctx:
        raise DailyContextError('daily_context not found')
    # New day contexts start at generation 1; retiring old is a no-op on storage.
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
    """Increment resident_generation for same-epoch respawn."""
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
    """CAS: only set if currently NULL (no duplicate morning greeting)."""
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
        # Look for previous day handoff readiness (informational).
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
