"""Unified Nexus event model — frozen names and public fields only."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Mapping

FROZEN_EVENTS = frozenset(
    {
        "meta",
        "think",
        "text",
        "tool_use",
        "tool_result",
        "status",
        "git",
        "done",
        "err",
    }
)
TERMINAL_EVENTS = frozenset({"done", "err"})
PUBLIC_FIELDS = ("event", "turn_id", "agent", "sequence", "timestamp", "data")

_SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|api[_-]?key|authorization|credential|private[_-]?key)",
    re.I,
)
_ENV_KEY_RE = re.compile(r"^env$", re.I)
_ABS_PATH_RE = re.compile(r"(^|[\s=\"'])(/opt/frontend|/root|/home/[^/\s]+|/etc)(/|\s|\"|'|$)")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_event_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if name in FROZEN_EVENTS:
        return name
    return "status"


def redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            if _SENSITIVE_KEY_RE.search(key_s) or _ENV_KEY_RE.match(key_s):
                out[key_s] = "[redacted]"
            else:
                out[key_s] = redact_value(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [redact_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, tuple):
        return [redact_value(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        text = value
        if len(text) > 8000:
            text = text[:8000] + "…"
        if _ABS_PATH_RE.search(text) or _SENSITIVE_KEY_RE.search(text):
            text = _ABS_PATH_RE.sub(r"\1[redacted_path]\3", text)
            text = _SENSITIVE_KEY_RE.sub("[redacted]", text)
        return text
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def make_event(
    *,
    event: str,
    turn_id: str,
    agent: str,
    sequence: int,
    data: Any = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    name = normalize_event_name(event)
    if name not in FROZEN_EVENTS:
        name = "status"
    payload = data if isinstance(data, dict) else {"value": data}
    return {
        "event": name,
        "turn_id": turn_id,
        "agent": agent,
        "sequence": int(sequence),
        "timestamp": timestamp or utc_now_iso(),
        "data": redact_value(payload),
    }


def is_terminal(event_name: str) -> bool:
    return event_name in TERMINAL_EVENTS
