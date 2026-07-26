"""Mode-keyed rolling summary storage."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

MODES = ('legacy_block', 'relay_hysteresis', 'cc_token_budget')
_LEGACY_MODE = 'legacy_block'


def _db_path() -> str:
    try:
        import config_store
        return config_store.DB_PATH
    except Exception:
        return '/opt/frontend/memories.db'


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS rolling_summaries (
        mode TEXT PRIMARY KEY,
        summary TEXT DEFAULT '',
        up_to_id INTEGER DEFAULT 0,
        oldest_retained_id INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        updated_at DATETIME
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS rolling_summary (
        id INTEGER PRIMARY KEY CHECK (id=1),
        summary TEXT DEFAULT '',
        up_to_id INTEGER DEFAULT 0,
        msg_count INTEGER DEFAULT 0,
        updated_at DATETIME
    )''')
    conn.commit()
    row = conn.execute('SELECT summary, up_to_id, msg_count FROM rolling_summary WHERE id=1').fetchone()
    if row and (row[0] or '').strip():
        existing = conn.execute(
            'SELECT 1 FROM rolling_summaries WHERE mode=?', (_LEGACY_MODE,)
        ).fetchone()
        if not existing:
            conn.execute(
                'INSERT INTO rolling_summaries (mode, summary, up_to_id, msg_count, updated_at) '
                'VALUES (?, ?, ?, ?, datetime(\'now\',\'+8 hours\'))',
                (_LEGACY_MODE, row[0], int(row[1] or 0), int(row[2] or 0)),
            )
            conn.commit()


def get_summary(mode: str) -> dict[str, Any]:
    mode = str(mode or _LEGACY_MODE)
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        ensure_tables(conn)
        row = conn.execute(
            'SELECT summary, up_to_id, oldest_retained_id, msg_count FROM rolling_summaries WHERE mode=?',
            (mode,),
        ).fetchone()
        if row:
            return {
                'summary': (row['summary'] or '').strip(),
                'up_to_id': int(row['up_to_id'] or 0),
                'oldest_retained_id': int(row['oldest_retained_id'] or 0),
                'msg_count': int(row['msg_count'] or 0),
            }
        if mode == _LEGACY_MODE:
            legacy = conn.execute(
                'SELECT summary, up_to_id, msg_count FROM rolling_summary WHERE id=1'
            ).fetchone()
            if legacy:
                return {
                    'summary': (legacy['summary'] or '').strip(),
                    'up_to_id': int(legacy['up_to_id'] or 0),
                    'oldest_retained_id': 0,
                    'msg_count': int(legacy['msg_count'] or 0),
                }
    finally:
        conn.close()
    return {'summary': '', 'up_to_id': 0, 'oldest_retained_id': 0, 'msg_count': 0}


def save_summary(
    mode: str,
    *,
    summary: str,
    up_to_id: int,
    oldest_retained_id: int = 0,
    msg_count: int = 0,
) -> None:
    mode = str(mode or _LEGACY_MODE)
    conn = sqlite3.connect(_db_path(), timeout=10)
    try:
        ensure_tables(conn)
        conn.execute(
            'INSERT INTO rolling_summaries (mode, summary, up_to_id, oldest_retained_id, msg_count, updated_at) '
            'VALUES (?, ?, ?, ?, ?, datetime(\'now\',\'+8 hours\')) '
            'ON CONFLICT(mode) DO UPDATE SET summary=excluded.summary, up_to_id=excluded.up_to_id, '
            'oldest_retained_id=excluded.oldest_retained_id, msg_count=excluded.msg_count, '
            'updated_at=excluded.updated_at',
            (mode, summary, int(up_to_id or 0), int(oldest_retained_id or 0), int(msg_count or 0)),
        )
        if mode == _LEGACY_MODE:
            conn.execute(
                'INSERT INTO rolling_summary (id, summary, up_to_id, msg_count, updated_at) '
                'VALUES (1, ?, ?, ?, datetime(\'now\',\'+8 hours\')) '
                'ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, up_to_id=excluded.up_to_id, '
                'msg_count=excluded.msg_count, updated_at=excluded.updated_at',
                (summary, int(up_to_id or 0), int(msg_count or 0)),
            )
        conn.commit()
    finally:
        conn.close()


def clear_summary(mode: str) -> None:
    save_summary(mode, summary='', up_to_id=0, oldest_retained_id=0, msg_count=0)
