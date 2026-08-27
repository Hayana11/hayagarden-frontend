"""Capability adapter for scheduling and cancelling one existing self trigger.

This adapter owns only strict input validation and HTTP transport shaping.
Lifecycle semantics remain in the existing /api/self_triggers routes and their
self_triggers table; it never opens or writes a database.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Mapping

DEFAULT_SELF_TRIGGER_API_BASE_URL = "http://127.0.0.1:5050"


def _api_base_url(value: Any = None) -> str:
    base = str(value or os.environ.get("SELF_TRIGGER_API_BASE_URL") or DEFAULT_SELF_TRIGGER_API_BASE_URL).strip()
    if not base:
        raise ValueError("SELF_TRIGGER_API_BASE_URL is required")
    return base.rstrip("/")


def _post(path: str, payload: Mapping[str, Any], *, api_base_url: Any = None) -> dict[str, Any]:
    request = urllib.request.Request(
        _api_base_url(api_base_url) + path,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"self trigger API HTTP {exc.code}: {detail}") from exc
    try:
        result = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("self trigger API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("self trigger API returned a non-object")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return int(value)


def schedule_self_trigger(
    *,
    minutes: Any,
    note: Any = None,
    api_base_url: Any = None,
) -> dict[str, Any]:
    clean_minutes = _positive_int(minutes, "minutes")
    if clean_minutes > 1440:
        raise ValueError("minutes must be 1-1440")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string")
    result = _post(
        "/api/self_triggers",
        {"minutes": clean_minutes, "note": note},
        api_base_url=api_base_url,
    )
    if result.get("ok") is not True:
        raise ValueError("self trigger schedule was not accepted")
    trigger_id = result.get("id")
    trigger_at = result.get("trigger_at")
    if isinstance(trigger_id, bool) or not isinstance(trigger_id, int) or trigger_id < 1:
        raise ValueError("self trigger API returned an invalid id")
    if not isinstance(trigger_at, str) or not trigger_at.strip():
        raise ValueError("self trigger API returned an invalid trigger_at")
    return {
        "status": "SCHEDULED",
        "id": int(trigger_id),
        "trigger_at": trigger_at,
    }


def cancel_self_trigger(
    *,
    trigger_id: Any,
    api_base_url: Any = None,
) -> dict[str, Any]:
    clean_id = _positive_int(trigger_id, "id")
    result = _post(
        "/api/self_triggers/cancel",
        {"id": clean_id},
        api_base_url=api_base_url,
    )
    if result.get("ok") is not True:
        raise ValueError("self trigger cancel was not accepted")
    return {"status": "CANCELLED", "id": clean_id}


def _main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    operation = payload.get("operation")
    if operation == "schedule_self_trigger":
        result = schedule_self_trigger(
            minutes=payload.get("minutes"),
            note=payload.get("note"),
        )
    elif operation == "cancel_self_trigger":
        result = cancel_self_trigger(trigger_id=payload.get("id"))
    else:
        raise ValueError("unknown self trigger operation")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
