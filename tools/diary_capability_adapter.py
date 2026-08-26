"""Provider-neutral Capability adapter for diary.write.

The adapter owns only stdin/SQLite transport shaping. Diary validation,
transaction boundaries, and row semantics remain in tools.product_handlers.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any

from tools.product_handlers import write_diary_row


def write_diary(db_path: str, *, content: Any) -> dict[str, Any]:
    path = str(db_path or "").strip()
    if not path:
        raise ValueError("db_path is required")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return write_diary_row(conn, content)
    finally:
        conn.close()


def _main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if payload.get("operation") != "write_diary":
        raise ValueError("operation must be write_diary")
    db_path = payload.get("db_path")
    if not isinstance(db_path, str) or not db_path.strip():
        raise ValueError("db_path is required")
    result = write_diary(db_path, content=payload.get("content"))
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
