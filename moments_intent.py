"""Shared pending chat-collection intents keyed by turn_key (cross-process safe)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingChatCollection:
    previous_turns: int
    caption: str


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def set_pending(
    memories_db_path: str,
    turn_key: str,
    *,
    previous_turns: int,
    caption: str,
) -> None:
    turns = int(previous_turns)
    if turns < 0 or turns > 2:
        raise ValueError('previous_turns must be between 0 and 2')
    text = (caption or '').strip()
    if len(text) > 500:
        raise ValueError('caption too long')
    key = (turn_key or '').strip()
    if not key:
        raise ValueError('turn_key required')
    conn = _conn(memories_db_path)
    try:
        conn.execute(
            '''INSERT INTO moments_pending_intent (turn_key, previous_turns, caption)
               VALUES (?, ?, ?)
               ON CONFLICT(turn_key) DO UPDATE SET
                 previous_turns=excluded.previous_turns,
                 caption=excluded.caption''',
            (key, turns, text),
        )
        conn.commit()
    finally:
        conn.close()


def pop_pending(memories_db_path: str, turn_key: str) -> PendingChatCollection | None:
    key = (turn_key or '').strip()
    if not key:
        return None
    conn = _conn(memories_db_path)
    try:
        row = conn.execute(
            'SELECT previous_turns, caption FROM moments_pending_intent WHERE turn_key=?',
            (key,),
        ).fetchone()
        if not row:
            return None
        conn.execute('DELETE FROM moments_pending_intent WHERE turn_key=?', (key,))
        conn.commit()
        return PendingChatCollection(
            previous_turns=int(row['previous_turns']),
            caption=row['caption'] or '',
        )
    finally:
        conn.close()


def clear_pending(memories_db_path: str, turn_key: str) -> None:
    key = (turn_key or '').strip()
    if not key:
        return
    conn = _conn(memories_db_path)
    try:
        conn.execute('DELETE FROM moments_pending_intent WHERE turn_key=?', (key,))
        conn.commit()
    finally:
        conn.close()
