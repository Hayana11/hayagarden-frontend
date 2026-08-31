#!/usr/bin/env python3
"""Render statusLine quota; persist only a newer, fresh transcript event sample."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

DEFAULT_SNAPSHOT = "/var/lib/haya-context-usage/claude-statusline.json"
MAX_INPUT_CHARS = 1024 * 1024
MAX_TRANSCRIPT_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024
SOURCE_EPOCH = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


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


def source_time(value: Any) -> dt.datetime | None:
    """Accept only timezone-aware ISO timestamps, normalized to UTC."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        parsed = parsed.astimezone(dt.timezone.utc)
        return parsed if parsed >= SOURCE_EPOCH else None
    except (ValueError, OverflowError):
        return None


def source_is_fresh(observed: dt.datetime, now: dt.datetime) -> bool:
    try:
        max_age = float(os.environ.get("CLAUDE_STATUSLINE_MAX_AGE_SEC", "3600"))
        if not math.isfinite(max_age) or max_age <= 0:
            max_age = 3600
    except (ValueError, OverflowError):
        max_age = 3600
    return -300 <= (now - observed).total_seconds() <= max_age


def read_bounded_tail(path: Path, limit: int) -> tuple[bytes, bool]:
    """One regular-file tail read. Nonblocking open refuses FIFO/device input."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        offset = max(0, info.st_size - limit)
        handle.seek(offset)
        return handle.read(limit), offset > 0


def transcript_source_time(value: Any, now: dt.datetime) -> dt.datetime | None:
    """Read only a bounded tail, scanning backwards for a real assistant envelope."""
    if not isinstance(value, str) or not value:
        return None
    try:
        tail, truncated = read_bounded_tail(Path(value), MAX_TRANSCRIPT_BYTES)
    except (OSError, ValueError):
        return None
    lines = tail.split(b"\n")
    if truncated:
        # The first row may start in the middle of a record; never parse it.
        lines = lines[1:]
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            return None
        # Claude writes synthetic API errors as assistant records too.
        if row.get("isApiErrorMessage") or message.get("model") == "<synthetic>":
            continue
        if (message.get("role") != "assistant" or message.get("type") != "message"
                or not isinstance(message.get("id"), str) or not message["id"].startswith("msg_")
                or not isinstance(message.get("usage"), dict)
                or not isinstance(message.get("content"), list)):
            return None
        observed = source_time(row.get("timestamp"))
        # A bad/stale newest real assistant never licenses falling back to an older one.
        return observed if observed is not None and source_is_fresh(observed, now) else None
    return None


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
    observed = transcript_source_time(payload.get("transcript_path"), now)
    if observed is None:
        return None
    return {
        "schema_version": 1,
        "source": "claude_statusline",
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        **windows,
    }


def existing_source_time(path: Path) -> dt.datetime | None:
    """Validate the existing whitelist without making stale snapshots disappear."""
    try:
        raw, truncated = read_bounded_tail(path, MAX_SNAPSHOT_BYTES)
    except FileNotFoundError:
        return None
    if truncated:
        raise ValueError("snapshot too large")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    allowed = {"schema_version", "source", "observed_at", "five_hour", "seven_day"}
    if not isinstance(data, dict) or not set(data) <= allowed:
        return None
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        return None
    if data.get("source") != "claude_statusline":
        return None
    found = False
    for name in ("five_hour", "seven_day"):
        if name not in data:
            continue
        window = data[name]
        if (not isinstance(window, dict) or set(window) != {"used_percentage", "resets_at"}
                or quota_window(window) is None):
            return None
        found = True
    return source_time(data.get("observed_at")) if found else None


@contextmanager
def snapshot_write_guard(directory: Path):
    # Linux producers serialize comparison + replacement using the existing
    # directory inode: no lock-file payload or additional persisted schema.
    fd = None
    try:
        if os.name == "posix":
            import fcntl
            fd = os.open(directory, os.O_RDONLY)
            # Never let another writer stall the terminal command.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        if fd is not None:
            os.close(fd)


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Compare source times before atomic replacement; equal/older never rewrite."""
    candidate = source_time(snapshot.get("observed_at"))
    if candidate is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with snapshot_write_guard(path.parent):
        previous = existing_source_time(path)
        if previous is not None and candidate <= previous:
            return
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
        payload = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return 0
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return 0
    # Terminal display does not grant authority to persist a quota sample.
    windows = {name: quota_window(limits.get(name)) for name in ("five_hour", "seven_day")}
    text = " · ".join(
        f"{label} {windows[name]['used_percentage']:g}%"
        for name, label in (("five_hour", "5h"), ("seven_day", "7d"))
        if windows[name] is not None
    )
    if not text:
        return 0
    try:
        print(text, flush=True)
    except (OSError, UnicodeError):
        pass
    snapshot = sanitized_snapshot(payload)
    if snapshot is None:
        return 0
    try:
        write_snapshot(Path(os.environ.get("HAYA_CLAUDE_STATUSLINE_SNAPSHOT", DEFAULT_SNAPSHOT)), snapshot)
    except (OSError, ValueError):
        print("Claude quota snapshot: write failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
