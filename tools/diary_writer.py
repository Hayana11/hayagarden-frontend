"""Model-free persistence for the narrow write_diary provider."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any


_DEFAULT_DB_PATH = "/opt/frontend/memories.db"


def write_diary_row(
    conn: sqlite3.Connection,
    content: str,
) -> dict[str, Any]:
    """Insert one diary row using only the caller-provided body."""
    if not isinstance(content, str) or not content.strip():
        return {"status": "INVALID_CONTENT"}

    text = content.strip()
    try:
        conn.execute("BEGIN IMMEDIATE")
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(posts)").fetchall()
        }
        if "processed" not in columns:
            raise RuntimeError("posts.processed column is required")

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
        return write_diary_row(conn, content)
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
