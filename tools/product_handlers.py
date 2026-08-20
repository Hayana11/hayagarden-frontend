"""Provider-neutral handlers for the Todo, Ledger, and Diary capabilities.

The handlers own validation, SQL, and transaction boundaries. HTTP routes and
MCP adapters should translate their return values, but must not duplicate the
business rules here.
"""
from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
from typing import Any


_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LEDGER_META_KEYS = {"who", "reason", "note", "mem", "read", "later"}


class ProductHandlerError(ValueError):
    """A client-input error with the legacy API payload preserved."""

    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(payload.get("error", "invalid request"))


def _error(message: str, *, ok: bool = False) -> ProductHandlerError:
    payload: dict[str, Any] = {"error": message}
    if ok:
        payload = {"ok": False, "error": message}
    return ProductHandlerError(payload)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


# -- Todo ---------------------------------------------------------------------

def list_todos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    undone = conn.execute(
        "SELECT * FROM todos WHERE done=0 ORDER BY "
        "CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END, "
        "due_date ASC, id ASC"
    ).fetchall()
    done = conn.execute(
        "SELECT * FROM todos WHERE done=1 ORDER BY id DESC LIMIT 5"
    ).fetchall()
    return [dict(row) for row in undone] + [dict(row) for row in done]


def create_todo(
    conn: sqlite3.Connection,
    *,
    content: Any,
    due_date: Any = None,
    author: Any = None,
) -> dict[str, Any]:
    text = _text(content)
    if not text:
        raise _error("content required")
    due = _text(due_date) or None
    owner = _text(author) or None
    cursor = conn.execute(
        "INSERT INTO todos (content, due_date, author) VALUES (?,?,?)",
        (text, due, owner),
    )
    conn.commit()
    return {"ok": True, "id": int(cursor.lastrowid)}


def toggle_todo(conn: sqlite3.Connection, todo_id: int) -> dict[str, Any]:
    conn.execute("UPDATE todos SET done = 1 - done WHERE id=?", (todo_id,))
    conn.commit()
    return {"ok": True}


def patch_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    *,
    done: Any,
) -> dict[str, Any] | None:
    if done is None:
        raise _error("done required")
    conn.execute(
        "UPDATE todos SET done=? WHERE id=?",
        (1 if bool(done) else 0, todo_id),
    )
    row = conn.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
    conn.commit()
    return dict(row) if row else None


def delete_todo(conn: sqlite3.Connection, todo_id: int) -> dict[str, Any]:
    conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
    conn.commit()
    return {"ok": True}


# -- Ledger -------------------------------------------------------------------

def valid_ledger_month(month: Any) -> bool:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        return False
    return 1 <= int(month[5:7]) <= 12


def valid_ledger_date(date: Any) -> bool:
    if not isinstance(date, str) or not _DATE_RE.match(date):
        return False
    try:
        datetime.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _parse_amount(raw: Any, *, allow_zero: bool = False) -> float:
    if raw is None:
        raise _error("amount required", ok=True)
    try:
        amount = float(raw)
    except (ValueError, TypeError):
        raise _error("invalid amount", ok=True)
    if math.isnan(amount) or math.isinf(amount):
        raise _error("invalid amount", ok=True)
    if not allow_zero and amount == 0:
        raise _error("invalid amount", ok=True)
    return amount


def _parse_budget_amount(raw: Any) -> float:
    if raw is None:
        raise _error("month and amount required", ok=True)
    try:
        amount = float(raw)
    except (ValueError, TypeError):
        raise _error("invalid amount", ok=True)
    if math.isnan(amount) or math.isinf(amount) or amount < 0:
        raise _error("invalid amount", ok=True)
    return amount


def _clean_ledger_meta(meta: Any) -> str | None:
    if not isinstance(meta, dict):
        return None
    cleaned = {
        key: value
        for key, value in meta.items()
        if key in _LEDGER_META_KEYS
        and isinstance(value, str)
        and value.strip()
    }
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def read_ledger(
    conn: sqlite3.Connection,
    *,
    month: Any = "",
) -> dict[str, Any]:
    if month and not valid_ledger_month(month):
        raise _error("invalid month", ok=True)

    if month:
        rows = conn.execute(
            "SELECT * FROM ledger WHERE date LIKE ? ORDER BY date DESC, id DESC",
            (month + "%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM ledger ORDER BY date DESC, id DESC LIMIT 50"
        ).fetchall()

    records = [dict(row) for row in rows]
    income = sum(row["amount"] for row in records if row["amount"] > 0)
    expense = sum(row["amount"] for row in records if row["amount"] < 0)
    previous_expense = 0.0
    if month:
        year, month_number = int(month[:4]), int(month[5:7]) - 1
        if month_number == 0:
            year, month_number = year - 1, 12
        previous_month = f"{year:04d}-{month_number:02d}"
        previous_rows = conn.execute(
            "SELECT amount FROM ledger WHERE date LIKE ? AND amount < 0",
            (previous_month + "%",),
        ).fetchall()
        previous_expense = sum(row["amount"] for row in previous_rows)

    return {
        "records": records,
        "summary": {
            "income": round(income, 2),
            "expense": round(expense, 2),
            "balance": round(income + expense, 2),
            "prev_expense": round(previous_expense, 2),
        },
    }


def create_ledger(
    conn: sqlite3.Connection,
    *,
    amount: Any,
    category: Any = None,
    note: Any = None,
    date: Any = None,
    author: Any = None,
    meta: Any = None,
) -> dict[str, Any]:
    parsed_amount = _parse_amount(amount)
    category_text = _text(category) or None
    note_text = _text(note) or None
    date_text = _text(date) or None
    if date_text is not None and not valid_ledger_date(date_text):
        raise _error("invalid date", ok=True)
    author_text = _text(author) or None
    cursor = conn.execute(
        "INSERT INTO ledger (amount, category, note, date, author, meta) "
        "VALUES (?,?,?,?,?,?)",
        (
            parsed_amount,
            category_text,
            note_text,
            date_text,
            author_text,
            _clean_ledger_meta(meta),
        ),
    )
    conn.commit()
    return {"ok": True, "id": int(cursor.lastrowid)}


def update_ledger(
    conn: sqlite3.Connection,
    ledger_id: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    sets: list[str] = []
    values: list[Any] = []
    if "amount" in data:
        sets.append("amount=?")
        values.append(_parse_amount(data["amount"]))
    for column in ("category", "note", "date", "author"):
        if column not in data:
            continue
        raw = _text(data[column]) or None
        if column == "date" and raw is not None and not valid_ledger_date(raw):
            raise _error("invalid date", ok=True)
        sets.append(f"{column}=?")
        values.append(raw)
    if "meta" in data:
        sets.append("meta=?")
        values.append(_clean_ledger_meta(data["meta"]))
    if not sets:
        raise _error("nothing to update")

    values.append(ledger_id)
    cursor = conn.execute(
        f"UPDATE ledger SET {','.join(sets)} WHERE id=?",
        values,
    )
    conn.commit()
    if not cursor.rowcount:
        raise ProductHandlerError({"ok": False, "error": "ledger entry not found"})
    return {"ok": True}


def delete_ledger(conn: sqlite3.Connection, ledger_id: int) -> dict[str, Any]:
    cursor = conn.execute("DELETE FROM ledger WHERE id=?", (ledger_id,))
    conn.commit()
    if not cursor.rowcount:
        raise ProductHandlerError({"ok": False, "error": "ledger entry not found"})
    return {"ok": True}


def read_ledger_budget(
    conn: sqlite3.Connection,
    *,
    month: Any = "",
) -> dict[str, Any]:
    if not month:
        return {"amount": None}
    if not valid_ledger_month(month):
        raise _error("invalid month", ok=True)
    row = conn.execute(
        "SELECT amount FROM ledger_budget WHERE month=?", (month,)
    ).fetchone()
    return {"amount": row["amount"] if row else None}


def write_ledger_budget(
    conn: sqlite3.Connection,
    *,
    month: Any,
    amount: Any,
) -> dict[str, Any]:
    month_text = _text(month)
    if not month_text or amount is None:
        raise _error("month and amount required")
    if not valid_ledger_month(month_text):
        raise _error("invalid month", ok=True)
    parsed_amount = _parse_budget_amount(amount)
    conn.execute(
        "INSERT OR REPLACE INTO ledger_budget (month, amount) VALUES (?,?)",
        (month_text, parsed_amount),
    )
    conn.commit()
    return {"ok": True}


# -- Diary --------------------------------------------------------------------

def write_diary_row(
    conn: sqlite3.Connection,
    content: Any,
) -> dict[str, Any]:
    """Insert one model-free diary row through the shared product handler."""
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
