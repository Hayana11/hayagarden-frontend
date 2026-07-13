"""SQLite persistence for the three fixed Haya group-chat timelines.

The ordinary one-to-one chat keeps using ``chat_messages``.  Group chat is
deliberately isolated so a second agent can be added without changing or
polluting the existing conversation history.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable


DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")
VALID_ROOMS = frozenset({"group", "claude", "codex"})
VALID_AUTHORS = frozenset({"user", "claude", "codex", "system"})


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _validate(value: str, allowed: Iterable[str], label: str) -> str:
    value = (value or "").strip().lower()
    if value not in allowed:
        raise ValueError(f"invalid {label}: {value}")
    return value


def ensure_schema(db_path: str | None = None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                author TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                meta TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE INDEX IF NOT EXISTS idx_group_chat_room_id
                ON group_chat_messages(room, id);
            CREATE TABLE IF NOT EXISTS group_chat_threads (
                room TEXT NOT NULL,
                agent TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
                PRIMARY KEY (room, agent)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def add_message(
    room: str,
    author: str,
    content: str,
    *,
    thinking: str = "",
    meta: str = "",
    db_path: str | None = None,
) -> dict:
    room = _validate(room, VALID_ROOMS, "room")
    author = _validate(author, VALID_AUTHORS, "author")
    content = (content or "").strip()
    if not content:
        raise ValueError("message content is empty")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO group_chat_messages (room, author, content, thinking, meta) "
            "VALUES (?, ?, ?, ?, ?)",
            (room, author, content, thinking or "", meta or ""),
        )
        row = conn.execute(
            "SELECT * FROM group_chat_messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def get_message(message_id: int, db_path: str | None = None) -> dict | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM group_chat_messages WHERE id=?", (int(message_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_messages(
    room: str,
    *,
    limit: int = 120,
    before: int | None = None,
    db_path: str | None = None,
) -> tuple[list[dict], bool]:
    room = _validate(room, VALID_ROOMS, "room")
    limit = min(max(int(limit), 1), 200)
    conn = _connect(db_path)
    try:
        if before:
            rows = conn.execute(
                "SELECT * FROM group_chat_messages WHERE room=? AND id<? "
                "ORDER BY id DESC LIMIT ?",
                (room, int(before), limit + 1),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM group_chat_messages WHERE room=? "
                "ORDER BY id DESC LIMIT ?",
                (room, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
        return [dict(row) for row in rows], has_more
    finally:
        conn.close()


def clear_room(room: str, db_path: str | None = None) -> int:
    room = _validate(room, VALID_ROOMS, "room")
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM group_chat_messages WHERE room=?", (room,))
        conn.execute("DELETE FROM group_chat_threads WHERE room=?", (room,))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def get_thread_binding(
    room: str,
    agent: str,
    *,
    db_path: str | None = None,
) -> dict | None:
    room = _validate(room, VALID_ROOMS, "room")
    agent = _validate(agent, {"claude", "codex"}, "agent")
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM group_chat_threads WHERE room=? AND agent=?",
            (room, agent),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_thread_binding(
    room: str,
    agent: str,
    thread_id: str,
    *,
    db_path: str | None = None,
) -> dict:
    room = _validate(room, VALID_ROOMS, "room")
    agent = _validate(agent, {"claude", "codex"}, "agent")
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise ValueError("thread id is empty")
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO group_chat_threads (room, agent, thread_id) VALUES (?, ?, ?) "
            "ON CONFLICT(room, agent) DO UPDATE SET "
            "thread_id=excluded.thread_id, updated_at=datetime('now', '+8 hours')",
            (room, agent, thread_id),
        )
        row = conn.execute(
            "SELECT * FROM group_chat_threads WHERE room=? AND agent=?",
            (room, agent),
        ).fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def delete_thread_binding(
    room: str,
    agent: str,
    *,
    db_path: str | None = None,
) -> bool:
    room = _validate(room, VALID_ROOMS, "room")
    agent = _validate(agent, {"claude", "codex"}, "agent")
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "DELETE FROM group_chat_threads WHERE room=? AND agent=?",
            (room, agent),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()
