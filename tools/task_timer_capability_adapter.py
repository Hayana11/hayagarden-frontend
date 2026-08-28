"""Provider-neutral adapter for the task.timer.start capability.

Only transport and input validation live here. Task lifecycle semantics remain
in command_store, which is shared with the legacy Gateway issue_command path.
"""
from __future__ import annotations

import json
import sys
from typing import Any

import command_store
from tools.task_timer_db import resolve_task_timer_commands_db_path


def start_task_timer(
    db_path: str,
    *,
    title: Any,
    countdown_seconds: Any = None,
) -> dict[str, Any]:
    path = resolve_task_timer_commands_db_path(explicit_path=db_path)
    if not path:
        return {"status": "INVALID_DB_PATH"}

    if not isinstance(title, str) or not title.strip():
        return {"status": "INVALID_TITLE"}

    if countdown_seconds is not None:
        if isinstance(countdown_seconds, bool) or not isinstance(countdown_seconds, int):
            return {"status": "INVALID_COUNTDOWN"}
        if countdown_seconds < 0:
            return {"status": "INVALID_COUNTDOWN"}

    command_id = command_store.issue(title, countdown_seconds, db_path=path)
    if command_id is None:
        return {"status": "INVALID_TITLE"}
    return {"status": "CREATED", "id": int(command_id)}


def _main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except (TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "INVALID_INPUT"}, ensure_ascii=False, sort_keys=True))
        return 2

    if payload.get("operation") != "start_task_timer":
        print(json.dumps({"status": "INVALID_OPERATION"}, ensure_ascii=False, sort_keys=True))
        return 2

    result = start_task_timer(
        payload.get("db_path"),
        title=payload.get("title"),
        countdown_seconds=payload.get("countdown_seconds"),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "CREATED" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
