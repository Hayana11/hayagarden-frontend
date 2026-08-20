"""Atomic, model-free diary persistence for the narrow write_diary provider."""
from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys
from typing import Any


_BEIJING = _dt.timezone(_dt.timedelta(hours=8))
_DEFAULT_DB_PATH = "/opt/frontend/memories.db"


def _day_bounds(now: _dt.datetime | None = None) -> tuple[str, str]:
    current = now or _dt.datetime.now(_BEIJING)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BEIJING)
    current = current.astimezone(_BEIJING)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + _dt.timedelta(days=1)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def write_diary_once(
    conn: sqlite3.Connection,
    content: str,
    *,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """Insert one Fyodor diary for the Beijing calendar day, or report duplicate.

    The caller supplies only the diary body.  Fixed product fields are owned by
    this function and the duplicate check plus insert share one write txn.
    """
    if not isinstance(content, str) or not content.strip():
        return {"status": "INVALID_CONTENT"}

    text = content.strip()
    day_start, day_end = _day_bounds(now)
    try:
        conn.execute("BEGIN IMMEDIATE")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "processed" not in columns:
            raise RuntimeError("posts.processed column is required")

        existing = conn.execute(
            "SELECT id FROM posts "
            "WHERE type='DIARY' AND author='fyodor' "
            "AND created_at >= ? AND created_at < ? LIMIT 1",
            (day_start, day_end),
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return {"status": "ALREADY_EXISTS"}

        cursor = conn.execute(
            "INSERT INTO posts (type, content, layer, author, processed) "
            "VALUES (?, ?, ?, ?, ?)",
            ("DIARY", text, "recent", "fyodor", 0),
        )
        conn.commit()
        return {"status": "CREATED", "id": int(cursor.lastrowid)}
    except Exception:
        conn.rollback()
        raise


def write_diary(content: str, *, db_path: str | None = None) -> dict[str, Any]:
    path = db_path or os.environ.get("DIARY_DB_PATH") or _DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    try:
        return write_diary_once(conn, content)
    finally:
        conn.close()


def main() -> int:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or set(payload) != {"content"}:
        raise ValueError("only content is accepted")
    result = write_diary(str(payload["content"]))
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
