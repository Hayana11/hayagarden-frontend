"""Compatibility adapter for the shared Diary product handler."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Any

from tools.product_handlers import write_diary_row as _write_diary_row


_DEFAULT_DB_PATH = "/opt/frontend/memories.db"


def write_diary_row(
    conn: sqlite3.Connection,
    content: str,
) -> dict[str, Any]:
    return _write_diary_row(conn, content)


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
