"""Epoch-fenced write helpers — no raw sqlite connection exposed to writers."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable, Optional

_FORBIDDEN_SQL = re.compile(
    r'^\s*(COMMIT|ROLLBACK|SAVEPOINT|RELEASE|ATTACH|DETACH|VACUUM|PRAGMA\s+)',
    re.IGNORECASE,
)


class EpochFenceError(Exception):
    """Epoch fence contract violation."""


class EpochFencedWriter:
    """Constrained SQL surface for epoch-fenced callbacks."""

    __slots__ = ('_conn',)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters: tuple | list = ()) -> sqlite3.Cursor:
        head = str(sql or '').strip()
        if _FORBIDDEN_SQL.match(head):
            raise EpochFenceError('epoch-fenced writer forbids transaction control SQL')
        if 'executescript' in head.lower():
            raise EpochFenceError('epoch-fenced writer forbids executescript')
        return self._conn.execute(sql, parameters)


def commit_if_epoch_current(
    token: dict[str, Any],
    writer: Callable[[EpochFencedWriter], Any],
    *,
    connect_fn,
    db_path: Optional[str] = None,
) -> tuple[bool, Any]:
    """Run writer inside BEGIN IMMEDIATE with epoch/generation CAS."""
    from chat.daily_context import DEFAULT_CHAT_ID

    chat_id = str(token.get('chat_id') or DEFAULT_CHAT_ID)
    want_epoch = int(token.get('context_epoch') or -1)
    want_gen = int(token.get('resident_generation') or -1)
    conn = connect_fn(db_path)
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
        fenced = EpochFencedWriter(conn)
        result = writer(fenced)
        if not conn.in_transaction:
            raise EpochFenceError(
                'epoch-fenced writer must not commit or rollback',
            )
        row2 = conn.execute(
            'SELECT context_epoch, resident_generation FROM daily_contexts '
            'WHERE chat_id=? ORDER BY context_epoch DESC LIMIT 1',
            (chat_id,),
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
