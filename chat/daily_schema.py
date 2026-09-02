"""Shared Daily Soft Window schema helpers (idempotent, concurrency-safe)."""
from __future__ import annotations

import sqlite3
from typing import Optional

META_SOURCE_KIND_CUTOVER = 'source_kind_cutover_message_id'


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute('PRAGMA table_info(%s)' % table)}


def ensure_chat_messages_display_segments(conn: sqlite3.Connection) -> bool:
    """Add the durable presentation column without dropping or backfilling data."""
    cols = _table_columns(conn, 'chat_messages')
    if not cols or 'display_segments' in cols:
        return False
    try:
        conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN display_segments TEXT DEFAULT ''"
        )
        return True
    except sqlite3.OperationalError as exc:
        if 'duplicate column' in str(exc).lower():
            return False
        raise


def ensure_daily_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS daily_soft_window_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )'''
    )


def get_meta_int(conn: sqlite3.Connection, key: str) -> Optional[int]:
    ensure_daily_meta_table(conn)
    row = conn.execute(
        'SELECT value FROM daily_soft_window_meta WHERE key=?', (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def set_meta_int(conn: sqlite3.Connection, key: str, value: int) -> None:
    ensure_daily_meta_table(conn)
    conn.execute(
        'INSERT INTO daily_soft_window_meta (key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(int(value))),
    )


def ensure_chat_messages_source_kind(
    conn: sqlite3.Connection,
    *,
    record_cutover: bool = True,
) -> bool:
    """Add chat_messages.source_kind if missing. Returns True if column already existed."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        cols = _table_columns(conn, 'chat_messages')
        if not cols:
            conn.commit()
            return True
        if 'source_kind' in cols:
            conn.commit()
            return True
        cutover_id = 0
        if record_cutover:
            row = conn.execute('SELECT MAX(id) AS m FROM chat_messages').fetchone()
            cutover_id = int(row['m'] or 0) if row else 0
        conn.execute(
            "ALTER TABLE chat_messages ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'chat'"
        )
        if record_cutover and cutover_id > 0:
            set_meta_int(conn, META_SOURCE_KIND_CUTOVER, cutover_id)
        conn.commit()
        return False
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if 'duplicate column' in str(exc).lower():
            return True
        raise
    except Exception:
        conn.rollback()
        raise


def ensure_chat_messages_source_kind_logged(
    db_path: str,
    *,
    connect_fn,
    record_cutover: bool = True,
) -> None:
    conn = connect_fn(db_path)
    try:
        ensure_chat_messages_source_kind(conn, record_cutover=record_cutover)
    finally:
        conn.close()
