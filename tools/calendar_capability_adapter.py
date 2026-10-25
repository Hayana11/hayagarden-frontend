"""Provider-neutral adapter for calendar.read and calendar.write.

Transport and argument shaping only; the snapshot, the write queue and every
validation rule live in tools.calendar_store.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from tools import calendar_store

WRITE_FIELDS = (
    "op", "event_id", "title", "start", "end", "all_day",
    "location", "notes", "reminder_minutes",
)


def calendar_read(db_path: str, *, start_date: Any = None, days: Any = None) -> dict[str, Any]:
    try:
        return calendar_store.read_events(db_path, start_date=start_date, days=days)
    except calendar_store.CalendarError as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}


def calendar_write(db_path: str, **fields: Any) -> dict[str, Any]:
    try:
        return calendar_store.queue_write(db_path, **fields)
    except calendar_store.CalendarError as exc:
        return {"status": "INVALID_INPUT", "message": str(exc)}


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except (TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "INVALID_INPUT"}, ensure_ascii=False, sort_keys=True))
        return 2
    db_path = str(payload.get("db_path") or "").strip()
    if not db_path:
        print(json.dumps({"status": "INVALID_DB_PATH"}, ensure_ascii=False, sort_keys=True))
        return 2
    operation = payload.get("operation")
    if operation == "calendar_read":
        result = calendar_read(
            db_path,
            start_date=payload.get("start_date"),
            days=payload.get("days"),
        )
    elif operation == "calendar_write":
        result = calendar_write(
            db_path,
            **{field: payload.get(field) for field in WRITE_FIELDS},
        )
    else:
        print(json.dumps({"status": "INVALID_OPERATION"}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
