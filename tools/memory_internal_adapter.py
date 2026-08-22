"""Internal Memory read-only shadow adapter for M3-04B.

This module owns only provider transport shaping. Memory search semantics,
ordering, filtering, and read-only behavior remain in the shared
tools.product_handlers.search_memory_posts handler.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from tools.product_handlers import ProductHandlerError, search_memory_posts


@contextmanager
def open_memory_db(db_path: str) -> Iterator[sqlite3.Connection]:
    path = str(db_path or "").strip()
    if not path:
        raise ValueError("explicit db_path is required")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def search_memories(
    db_path: str,
    *,
    keyword: Any = "",
) -> dict[str, Any]:
    with open_memory_db(db_path) as conn:
        return {"posts": search_memory_posts(conn, keyword=keyword, limit=8)}


def _main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("operation") != "search_memories":
        raise ValueError("unknown internal Memory operation")
    result = search_memories(
        payload.get("db_path"),
        keyword=payload.get("keyword", ""),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except ProductHandlerError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2)
