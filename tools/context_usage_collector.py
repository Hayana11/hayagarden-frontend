#!/usr/bin/env python3
"""Collect local Claude Code/Codex usage and POST a redacted snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Iterable


RATE_LIMIT_MARKERS = (
    "rate limit",
    "usage limit",
    "weekly limit",
    "opus limit",
    "too many requests",
    "status code 429",
    '"status":429',
)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def iso_time(value: Any, fallback: str = "") -> str:
    if isinstance(value, (int, float)) and math.isfinite(value):
        try:
            return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            return fallback
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.isoformat().replace("+00:00", "Z")
    except ValueError:
        return fallback


def recent_jsonl(root: Path, limit: int) -> list[Path]:
    if not root.exists():
        return []
    files = (path for path in root.rglob("*.jsonl") if path.is_file())
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:limit]


def reverse_lines(path: Path, block_size: int = 64 * 1024) -> Iterable[str]:
    """Yield UTF-8 lines from the end without loading a whole session file."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position > 0:
            size = min(block_size, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size) + remainder
            rows = chunk.split(b"\n")
            remainder = rows[0]
            for row in reversed(rows[1:]):
                if row:
                    yield row.decode("utf-8", errors="replace")
        if remainder:
            yield remainder.decode("utf-8", errors="replace")


def parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        row = json.loads(line)
        return row if isinstance(row, dict) else None
    except json.JSONDecodeError:
        return None


def numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def integer(value: Any) -> int | None:
    result = numeric(value)
    return max(0, int(round(result))) if result is not None else None


def usage_tokens(value: Any) -> int | None:
    if isinstance(value, (int, float)):
        return integer(value)
    if not isinstance(value, dict):
        return None
    for key in ("total_tokens", "totalTokens"):
        total = integer(value.get(key))
        if total is not None:
            return total
    keys = (
        "input_tokens", "output_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "cached_input_tokens",
    )
    values = [integer(value.get(key)) for key in keys]
    found = [item for item in values if item is not None]
    return sum(found) if found else None


def codex_window(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    used = numeric(raw.get("used_percent", raw.get("used_percentage")))
    result: dict[str, Any] = {}
    if used is not None:
        used = min(100.0, max(0.0, used))
        result["used_percentage"] = round(used, 4)
        result["remaining_percentage"] = round(100 - used, 4)
    reset = iso_time(raw.get("resets_at"))
    if reset:
        result["resets_at"] = reset
    return result


def collect_codex(sessions_dir: Path, file_limit: int = 8) -> dict[str, Any]:
    quota: dict[str, Any] | None = None
    active_sessions: list[dict[str, Any]] = []
    for path in recent_jsonl(sessions_dir, file_limit):
        newest_session: dict[str, Any] | None = None
        lines_seen = 0
        for line in reverse_lines(path):
            lines_seen += 1
            if lines_seen > 12000:
                break
            row = parse_json_line(line)
            if not row or row.get("type") != "event_msg":
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            updated_at = iso_time(row.get("timestamp"), iso_time(path.stat().st_mtime))
            if newest_session is None:
                latest_tokens = usage_tokens(info.get("last_token_usage"))
                context_window = integer(info.get("model_context_window"))
                newest_session = {
                    key: value for key, value in {
                        "latest_context_tokens": latest_tokens,
                        "context_window_tokens": context_window,
                        "updated_at": updated_at,
                    }.items() if value not in (None, "")
                }
            rate_limits = payload.get("rate_limits")
            if quota is None and isinstance(rate_limits, dict):
                quota = {
                    "five_hour": codex_window(rate_limits.get("primary")),
                    "seven_day": codex_window(rate_limits.get("secondary")),
                    "updated_at": updated_at,
                }
                latest = usage_tokens(info.get("last_token_usage"))
                total = usage_tokens(info.get("total_token_usage"))
                context = integer(info.get("model_context_window"))
                if latest is not None:
                    quota["latest_tokens"] = latest
                if total is not None:
                    quota["total_tokens"] = total
                if context is not None:
                    quota["context_window_tokens"] = context
            if newest_session is not None and quota is not None:
                break
        if newest_session:
            active_sessions.append(newest_session)
    return {
        "id": "codex",
        "name": "Codex",
        "quota_source": "codex_session_jsonl" if quota else "unavailable",
        "quota": quota or {"five_hour": {}, "seven_day": {}, "updated_at": utc_now_iso()},
        "active_sessions": active_sessions[:6],
    }


def json_from_output(output: str) -> Any:
    output = output.strip()
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        pass
    starts = [index for index in (output.find("{"), output.find("[")) if index >= 0]
    if not starts:
        raise ValueError("ccusage did not return JSON")
    decoder = json.JSONDecoder()
    value, _ = decoder.raw_decode(output[min(starts):])
    return value


def ccusage_command(timezone: str) -> list[str]:
    configured = os.environ.get("CCUSAGE_COMMAND", "").strip()
    if configured:
        return shlex.split(configured, posix=os.name != "nt")
    runner = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    return [
        runner, "-y", "ccusage@latest", "blocks", "--active", "--json",
        "--offline", "--recent", "--timezone", timezone,
    ]


def read_ccusage_block(timezone: str, timeout: int = 45) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            ccusage_command(timezone),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = json_from_output(completed.stdout)
    except (ValueError, json.JSONDecodeError):
        return None
    blocks = payload.get("blocks") if isinstance(payload, dict) else payload
    if not isinstance(blocks, list):
        return None
    active = [block for block in blocks if isinstance(block, dict) and block.get("isActive") is True]
    candidates = active or [block for block in blocks if isinstance(block, dict)]
    return candidates[0] if candidates else None


def claude_window(block: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    start_text = iso_time(block.get("startTime"))
    end_text = iso_time(block.get("endTime") or block.get("actualEndTime"))
    try:
        start = dt.datetime.fromisoformat(start_text.replace("Z", "+00:00")) if start_text else now
        end = dt.datetime.fromisoformat(end_text.replace("Z", "+00:00")) if end_text else start + dt.timedelta(hours=5)
    except ValueError:
        start, end = now, now + dt.timedelta(hours=5)
    window_minutes = max(1, round((end - start).total_seconds() / 60))
    projection = block.get("projection") if isinstance(block.get("projection"), dict) else {}
    remaining = integer(projection.get("remainingMinutes"))
    if remaining is None:
        remaining = max(0, round((end - now).total_seconds() / 60))
    remaining_pct = min(100.0, max(0.0, remaining / window_minutes * 100))
    result: dict[str, Any] = {
        "remaining_basis": "time_window",
        "remaining_minutes": remaining,
        "remaining_percentage": round(remaining_pct, 4),
        "used_percentage": round(100 - remaining_pct, 4),
        "resets_at": end.isoformat().replace("+00:00", "Z"),
    }
    for source, target in (("totalTokens", "total_tokens"), ("projectedTotalTokens", "projected_total_tokens")):
        value = integer(block.get(source) if source in block else projection.get(source))
        if value is not None:
            result[target] = value
    models = block.get("models")
    if isinstance(models, list):
        result["models"] = [str(model)[:80] for model in models[:8]]
    return result


def rate_limit_detail(text: str, observed_at: str) -> dict[str, Any] | None:
    lowered = text.lower()
    if not any(marker in lowered for marker in RATE_LIMIT_MARKERS):
        return None
    kind = "weekly" if "weekly" in lowered else "opus" if "opus" in lowered else "rate_limit"
    reset_match = re.search(r"(?:reset(?:s)?|try again)[^\n\r.]{0,180}", text, re.IGNORECASE)
    return {
        "kind": kind,
        "exhausted": True,
        "reset_text": reset_match.group(0)[:200] if reset_match else "",
        "observed_at": observed_at,
    }


def scan_claude_projects(projects_dir: Path, file_limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    sessions: list[dict[str, Any]] = []
    newest_limit: dict[str, Any] | None = None
    for path in recent_jsonl(projects_dir, file_limit):
        session: dict[str, Any] | None = None
        lines_seen = 0
        for line in reverse_lines(path):
            lines_seen += 1
            if lines_seen > 12000:
                break
            row = parse_json_line(line)
            if not row:
                continue
            observed_at = iso_time(row.get("timestamp"), iso_time(path.stat().st_mtime))
            if newest_limit is None:
                detail = rate_limit_detail(line, observed_at)
                if detail:
                    newest_limit = detail
            if session is None:
                message = row.get("message") if isinstance(row.get("message"), dict) else {}
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else row.get("usage")
                tokens = usage_tokens(usage)
                if tokens is not None:
                    session = {
                        "latest_context_tokens": tokens,
                        "model": str(message.get("model") or row.get("model") or "")[:100],
                        "updated_at": observed_at,
                    }
            if session is not None and newest_limit is not None:
                break
        if session:
            sessions.append({key: value for key, value in session.items() if value not in (None, "")})
    return sessions[:6], newest_limit


def collect_claude(projects_dir: Path, timezone: str) -> dict[str, Any]:
    block = read_ccusage_block(timezone)
    sessions, effective_limit = scan_claude_projects(projects_dir)
    quota: dict[str, Any] = {
        "five_hour": claude_window(block) if block else {},
        "seven_day": {},
        "updated_at": utc_now_iso(),
    }
    if effective_limit:
        quota["effective_limit"] = effective_limit
    return {
        "id": "claude",
        "name": "Claude Code",
        "quota_source": "ccusage_blocks" if block else "claude_project_jsonl" if effective_limit else "unavailable",
        "quota": quota,
        "active_sessions": sessions,
    }


def collect_snapshot(codex_dir: Path, claude_dir: Path, timezone: str) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "agents": [collect_claude(claude_dir, timezone), collect_codex(codex_dir)],
    }


def post_snapshot(url: str, token: str, snapshot: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    if not url:
        raise ValueError("CONTEXT_USAGE_REPORT_URL or --url is required")
    if not token:
        raise ValueError("CONTEXT_USAGE_REPORT_TOKEN or --token is required")
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("CONTEXT_USAGE_REPORT_URL", ""))
    parser.add_argument("--token", default=os.environ.get("CONTEXT_USAGE_REPORT_TOKEN", ""))
    parser.add_argument("--codex-dir", type=Path, default=Path(os.environ.get("CODEX_SESSIONS_DIR", "~/.codex/sessions")).expanduser())
    parser.add_argument("--claude-dir", type=Path, default=Path(os.environ.get("CLAUDE_PROJECTS_DIR", "~/.claude/projects")).expanduser())
    parser.add_argument("--timezone", default=os.environ.get("CONTEXT_USAGE_TIMEZONE", "Asia/Shanghai"))
    parser.add_argument("--watch", action="store_true", help="keep reporting until interrupted")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("CONTEXT_USAGE_INTERVAL", "60")))
    parser.add_argument("--print", action="store_true", dest="print_only", help="print the redacted snapshot without POSTing")
    return parser.parse_args(argv)


def run_once(args: argparse.Namespace) -> None:
    snapshot = collect_snapshot(args.codex_dir, args.claude_dir, args.timezone)
    if args.print_only:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return
    result = post_snapshot(args.url, args.token, snapshot)
    accepted = ", ".join(result.get("accepted") or []) or "none"
    print(f"context usage reported: {accepted}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.interval = max(30, args.interval)
    while True:
        try:
            run_once(args)
        except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            print(f"context usage report failed: {exc}", file=sys.stderr)
            if not args.watch:
                return 1
        if not args.watch:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
