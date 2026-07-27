"""Epoch-fenced write helpers — structured SQL operations only."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Optional, Sequence

Operation = tuple[str, tuple | list]

_FORBIDDEN_SQL = re.compile(
    r'^\s*(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH|VACUUM|PRAGMA\b)',
    re.IGNORECASE,
)

_CURRENT_ROW_SQL = (
    'SELECT id, context_epoch, resident_generation FROM daily_contexts '
    'WHERE chat_id=? AND is_backfill=0 ORDER BY context_epoch DESC LIMIT 1'
)


class EpochFenceError(Exception):
    """Epoch fence contract violation."""


def _validate_sql(sql: str) -> None:
    head = str(sql or '').strip()
    if not head:
        raise EpochFenceError('empty SQL')
    stripped = head.rstrip(';').rstrip()
    if ';' in stripped:
        raise EpochFenceError('epoch-fenced writer forbids multi-statement SQL')
    if _FORBIDDEN_SQL.match(head):
        raise EpochFenceError('epoch-fenced writer forbids transaction control SQL')
    if 'executescript' in head.lower():
        raise EpochFenceError('epoch-fenced writer forbids executescript')


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
