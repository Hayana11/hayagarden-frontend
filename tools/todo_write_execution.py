"""Crash-safe idempotent execution for the API Relay Todo write.

This module owns only the Todo write execution table and its caller-owned
SQLite transaction.  It deliberately does not issue leases or perform HTTP.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from tools.execution_fence import build_approval_id
from tools.product_handlers import ProductHandlerError, create_todo

CAPABILITY_ID = "todo.write"
TOOL_NAME = "add_todo"
EXECUTION_STATUSES = frozenset({"succeeded", "failed_permanent"})


class TodoWriteExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


def _canonical_input(tool_input: Mapping[str, Any]) -> str:
    if not isinstance(tool_input, Mapping):
        raise TodoWriteExecutionError("MALFORMED_INPUT", "tool_input must be an object")
    try:
        return json.dumps(
            dict(tool_input),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TodoWriteExecutionError(
            "MALFORMED_INPUT", "tool_input is not JSON-safe"
        ) from exc


def input_digest(tool_input: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_input(tool_input).encode("utf-8")).hexdigest()


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todo_write_executions (
            pending_action_id TEXT PRIMARY KEY,
            capability_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            execution_status TEXT NOT NULL CHECK (
                execution_status IN ('succeeded', 'failed_permanent')
            ),
            todo_id INTEGER,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_todo_write_executions_digest
        ON todo_write_executions (input_digest)
        """
    )
    conn.commit()


def _row_values(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = (
        "pending_action_id", "approval_id", "capability_id", "tool_name",
        "tool_input_json", "owner_id", "state",
    )
    return dict(zip(names, row))


def _load_action(conn: sqlite3.Connection, pending_action_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT pending_action_id, approval_id, capability_id, tool_name,
               tool_input_json, owner_id, state
        FROM confirmation_pending_actions
        WHERE pending_action_id=?
        """,
        (pending_action_id,),
    ).fetchone()
    if row is None:
        raise TodoWriteExecutionError("UNKNOWN_PENDING_ACTION", "unknown pending_action_id")
    values = _row_values(row)
    try:
        tool_input = json.loads(values["tool_input_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TodoWriteExecutionError("MALFORMED_PENDING_ACTION", "pending input is invalid") from exc
    if not isinstance(tool_input, dict):
        raise TodoWriteExecutionError("MALFORMED_PENDING_ACTION", "pending input is not an object")
    expected_approval = build_approval_id(
        values["capability_id"], values["tool_name"], tool_input
    )
    if (
        values["capability_id"] != CAPABILITY_ID
        or values["tool_name"] != TOOL_NAME
        or expected_approval != values["approval_id"]
    ):
        raise TodoWriteExecutionError("ACTION_IDENTITY_MISMATCH", "pending action identity mismatch")
    values["tool_input"] = tool_input
    return values


def _request_values(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise TodoWriteExecutionError("MALFORMED_REQUEST", "execution payload must be an object")
    required = (
        "pending_action_id", "approval_id", "capability_id", "tool_name",
        "tool_input", "owner_id",
    )
    values = {name: payload.get(name) for name in required}
    for name in required:
        if name in {"tool_input"}:
            continue
        if not isinstance(values[name], str) or not values[name].strip():
            raise TodoWriteExecutionError("MALFORMED_REQUEST", f"{name} is required")
    if values["capability_id"] != CAPABILITY_ID or values["tool_name"] != TOOL_NAME:
        raise TodoWriteExecutionError("DENIED_CAPABILITY", "only add_todo is accepted")
    if not isinstance(values["tool_input"], Mapping):
        raise TodoWriteExecutionError("MALFORMED_REQUEST", "tool_input must be an object")
    return values


def _replay_execution(row: sqlite3.Row | tuple[Any, ...], values: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        existing = dict(row)
    else:
        existing = dict(zip(
            ("pending_action_id", "capability_id", "tool_name", "input_digest",
             "execution_status", "todo_id", "result_json"),
            row,
        ))
    expected_digest = input_digest(values["tool_input"])
    if (
        existing["pending_action_id"] != values["pending_action_id"]
        or existing["capability_id"] != CAPABILITY_ID
        or existing["tool_name"] != TOOL_NAME
        or existing["input_digest"] != expected_digest
        or existing["execution_status"] not in EXECUTION_STATUSES
    ):
        raise TodoWriteExecutionError("EXECUTION_IDENTITY_MISMATCH", "execution identity mismatch")
    try:
        result = json.loads(existing["result_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TodoWriteExecutionError("MALFORMED_EXECUTION", "durable result is invalid") from exc
    if not isinstance(result, dict) or set(result) - {"ok", "id", "error", "code"}:
        raise TodoWriteExecutionError("MALFORMED_EXECUTION", "durable result schema mismatch")
    return result


def execute_todo_write(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one approved action and atomically insert/replay its Todo."""
    values = _request_values(payload)
    pending_id = values["pending_action_id"]
    ensure_schema(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        action = _load_action(conn, pending_id)
        if (
            action["approval_id"] != values["approval_id"]
            or action["owner_id"] != values["owner_id"]
            or action["capability_id"] != values["capability_id"]
            or action["tool_name"] != values["tool_name"]
            or action["tool_input"] != dict(values["tool_input"])
        ):
            raise TodoWriteExecutionError("ACTION_IDENTITY_MISMATCH", "request does not match durable action")
        if action["state"] not in {"approved", "completed"}:
            raise TodoWriteExecutionError("CONFIRMATION_REQUIRED", "action is not approved")

        existing = conn.execute(
            """
            SELECT pending_action_id, capability_id, tool_name, input_digest,
                   execution_status, todo_id, result_json
            FROM todo_write_executions
            WHERE pending_action_id=?
            """,
            (pending_id,),
        ).fetchone()
        if existing is not None:
            result = _replay_execution(existing, values)
            conn.commit()
            return result
        if action["state"] == "completed":
            raise TodoWriteExecutionError("MALFORMED_EXECUTION", "completed action has no durable result")

        tool_input = dict(action["tool_input"])
        try:
            result = create_todo(
                conn,
                content=tool_input.get("content", ""),
                due_date=tool_input.get("due_date"),
                author="fyodor_api",
                commit=False,
            )
            durable = {"ok": True, "id": int(result["id"])}
            status = "succeeded"
            todo_id = int(result["id"])
        except ProductHandlerError as exc:
            durable = {
                "ok": False,
                "error": str(exc),
                "code": "PERMANENT_INPUT_ERROR",
            }
            status = "failed_permanent"
            todo_id = None

        encoded = json.dumps(durable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            """
            INSERT INTO todo_write_executions (
                pending_action_id, capability_id, tool_name, input_digest,
                execution_status, todo_id, result_json, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pending_id, CAPABILITY_ID, TOOL_NAME, input_digest(tool_input),
                status, todo_id, encoded, _stamp(), _stamp(),
            ),
        )
        if status == "succeeded":
            changed = conn.execute(
                """
                UPDATE confirmation_pending_actions
                SET state='completed', completed_at=?
                WHERE pending_action_id=? AND state='approved'
                """,
                (_stamp(), pending_id),
            )
            if changed.rowcount != 1:
                raise TodoWriteExecutionError("STATE_CONFLICT", "approved action changed during execution")
        conn.commit()
        return durable
    except TodoWriteExecutionError:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise TodoWriteExecutionError("EXECUTION_CONFLICT", "execution identity already exists") from exc
    except sqlite3.Error as exc:
        conn.rollback()
        raise TodoWriteExecutionError("EXECUTION_UNAVAILABLE", "Todo execution transaction failed") from exc
