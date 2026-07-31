"""Epoch-fenced write helpers — structured SQL operations only."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Optional, Sequence

Operation = tuple[str, tuple | list]

_ALLOWED_KEYWORDS = frozenset({'INSERT', 'UPDATE', 'DELETE', 'REPLACE'})
_FIRST_KEYWORD_RE = re.compile(r'^([A-Za-z_]+)')

# Formal current only — never treat provisional manual_staged prepare rows as current.
_CURRENT_ROW_SQL = (
    'SELECT id, context_epoch, resident_generation FROM daily_contexts '
    "WHERE chat_id=? AND is_backfill=0 AND window_mode != 'manual_staged' "
    'ORDER BY context_epoch DESC LIMIT 1'
)


class EpochFenceError(Exception):
    """Epoch fence contract violation."""


def _strip_leading_sql_comments(sql: str) -> str:
    """Remove consecutive leading -- line comments and /* */ block comments."""
    s = str(sql or '')
    while True:
        s = s.lstrip()
        if not s:
            return ''
        if s.startswith('/*'):
            end = s.find('*/', 2)
            if end < 0:
                return ''
            s = s[end + 2:]
            continue
        if s.startswith('--'):
            nl = s.find('\n')
            if nl < 0:
                return ''
            s = s[nl + 1:]
            continue
        break
    return s.lstrip()


def _sql_first_keyword(sql: str) -> str:
    stripped = _strip_leading_sql_comments(sql)
    if not stripped:
        return ''
    match = _FIRST_KEYWORD_RE.match(stripped)
    return match.group(1).upper() if match else ''


def _validate_sql(sql: str) -> None:
    raw = str(sql or '').strip()
    if not raw:
        raise EpochFenceError('empty SQL')
    body = raw.rstrip(';').rstrip()
    if ';' in body:
        raise EpochFenceError('epoch-fenced writer forbids multi-statement SQL')
    if 'executescript' in raw.lower():
        raise EpochFenceError('epoch-fenced writer forbids executescript')
    keyword = _sql_first_keyword(raw)
    if not keyword:
        raise EpochFenceError('empty SQL after comment stripping')
    if keyword not in _ALLOWED_KEYWORDS:
        raise EpochFenceError(
            'epoch-fenced writer only allows INSERT/UPDATE/DELETE/REPLACE',
        )


def _execute_operations(conn: sqlite3.Connection, operations: Sequence[Operation]) -> int:
    total = 0
    for sql, params in operations:
        _validate_sql(sql)
        cur = conn.execute(sql, params or ())
        total += int(cur.rowcount or 0)
    return total


def commit_if_epoch_current(
    token: dict[str, Any],
    operations: Sequence[Operation],
    *,
    connect_fn: Callable[[Optional[str]], sqlite3.Connection],
    db_path: Optional[str] = None,
) -> tuple[bool, int]:
    """Run validated SQL operations inside BEGIN IMMEDIATE with epoch/generation CAS."""
    from chat.daily_context import DEFAULT_CHAT_ID

    if int(token.get('is_backfill') or 0):
        return False, 0
    chat_id = str(token.get('chat_id') or DEFAULT_CHAT_ID)
    want_epoch = int(token.get('context_epoch') or -1)
    want_gen = int(token.get('resident_generation') or -1)
    conn = connect_fn(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(_CURRENT_ROW_SQL, (chat_id,)).fetchone()
        if row is None:
            conn.rollback()
            return False, 0
        if int(row['context_epoch']) != want_epoch or int(row['resident_generation']) != want_gen:
            conn.rollback()
            return False, 0
        affected = _execute_operations(conn, operations)
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
            return False, 0
        current = conn.execute(_CURRENT_ROW_SQL, (chat_id,)).fetchone()
        if (
            current is None
            or int(current['context_epoch']) != want_epoch
            or int(current['resident_generation']) != want_gen
        ):
            conn.rollback()
            return False, 0
        conn.commit()
        return True, affected
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
