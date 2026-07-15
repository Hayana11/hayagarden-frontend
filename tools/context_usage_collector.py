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

# Unofficial, reverse-engineered account usage endpoints. These are the same
# endpoints the Claude Code CLI and Codex CLI call to render their own quota
# bars, so we read them the same way instead of estimating usage from local
# session files. Anthropic/OpenAI have not published these as stable public
# API and may change or remove them without notice; treat failures here as
# "no data" and fall back to local-file estimation rather than raising.
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


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


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_claude_oauth_token(path: Path) -> str | None:
    """Read the access token Claude Code's own CLI already stores locally.

    We only ever read this file, never write to it — token refresh stays the
    CLI's own responsibility so this collector can't corrupt a live login.
    """
    data = _read_json_file(path)
    if not data:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
    token = oauth.get("accessToken") or oauth.get("access_token")
    return str(token) if token else None


def read_codex_oauth(path: Path) -> tuple[str, str] | None:
    """Read the access token + account id Codex's own CLI already stores locally."""
    data = _read_json_file(path)
    if not data:
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
    token = tokens.get("access_token") or tokens.get("accessToken")
    if not token:
        return None
    account_id = tokens.get("account_id") or tokens.get("accountId") or data.get("account_id") or ""
    return str(token), str(account_id)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    urllib's default handler follows 30x responses and replays the original
    Authorization header at the new Location, even across hosts. These usage
    calls carry a live OAuth token, so any redirect must be treated as a
    failure (fall back to local estimation) rather than followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _http_get_json(url: str, headers: dict[str, str], timeout: int = 6) -> dict[str, Any] | None:
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _official_window(section: Any, percent_key: str) -> dict[str, Any]:
    section = section if isinstance(section, dict) else {}
    used = numeric(section.get(percent_key))
    result: dict[str, Any] = {}
    if used is not None:
        used = min(100.0, max(0.0, used))
        result["used_percentage"] = round(used, 4)
        result["remaining_percentage"] = round(100 - used, 4)
    reset = iso_time(section.get("resets_at") or section.get("reset_at"))
    if reset:
        result["resets_at"] = reset
    return result


def fetch_claude_official_usage(token: str) -> dict[str, Any] | None:
    """Call the same account-usage endpoint the Claude Code CLI itself reads.

    This is unofficial/reverse-engineered, not a published stable API — treat
    any failure as "no data" and let the caller fall back to local estimation.
    """
    payload = _http_get_json(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli",
        },
    )
    if not payload:
        return None
    five_hour = _official_window(payload.get("five_hour"), "utilization")
    seven_day = _official_window(payload.get("seven_day"), "utilization")
    if not five_hour and not seven_day:
        return None
    return {"five_hour": five_hour, "seven_day": seven_day, "updated_at": utc_now_iso()}


def fetch_codex_official_usage(token: str, account_id: str) -> dict[str, Any] | None:
    """Call the same account-usage endpoint the Codex CLI itself reads.

    Same caveat as fetch_claude_official_usage: unofficial endpoint, fail soft.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if account_id:
        headers["chatgpt-account-id"] = account_id
    payload = _http_get_json(CODEX_USAGE_URL, headers=headers)
    if not payload:
        return None
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    five_hour = _official_window(rate_limit.get("primary_window"), "used_percent")
    seven_day = _official_window(rate_limit.get("secondary_window"), "used_percent")
    if not five_hour and not seven_day:
        return None
    return {"five_hour": five_hour, "seven_day": seven_day, "updated_at": utc_now_iso()}


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


def default_codex_auth_path() -> Path:
    return Path(os.environ.get("CODEX_AUTH_PATH", "~/.codex/auth.json")).expanduser()


def default_claude_credentials_path() -> Path:
    return Path(os.environ.get("CLAUDE_CREDENTIALS_PATH", "~/.claude/.credentials.json")).expanduser()


def collect_codex(
    sessions_dir: Path,
    auth_path: Path | None = None,
    use_official: bool = False,
    file_limit: int = 8,
) -> dict[str, Any]:
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

    source = "codex_session_jsonl" if quota else "unavailable"
    if use_official:
        creds = read_codex_oauth(auth_path or default_codex_auth_path())
        if creds:
            official = fetch_codex_official_usage(*creds)
            if official:
                # Keep token/context counters from the local session scan —
                # the official endpoint only reports quota %, not context size.
                for key in ("latest_tokens", "total_tokens", "context_window_tokens"):
                    if quota and key in quota:
                        official[key] = quota[key]
                quota = official
                source = "codex_oauth_usage"

    return {
        "id": "codex",
        "name": "Codex",
        "quota_source": source,
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
    projection = block.get("projection") if isinstance(block.get("projection"), dict) else {}
    remaining = integer(projection.get("remainingMinutes"))
    if remaining is None:
        remaining = max(0, round((end - now).total_seconds() / 60))
    result: dict[str, Any] = {
        # ccusage exposes time remaining in the active block, not the user's
        # subscription quota remaining. Keep it only as reset metadata; using
        # elapsed time as a quota percentage would be semantically false.
        "remaining_basis": "time_until_reset",
        "remaining_minutes": remaining,
        "resets_at": end.isoformat().replace("+00:00", "Z"),
    }
    total_tokens = integer(block.get("totalTokens"))
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    projected_tokens = integer(
        block.get("projectedTotalTokens")
        if "projectedTotalTokens" in block
        else projection.get("projectedTotalTokens", projection.get("totalTokens"))
    )
    if projected_tokens is not None:
        result["projected_total_tokens"] = projected_tokens
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


def collect_claude(
    projects_dir: Path,
    timezone: str,
    credentials_path: Path | None = None,
    use_official: bool = False,
) -> dict[str, Any]:
    sessions, effective_limit = scan_claude_projects(projects_dir)

    official = None
    if use_official:
        token = read_claude_oauth_token(credentials_path or default_claude_credentials_path())
        if token:
            official = fetch_claude_official_usage(token)

    if official:
        quota = official
        source = "claude_oauth_usage"
    else:
        block = read_ccusage_block(timezone)
        quota = {
            "five_hour": claude_window(block) if block else {},
            "seven_day": {},
            "updated_at": utc_now_iso(),
        }
        source = "ccusage_blocks" if block else "claude_project_jsonl" if effective_limit else "unavailable"

    if effective_limit:
        quota["effective_limit"] = effective_limit

    return {
        "id": "claude",
        "name": "Claude Code",
        "quota_source": source,
        "quota": quota,
        "active_sessions": sessions,
    }


def collect_snapshot(
    codex_dir: Path,
    claude_dir: Path,
    timezone: str,
    claude_credentials: Path | None = None,
    codex_auth: Path | None = None,
    use_official: bool = False,
) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "agents": [
            collect_claude(claude_dir, timezone, claude_credentials, use_official),
            collect_codex(codex_dir, codex_auth, use_official),
        ],
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
    parser.add_argument(
        "--claude-credentials",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CREDENTIALS_PATH", "~/.claude/.credentials.json")).expanduser(),
        help="local Claude Code OAuth credentials file (read-only; never modified)",
    )
    parser.add_argument(
        "--codex-auth",
        type=Path,
        default=Path(os.environ.get("CODEX_AUTH_PATH", "~/.codex/auth.json")).expanduser(),
        help="local Codex OAuth credentials file (read-only; never modified)",
    )
    parser.add_argument("--timezone", default=os.environ.get("CONTEXT_USAGE_TIMEZONE", "Asia/Shanghai"))
    parser.add_argument("--watch", action="store_true", help="keep reporting until interrupted")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("CONTEXT_USAGE_INTERVAL", "60")))
    parser.add_argument("--print", action="store_true", dest="print_only", help="print the redacted snapshot without POSTing")
    parser.add_argument(
        "--no-official-usage",
        action="store_true",
        help="skip the unofficial Claude/Codex account-usage endpoints and only estimate from local files",
    )
    return parser.parse_args(argv)


def official_usage_enabled(args: argparse.Namespace) -> bool:
    if args.no_official_usage:
        return False
    return os.environ.get("CONTEXT_USAGE_OFFICIAL", "1") != "0"


def run_once(args: argparse.Namespace) -> None:
    snapshot = collect_snapshot(
        args.codex_dir,
        args.claude_dir,
        args.timezone,
        args.claude_credentials,
        args.codex_auth,
        official_usage_enabled(args),
    )
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
