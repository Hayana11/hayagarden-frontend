"""Internal Ledger shadow adapter for M3-03A.

This module owns only provider transport shaping. Ledger validation, SQL,
transaction boundaries, and result semantics remain in tools.product_handlers.
The database path is always explicit; this adapter never reads an environment
variable or chooses a default database.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from tools.product_handlers import (
    ProductHandlerError,
    create_ledger,
    read_ledger,
    read_ledger_budget,
)


def _utc_month() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")


def _utc_date() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


@contextmanager
def open_ledger_db(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    path = str(db_path or "").strip()
    if not path:
        raise ValueError("explicit db_path is required")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def get_ledger(
    db_path: str | os.PathLike[str],
    *,
    month: Any = None,
) -> dict[str, Any]:
    resolved_month = month or _utc_month()
    with open_ledger_db(db_path) as conn:
        return read_ledger(conn, month=resolved_month)


def get_ledger_budget(
    db_path: str | os.PathLike[str],
    *,
    month: Any = None,
) -> dict[str, Any]:
    resolved_month = month or _utc_month()
    with open_ledger_db(db_path) as conn:
        return read_ledger_budget(conn, month=resolved_month)


def add_ledger(
    db_path: str | os.PathLike[str],
    *,
    amount: Any,
    category: Any = None,
    note: Any = None,
    date: Any = None,
) -> dict[str, Any]:
    with open_ledger_db(db_path) as conn:
        return create_ledger(
            conn,
            amount=amount,
            category=category or "其他",
            note=note or None,
            date=date or _utc_date(),
            author="fyodor_api",
        )


def _main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    db_path = payload.get("db_path")
    operation = payload.get("operation")
    if operation == "get_ledger":
        result = get_ledger(db_path, month=payload.get("month"))
    elif operation == "get_ledger_budget":
        result = get_ledger_budget(db_path, month=payload.get("month"))
    elif operation == "add_ledger":
        result = add_ledger(
            db_path,
            amount=payload.get("amount"),
            category=payload.get("category"),
            note=payload.get("note"),
            date=payload.get("date"),
        )
    else:
        raise ValueError("unknown internal Ledger operation")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except ProductHandlerError as exc:
        print(json.dumps(exc.payload, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2)
