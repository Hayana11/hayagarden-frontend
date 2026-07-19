"""SQLite persistence for monopoly rooms, game events, live events and messages."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


DEFAULT_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_schema(db_path: str | None = None) -> None:
    # WAL lets the SSE readers coexist with short BEGIN IMMEDIATE writes.  It
    # is database-persistent, so set it once during service initialization.
    pragma_conn = _connect(db_path)
    try:
        pragma_conn.execute("PRAGMA journal_mode=WAL")
        pragma_conn.execute("PRAGMA synchronous=NORMAL")
    finally:
        pragma_conn.close()
    with transaction(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS monopoly_rooms (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                game_id TEXT,
                player_token_cipher TEXT,
                pair_code TEXT,
                seats_json TEXT NOT NULL,
                active_actor TEXT,
                pending_json TEXT NOT NULL DEFAULT '{}',
                state_json TEXT NOT NULL DEFAULT '{}',
                agent_state_json TEXT NOT NULL DEFAULT '{}',
                event_seq INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE TABLE IF NOT EXISTS monopoly_room_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL REFERENCES monopoly_rooms(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL,
                type TEXT NOT NULL,
                actor TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours')),
                UNIQUE(room_id, seq)
            );
            CREATE INDEX IF NOT EXISTS idx_monopoly_events_room_seq
                ON monopoly_room_events(room_id, seq);
            CREATE TABLE IF NOT EXISTS monopoly_room_live_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL REFERENCES monopoly_rooms(id) ON DELETE CASCADE,
                type TEXT NOT NULL,
                actor TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE INDEX IF NOT EXISTS idx_monopoly_live_events_room_id
                ON monopoly_room_live_events(room_id, id);
            CREATE TABLE IF NOT EXISTS monopoly_room_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL REFERENCES monopoly_rooms(id) ON DELETE CASCADE,
                author TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT NOT NULL DEFAULT '',
                reply_to INTEGER,
                game_event_id INTEGER,
                provider_meta TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+8 hours'))
            );
            CREATE INDEX IF NOT EXISTS idx_monopoly_messages_room_id
                ON monopoly_room_messages(room_id, id);
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(monopoly_rooms)")}
        if "agent_state_json" not in columns:
            conn.execute("ALTER TABLE monopoly_rooms ADD COLUMN agent_state_json TEXT NOT NULL DEFAULT '{}'")


def _json_load(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def room_dict(row: sqlite3.Row | dict | None, *, include_token: bool = False) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["seats"] = _json_load(out.pop("seats_json", "{}"), {})
    out["pending"] = _json_load(out.pop("pending_json", "{}"), {}) or None
    out["state"] = _json_load(out.pop("state_json", "{}"), {})
    out.pop("agent_state_json", None)
    if not include_token:
        out.pop("player_token_cipher", None)
    return out


def get_room(room_id: str, db_path: str | None = None, *, include_token: bool = False) -> dict | None:
    conn = _connect(db_path)
    try:
        return room_dict(
            conn.execute("SELECT * FROM monopoly_rooms WHERE id=?", (room_id,)).fetchone(),
            include_token=include_token,
        )
    finally:
        conn.close()


def create_room(room_id: str, mode: str, seats: dict, pair_code: str, db_path: str | None = None) -> dict:
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO monopoly_rooms (id, mode, status, pair_code, seats_json) VALUES (?, ?, 'lobby', ?, ?)",
            (room_id, mode, pair_code, json.dumps(seats, ensure_ascii=False, separators=(",", ":"))),
        )
    return get_room(room_id, db_path)  # type: ignore[return-value]


def update_room(conn: sqlite3.Connection, room_id: str, **fields) -> None:
    allowed = {
        "status", "game_id", "player_token_cipher", "pair_code", "seats_json",
        "active_actor", "pending_json", "state_json", "event_seq",
        "agent_state_json",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown room fields: {sorted(unknown)}")
    if not fields:
        return
    fields["updated_at"] = None
    assignments = []
    values = []
    for key, value in fields.items():
        if key == "updated_at":
            assignments.append("updated_at=datetime('now', '+8 hours')")
        else:
            assignments.append(f"{key}=?")
            values.append(value)
    values.append(room_id)
    conn.execute(f"UPDATE monopoly_rooms SET {', '.join(assignments)} WHERE id=?", values)


def append_event(
    conn: sqlite3.Connection,
    room_id: str,
    event_type: str,
    actor: str | None,
    payload: dict,
) -> dict:
    row = conn.execute("SELECT event_seq FROM monopoly_rooms WHERE id=?", (room_id,)).fetchone()
    if row is None:
        raise KeyError(room_id)
    seq = int(row["event_seq"]) + 1
    cur = conn.execute(
        "INSERT INTO monopoly_room_events (room_id, seq, type, actor, payload) VALUES (?, ?, ?, ?, ?)",
        (room_id, seq, event_type, actor, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
    )
    update_room(conn, room_id, event_seq=seq)
    saved = conn.execute("SELECT * FROM monopoly_room_events WHERE id=?", (cur.lastrowid,)).fetchone()
    return event_dict(saved)


def event_dict(row: sqlite3.Row | dict) -> dict:
    out = dict(row)
    out["payload"] = _json_load(out["payload"], {})
    return out


def live_event_dict(row: sqlite3.Row | dict) -> dict:
    out = dict(row)
    out["payload"] = _json_load(out["payload"], {})
    return out


def append_live_event(
    room_id: str,
    event_type: str,
    actor: str | None,
    payload: dict,
    db_path: str | None = None,
) -> dict:
    """Append an SSE-only event without advancing the game's optimistic-lock seq."""
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO monopoly_room_live_events (room_id, type, actor, payload) VALUES (?, ?, ?, ?)",
            (room_id, event_type, actor, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        )
        saved = conn.execute(
            "SELECT * FROM monopoly_room_live_events WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return live_event_dict(saved)


def latest_live_event_id(room_id: str, db_path: str | None = None) -> int:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM monopoly_room_live_events WHERE room_id=?",
            (room_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def prune_live_events(room_id: str, *, keep: int = 1000, db_path: str | None = None) -> None:
    """Bound transient typing history; completed messages remain in their own table."""
    with transaction(db_path) as conn:
        cutoff = conn.execute(
            "SELECT id FROM monopoly_room_live_events WHERE room_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (room_id, max(int(keep), 1) - 1),
        ).fetchone()
        if cutoff:
            conn.execute(
                "DELETE FROM monopoly_room_live_events WHERE room_id=? AND id<?",
                (room_id, int(cutoff[0])),
            )


def list_events(room_id: str, *, after: int = 0, limit: int = 500, db_path: str | None = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM monopoly_room_events WHERE room_id=? AND seq>? ORDER BY seq LIMIT ?",
            (room_id, max(int(after), 0), min(max(int(limit), 1), 1000)),
        ).fetchall()
        return [event_dict(row) for row in rows]
    finally:
        conn.close()


def add_message(
    room_id: str,
    author: str,
    content: str,
    *,
    thinking: str = "",
    reply_to: int | None = None,
    game_event_id: int | None = None,
    provider_meta: dict | str | None = None,
    db_path: str | None = None,
) -> dict:
    if author not in {"haya", "cc", "codex", "system"}:
        raise ValueError("invalid monopoly message author")
    content = (content or "").strip()
    if not content:
        raise ValueError("message content is empty")
    meta = provider_meta if isinstance(provider_meta, str) else json.dumps(provider_meta or {}, ensure_ascii=False)
    with transaction(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO monopoly_room_messages "
            "(room_id, author, content, thinking, reply_to, game_event_id, provider_meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room_id, author, content, thinking or "", reply_to, game_event_id, meta),
        )
        row = conn.execute("SELECT * FROM monopoly_room_messages WHERE id=?", (cur.lastrowid,)).fetchone()
        return message_dict(row)


def message_dict(row: sqlite3.Row | dict) -> dict:
    out = dict(row)
    out["provider_meta"] = _json_load(out.get("provider_meta"), {})
    return out


def list_messages(
    room_id: str,
    *,
    after_id: int = 0,
    limit: int = 120,
    db_path: str | None = None,
) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM monopoly_room_messages WHERE room_id=? AND id>? ORDER BY id LIMIT ?",
            (room_id, max(int(after_id), 0), min(max(int(limit), 1), 300)),
        ).fetchall()
        return [message_dict(row) for row in rows]
    finally:
        conn.close()


def poll_room_stream(
    room_id: str,
    *,
    after_seq: int,
    after_message_id: int,
    after_live_id: int = 0,
    event_limit: int = 500,
    message_limit: int = 120,
    live_limit: int = 500,
    db_path: str | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Read the game, message and transient SSE queues with one connection."""
    conn = _connect(db_path)
    try:
        event_rows = conn.execute(
            "SELECT * FROM monopoly_room_events WHERE room_id=? AND seq>? ORDER BY seq LIMIT ?",
            (room_id, max(int(after_seq), 0), min(max(int(event_limit), 1), 1000)),
        ).fetchall()
        message_rows = conn.execute(
            "SELECT * FROM monopoly_room_messages WHERE room_id=? AND id>? ORDER BY id LIMIT ?",
            (room_id, max(int(after_message_id), 0), min(max(int(message_limit), 1), 300)),
        ).fetchall()
        live_rows = conn.execute(
            "SELECT * FROM monopoly_room_live_events WHERE room_id=? AND id>? ORDER BY id LIMIT ?",
            (room_id, max(int(after_live_id), 0), min(max(int(live_limit), 1), 1000)),
        ).fetchall()
        return (
            [event_dict(row) for row in event_rows],
            [message_dict(row) for row in message_rows],
            [live_event_dict(row) for row in live_rows],
        )
    finally:
        conn.close()


def recent_messages(room_id: str, *, limit: int = 120, db_path: str | None = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM monopoly_room_messages WHERE room_id=? ORDER BY id DESC LIMIT ?",
            (room_id, min(max(int(limit), 1), 300)),
        ).fetchall()
        return [message_dict(row) for row in reversed(rows)]
    finally:
        conn.close()


def delete_room(room_id: str, db_path: str | None = None) -> bool:
    with transaction(db_path) as conn:
        cur = conn.execute("DELETE FROM monopoly_rooms WHERE id=?", (room_id,))
        return bool(cur.rowcount)


def get_agent_state(room_id: str, db_path: str | None = None) -> dict:
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT agent_state_json FROM monopoly_rooms WHERE id=?", (room_id,)).fetchone()
        return _json_load(row[0], {}) if row else {}
    finally:
        conn.close()


def save_agent_state(room_id: str, value: dict, db_path: str | None = None) -> None:
    with transaction(db_path) as conn:
        update_room(conn, room_id, agent_state_json=json.dumps(value, ensure_ascii=False))

