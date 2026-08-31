#!/usr/bin/env python3
"""Render Claude statusLine quota and atomically persist only its safe fields."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

DEFAULT_SNAPSHOT = "/var/lib/haya-context-usage/claude-statusline.json"
MAX_INPUT_CHARS = 1024 * 1024


def quota_window(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    used, reset = value.get("used_percentage"), value.get("resets_at")
    try:
        if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
            return None
        if type(reset) not in (int, float) or not math.isfinite(reset):
            return None
        # Seconds only: reject unrepresentable dates, never convert milliseconds.
        dt.datetime.fromtimestamp(reset, dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return {"used_percentage": used, "resets_at": reset}


def sanitized_snapshot(payload: Any, now: dt.datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("rate_limits"), dict):
        return None
    windows = {}
    for name in ("five_hour", "seven_day"):
        window = quota_window(payload["rate_limits"].get(name))
        if window is not None:
            windows[name] = window
    if not windows:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "schema_version": 1,
        "source": "claude_statusline",
        "observed_at": now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        **windows,
    }


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """A reader sees either the old complete snapshot or the new complete one."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".claude-statusline-", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            json.dump(snapshot, handle, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
        if len(raw) > MAX_INPUT_CHARS:
            return 0
        snapshot = sanitized_snapshot(json.loads(raw))
    except (OSError, ValueError, RecursionError):
        return 0
    if snapshot is None:
        return 0
    text = " · ".join(
        f"{label} {snapshot[name]['used_percentage']:g}%"
        for name, label in (("five_hour", "5h"), ("seven_day", "7d"))
        if name in snapshot
    )
    try:
        print(text, flush=True)
    except (OSError, UnicodeError):
        pass
    try:
        write_snapshot(Path(os.environ.get("HAYA_CLAUDE_STATUSLINE_SNAPSHOT", DEFAULT_SNAPSHOT)), snapshot)
    except (OSError, ValueError):
        print("Claude quota snapshot: write failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
