"""Internal Todo adapter for the M3-01 shadow provider.

This module owns only provider transport shaping. Todo validation, ordering,
SQL, and transaction boundaries remain in tools.product_handlers.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from tools.product_handlers import ProductHandlerError, create_todo, list_todos


@contextmanager
def open_todo_db(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    path = str(db_path or "").strip()
    if not path:
        raise ValueError("TODO_INTERNAL_DB_PATH is required")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def read_todos(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    with open_todo_db(db_path) as conn:
        return {"todos": list_todos(conn)}


def add_todo(
    db_path: str | os.PathLike[str],
    *,
    content: Any,
    due_date: Any = None,
) -> dict[str, Any]:
    with open_todo_db(db_path) as conn:
        result = create_todo(
            conn,
            content=content,
            due_date=due_date,
            author="fyodor_api",
        )
    # Preserve the existing Home provider's model-visible write contract.
    return {"ok": bool(result.get("ok"))}


def _main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    db_path = payload.get("db_path") or os.environ.get("TODO_INTERNAL_DB_PATH")
    operation = payload.get("operation")
    if operation == "get_todos":
        result = read_todos(db_path)
    elif operation == "add_todo":
        result = add_todo(
            db_path,
            content=payload.get("content"),
            due_date=payload.get("due_date"),
        )
    else:
        raise ValueError("unknown internal Todo operation")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except ProductHandlerError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2)
