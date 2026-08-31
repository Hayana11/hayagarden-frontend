#!/usr/bin/env python3
"""Collect local Claude Code/Codex usage and POST a redacted snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
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
CLAUDE_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# Claude oauth/usage is aggressively rate-limited (community note: refresh ≥10–15min).
# Two VPS timers every 5 minutes will 429; cache last good official payload and reuse.
OFFICIAL_CACHE_PATH = Path(
    os.environ.get(
        "CONTEXT_USAGE_OFFICIAL_CACHE",
        "/var/lib/haya-context-usage/official.json",
    )
)
OFFICIAL_MIN_INTERVAL_SEC = int(os.environ.get("CONTEXT_USAGE_OFFICIAL_MIN_INTERVAL", "720"))


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
    """Get the access token Claude Code's own CLI already holds.

    Prefers CLAUDE_CODE_OAUTH_TOKEN (a long-lived token from `claude
    setup-token`, ~1 year validity) when set — this is the token to use on a
    machine that never runs Claude Code interactively, since the normal
    credentials file only gets refreshed by an interactive/long-running CLI
    session and a bare `-p` invocation does not renew it. Falls back to
    reading the short-lived token from the credentials file, which stays
    fresh as long as something on this machine is using the CLI normally.
    Either way we only ever read — never write to or refresh — local state.
    """
    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token
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
    payload, _status = _http_get_json_result(url, headers, timeout=timeout)
    return payload


def _http_get_json_result(
    url: str, headers: dict[str, str], timeout: int = 6,
    *, retry_metadata: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """GET JSON; return (payload, http_status). status is set on HTTPError too."""
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            status = int(response.status)
            if status != 200:
                if retry_metadata is not None:
                    retry_metadata["retry_after_seconds"] = _claude_retry_after_seconds(response.headers.get("Retry-After"))
                return None, status
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if retry_metadata is not None:
            retry_metadata["retry_after_seconds"] = _claude_retry_after_seconds(
                exc.headers.get("Retry-After") if exc.headers else None
            )
        return None, int(exc.code)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return None, None
    return (payload if isinstance(payload, dict) else None), 200


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


def _read_official_cache() -> dict[str, Any]:
    data = _read_json_file(OFFICIAL_CACHE_PATH)
    return data if isinstance(data, dict) else {}


def _write_official_cache(cache: dict[str, Any]) -> None:
    try:
        OFFICIAL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFICIAL_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _cache_age_seconds(entry: dict[str, Any] | None) -> float | None:
    if not isinstance(entry, dict):
        return None
    fetched = entry.get("fetched_at") or (entry.get("quota") or {}).get("updated_at")
    if not fetched:
        return None
    try:
        when = dt.datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - when).total_seconds())
    except ValueError:
        return None


def _cached_official_source(agent: str) -> str:
    entry = _read_official_cache().get(agent)
    if not isinstance(entry, dict):
        return "claude_oauth_usage" if agent == "claude" else ""
    source = entry.get("source")
    if agent == "claude" and source == "claude_api_headers":
        return source
    return "claude_oauth_usage" if agent == "claude" else ""


def _get_cached_official(agent: str) -> dict[str, Any] | None:
    entry = _read_official_cache().get(agent)
    if not isinstance(entry, dict):
        return None
    quota = entry.get("quota")
    return quota if isinstance(quota, dict) and (quota.get("five_hour") or quota.get("seven_day")) else None


def _store_cached_official(
    agent: str,
    quota: dict[str, Any],
    *,
    source: str | None = None,
    preserve_retry: bool = False,
) -> None:
    cache = _read_official_cache()
    entry: dict[str, Any] = {"fetched_at": utc_now_iso(), "quota": quota}
    if source == "claude_api_headers":
        entry["source"] = source
    if preserve_retry:
        previous = cache.get(agent)
        if isinstance(previous, dict):
            for key in ("last_attempt_at", "last_status", "next_retry_at"):
                if key in previous:
                    entry[key] = previous[key]
    cache[agent] = entry
    _write_official_cache(cache)


def _claude_retry_after_seconds(value: Any) -> float:
    """Bound untrusted Retry-After (delay seconds or HTTP date) to one day."""
    delay = 0.0
    if isinstance(value, str) and len(value) <= 128:
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            delay = float(value)
        else:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is not None:
                    delay = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, min(delay, 86400.0))


def _claude_cooldown_active(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    retry_at = iso_time(entry.get("next_retry_at"))
    if not retry_at:
        return False
    return dt.datetime.fromisoformat(retry_at.replace("Z", "+00:00")) > dt.datetime.now(dt.timezone.utc)


def _store_claude_failure(attempt: dict[str, Any]) -> None:
    """Persist only safe failure metadata, even before the first good quota."""
    cache = _read_official_cache()
    previous = cache.get("claude")
    entry = dict(previous) if isinstance(previous, dict) else {}
    status = attempt.get("last_status")
    # Network errors, 408/429/5xx and invalid payloads also need a retry floor.
    delay = max(OFFICIAL_MIN_INTERVAL_SEC, 1)
    if status == 429 or status == 503:
        delay = max(delay, attempt.get("retry_after_seconds", 0))
    now = dt.datetime.now(dt.timezone.utc)
    entry.update({
        "last_attempt_at": now.isoformat().replace("+00:00", "Z"),
        "last_status": status,
        "next_retry_at": (now + dt.timedelta(seconds=delay)).isoformat().replace("+00:00", "Z"),
    })
    cache["claude"] = entry
    # Replace atomically so a killed one-shot collector cannot truncate last-good.
    temp_path = None
    try:
        OFFICIAL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=OFFICIAL_CACHE_PATH.parent, delete=False) as handle:
            temp_path = Path(handle.name)
            json.dump(cache, handle, ensure_ascii=False)
        temp_path.replace(OFFICIAL_CACHE_PATH)
    except OSError:
        print("Claude official usage: failed to persist retry cooldown", file=sys.stderr)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    transient = status is None or status in (408, 429) or 500 <= status <= 599
    if not transient:
        # Never print exception text, headers, tokens or response bodies.
        print(f"Claude official usage: HTTP {status}; no usable quota; check credentials/endpoint", file=sys.stderr)


def fetch_claude_official_usage(
    token: str, *, attempt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Call the same account-usage endpoint the Claude Code CLI itself reads.

    Same endpoint as 双子续杯 / Claude Code Settings:
    GET https://api.anthropic.com/api/oauth/usage → five_hour/seven_day.utilization.
    Unofficial; 429 is common if polled too often — caller should reuse last good.
    """
    payload, status = _http_get_json_result(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.1.220 (external, cli)",
        },
        retry_metadata=attempt,
    )
    if attempt is not None:
        attempt["last_status"] = status
    if status == 429:
        return None  # caller uses cache; do not invent JSONL "exhausted"
    if not payload:
        return None
    # Reject error bodies that lack utilization fields (don't treat as 100% remaining)
    if not isinstance(payload.get("five_hour"), dict) and not isinstance(payload.get("seven_day"), dict):
        return None
    five_hour = _official_window(payload.get("five_hour"), "utilization")
    seven_day = _official_window(payload.get("seven_day"), "utilization")
    if not five_hour and not seven_day:
        return None
    return {"five_hour": five_hour, "seven_day": seven_day, "updated_at": utc_now_iso()}


def _http_post_headers_result(
    url: str,
    headers: dict[str, str],
    data: bytes,
    timeout: int = 6,
) -> tuple[dict[str, str], int | None]:
    request = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return {str(key).lower(): str(value).strip() for key, value in response.headers.items()}, int(response.status)
    except urllib.error.HTTPError as exc:
        return (
            {str(key).lower(): str(value).strip() for key, value in exc.headers.items()} if exc.headers else {},
            int(exc.code),
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return {}, None


def _claude_header_window(headers: dict[str, str], suffix: str) -> dict[str, Any]:
    utilization = numeric(headers.get(f"anthropic-ratelimit-unified-{suffix}-utilization"))
    reset_raw = headers.get(f"anthropic-ratelimit-unified-{suffix}-reset")
    reset_number = numeric(reset_raw)
    reset = iso_time(reset_number if reset_number is not None else reset_raw)
    if utilization is None or not reset:
        return {}
    used = min(100.0, max(0.0, utilization * 100))
    return {
        "used_percentage": round(used, 4),
        "remaining_percentage": round(100 - used, 4),
        "resets_at": reset,
    }


def fetch_claude_api_headers_usage(
    token: str, *, attempt: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1,
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": "quota"}],
    }).encode("utf-8")
    headers, status = _http_post_headers_result(
        CLAUDE_MESSAGES_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.1.220 (external, cli)",
        },
        data=body,
    )
    if attempt is not None:
        attempt["last_status"] = status
    if status != 200:
        return None
    five_hour = _claude_header_window(headers, "5h")
    seven_day = _claude_header_window(headers, "7d")
    if not five_hour and not seven_day:
        return None
    return {"five_hour": five_hour, "seven_day": seven_day, "updated_at": utc_now_iso()}


def fetch_codex_official_usage(token: str, account_id: str) -> dict[str, Any] | None:
    """Call the same account-usage endpoint the Codex CLI itself reads.

    Same caveat as fetch_claude_official_usage: unofficial endpoint, fail soft.
    Weekly-only plans expose primary_window with limit_window_seconds=604800.
    """
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "codex-cli"}
    if account_id:
        headers["chatgpt-account-id"] = account_id
    payload, status = _http_get_json_result(CODEX_USAGE_URL, headers=headers)
    if status == 429 or not payload:
        return None
    rate_limit = payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {}
    if not rate_limit:
        return None
    five_hour, seven_day = parse_codex_rate_limit_windows(rate_limit)
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
    reset = iso_time(raw.get("resets_at") or raw.get("reset_at"))
    if reset:
        result["resets_at"] = reset
    return result


def _codex_window_kind(section: dict[str, Any]) -> str | None:
    """Classify Codex rate-limit slot by window length (seconds).

    Official wham/usage uses ~18000 for 5h and ~604800 for 7d. Weekly-only
    plans may expose only primary_window with the weekly duration.
    """
    secs = integer(section.get("limit_window_seconds"))
    if secs is None:
        return None
    if secs <= 36 * 3600:
        return "five_hour"
    if secs >= 2 * 86400:
        return "seven_day"
    return None


def _reset_horizon_seconds(window: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    now = now or dt.datetime.now(dt.timezone.utc)
    reset = window.get("resets_at")
    if not reset:
        return None
    try:
        when = dt.datetime.fromisoformat(str(reset).replace("Z", "+00:00"))
        return (when - now).total_seconds()
    except ValueError:
        return None


def parse_codex_rate_limit_windows(rate_limit: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map Codex primary/secondary slots to five_hour / seven_day buckets."""
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    seen: set[int] = set()
    slots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for key in ("primary_window", "primary", "secondary_window", "secondary"):
        raw = rate_limit.get(key)
        if not isinstance(raw, dict):
            continue
        ident = id(raw)
        if ident in seen:
            continue
        seen.add(ident)
        parsed = codex_window(raw)
        if parsed:
            slots.append((raw, parsed))

    five_hour: dict[str, Any] = {}
    seven_day: dict[str, Any] = {}
    unclassified: list[dict[str, Any]] = []
    for raw, parsed in slots:
        kind = _codex_window_kind(raw)
        if kind == "five_hour":
            five_hour = parsed
        elif kind == "seven_day":
            seven_day = parsed
        else:
            unclassified.append(parsed)

    for parsed in sorted(
        unclassified,
        key=lambda w: _reset_horizon_seconds(w) if _reset_horizon_seconds(w) is not None else 0,
    ):
        horizon = _reset_horizon_seconds(parsed)
        if horizon is not None and horizon > 48 * 3600 and not seven_day:
            seven_day = parsed
        elif not five_hour:
            five_hour = parsed
        elif not seven_day:
            seven_day = parsed

    return five_hour, seven_day


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
                five_hour, seven_day = parse_codex_rate_limit_windows(rate_limits)
                quota = {
                    "five_hour": five_hour,
                    "seven_day": seven_day,
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
        cached = _get_cached_official("codex")
        cache_age = _cache_age_seconds(_read_official_cache().get("codex"))
        official = None
        if cached and cache_age is not None and cache_age < OFFICIAL_MIN_INTERVAL_SEC:
            official = cached
        else:
            creds = read_codex_oauth(auth_path or default_codex_auth_path())
            if creds:
                official = fetch_codex_official_usage(*creds)
            if official:
                _store_cached_official("codex", official)
            elif cached:
                official = cached
        if official:
            # Keep token/context counters from the local session scan —
            # the official endpoint only reports quota %, not context size.
            merged = dict(official)
            for key in ("latest_tokens", "total_tokens", "context_window_tokens"):
                if quota and key in quota:
                    merged[key] = quota[key]
            quota = merged
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
    """Build ccusage argv.

    systemd ``Environment=CCUSAGE_COMMAND=...`` cannot carry unquoted spaces, so
    the unit often collapses to just ``/usr/bin/ccusage``. Treat a bare binary
    as a prefix and append the standard ``blocks`` flags.
    """
    default_flags = [
        "blocks", "--active", "--json", "--offline", "--recent",
        "--timezone", timezone,
    ]
    configured = os.environ.get("CCUSAGE_COMMAND", "").strip()
    if configured:
        parts = shlex.split(configured, posix=os.name != "nt")
        if parts and "blocks" not in parts:
            return parts + default_flags
        # Ensure timezone flag present when caller omitted it
        if parts and "--timezone" not in parts:
            parts = parts + ["--timezone", timezone]
        return parts
    binary = shutil.which("ccusage") or shutil.which("ccusage.cmd")
    if binary:
        return [binary] + default_flags
    runner = shutil.which("npx") or shutil.which("npx.cmd") or "npx"
    return [runner, "-y", "ccusage@latest"] + default_flags


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


def _limit_error_texts(row: dict[str, Any]) -> list[str]:
    """Read error fields, never ordinary assistant/user conversation content."""
    texts: list[str] = []

    def error_text(value: Any) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            for key in ("message", "error", "detail", "type", "code"):
                if isinstance(value.get(key), str):
                    texts.append(value[key])

    error_text(row.get("error"))
    if row.get("type") == "error" or row.get("is_error") is True or row.get("isApiErrorMessage") is True:
        if row.get("status") in (429, "429") or row.get("status_code") in (429, "429"):
            texts.append("429")
        message = row.get("message")
        error_text(message)
        content = message.get("content") if isinstance(message, dict) else row.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    texts.append(item["text"])
    return texts


def _safe_limit_reset_text(text: str) -> str:
    """Keep a short provider time hint, not the rest of an error or JSONL row."""
    if re.search(r"authorization|bearer\b|token\b|api[_ -]?key|https?://|[A-Za-z]:[\\/]|/(?:root|home|opt|tmp|var)/", text, re.IGNORECASE):
        return ""
    clock = r"(?:(?:0?[1-9]|1[0-2])(?::[0-5][0-9])?[ \t]*[ap]m|(?:[01]?[0-9]|2[0-3]):[0-5][0-9])"
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    date = rf"{month}[ \t]+(?:[12][0-9]|3[01]|0?[1-9])(?:,[ \t]*[0-9]{{4}})?(?:[ \t]+(?:at[ \t]+)?{clock})?"
    unit = r"[0-9]{1,3}[ \t]*(?:seconds?|minutes?|hours?|days?)"
    duration = rf"{unit}(?:(?:[ \t]+(?:and[ \t]+)?|,[ \t]*(?:and[ \t]+)?){unit}){{0,3}}"
    hint = re.search(
        rf"\b(?:resets?|try[ \t]+again)[ \t]*(?:(?:at|on|after|in)[ \t]+|:[ \t]*)?(?:{date}|{clock}|{duration})\b",
        text,
        re.IGNORECASE,
    )
    if not hint or re.match(r"[ \t]*(?:[,/+:&-]|(?:and|or)\b|[0-9])", text[hint.end():]):
        # Never turn an unsupported compound time into an earlier partial reset.
        return ""
    return re.sub(r"[ \t]+", " ", hint.group(0)).strip()[:120]


def rate_limit_detail(text: str, observed_at: str) -> dict[str, Any] | None:
    if len(text) > 500:
        return None
    if re.search(r"\[\[?(?:DIARY|THOUGHT|SAVE)\b", text, re.IGNORECASE):
        return None
    row = parse_json_line(text)
    if row is None:
        return None
    texts = _limit_error_texts(row)
    if any(re.search(r"\[\[?(?:DIARY|THOUGHT|SAVE)\b", item, re.IGNORECASE) for item in texts):
        return None
    lowered = " ".join(texts).lower().replace("_", " ").replace("-", " ")
    weekly = bool(re.search(r"\bweekly(?: usage)? limit\b", lowered))
    opus = bool(re.search(r"\bopus(?: usage)? limit\b", lowered))
    explicit_usage = bool(re.search(
        r"\b(?:(?:usage|quota) limit(?: (?:has been|is))? (?:reached|exhausted|exceeded)"
        r"|(?:reached|exceeded|exhausted) (?:your |the )?(?:usage|quota)(?: limit)?"
        r"|(?:usage|quota)(?: (?:is|has been))? (?:exhausted|exceeded))\b",
        lowered,
    ))
    reached_own_limit = bool(re.search(r"\byou(?:'ve|’ve| have) (?:hit|reached) your (?:usage )?limit\b", lowered))
    request_frequency = bool(re.search(
        r"\b(?:request|token)s? (?:rate limit|(?:per|a|each) (?:second|minute|hour|day|sec|min|hr)s?)\b"
        r"|\b(?:requests|tokens)/(?:s|m|h|d|second|minute|hour|day)\b",
        lowered,
    ))
    exhausted = weekly or opus or ((explicit_usage or reached_own_limit) and not request_frequency)
    if not exhausted and not request_frequency and not any(marker in lowered for marker in RATE_LIMIT_MARKERS) and not re.search(r"\b429\b", lowered):
        return None
    kind = "weekly" if weekly else "opus" if opus else "rate_limit"
    reset_text = next((hint for item in texts if (hint := _safe_limit_reset_text(item))), "")
    return {
        "kind": kind,
        "exhausted": exhausted,
        "reset_text": reset_text,
        "observed_at": observed_at,
    }


def _claude_event_time(value: Any) -> dt.datetime:
    """Compare Claude event instants without changing their serialized timestamps."""
    return dt.datetime.fromisoformat(iso_time(value, "1970-01-01T00:00:00Z").replace("Z", "+00:00"))


def scan_claude_projects(projects_dir: Path, file_limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    sessions: list[dict[str, Any]] = []
    newest_limit: dict[str, Any] | None = None
    for path in recent_jsonl(projects_dir, file_limit):
        session: dict[str, Any] | None = None
        project_limit: dict[str, Any] | None = None
        lines_seen = 0
        for line in reverse_lines(path):
            lines_seen += 1
            if lines_seen > 12000:
                break
            row = parse_json_line(line)
            if not row:
                continue
            observed_at = iso_time(row.get("timestamp"), iso_time(path.stat().st_mtime))
            if project_limit is None:
                project_limit = rate_limit_detail(line, observed_at)
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
            if session is not None and project_limit is not None:
                break
        # File mtime chooses the bounded scan set, not the newest limit event.
        if project_limit and (
            newest_limit is None
            or _claude_event_time(project_limit["observed_at"]) > _claude_event_time(newest_limit["observed_at"])
        ):
            newest_limit = project_limit
        if session:
            sessions.append({key: value for key, value in session.items() if value not in (None, "")})
    sessions.sort(key=lambda session: _claude_event_time(session.get("updated_at")), reverse=True)
    return sessions[:6], newest_limit


def collect_claude(
    projects_dir: Path,
    timezone: str,
    credentials_path: Path | None = None,
    use_official: bool = False,
) -> dict[str, Any]:
    sessions, effective_limit = scan_claude_projects(projects_dir)

    official = None
    source = "unavailable"
    if use_official:
        cached = _get_cached_official("claude")
        cached_source = _cached_official_source("claude")
        entry = _read_official_cache().get("claude")
        cache_age = _cache_age_seconds(entry)
        # Throttle: Claude oauth/usage 429s under ~5min dual timers (双子续杯: 10–15min).
        if _claude_cooldown_active(entry) or (cached and cache_age is not None and cache_age < OFFICIAL_MIN_INTERVAL_SEC):
            official = cached
            source = cached_source
        else:
            token = read_claude_oauth_token(credentials_path or default_claude_credentials_path())
            if token:
                attempt: dict[str, Any] = {}
                official = fetch_claude_official_usage(token, attempt=attempt)
                if official:
                    _store_cached_official("claude", official)
                    source = "claude_oauth_usage"
                else:
                    # Persist the primary failure before the one allowed fallback probe.
                    _store_claude_failure(attempt)
                    header_attempt: dict[str, Any] = {}
                    official = fetch_claude_api_headers_usage(token, attempt=header_attempt)
                    if official:
                        _store_cached_official(
                            "claude",
                            official,
                            source="claude_api_headers",
                            preserve_retry=True,
                        )
                        source = "claude_api_headers"
            if not official and cached:
                # Reuse the last authoritative quota after primary/probe failure.
                official = cached
                source = cached_source

    if official:
        quota = dict(official)
    else:
        block = read_ccusage_block(timezone)
        quota = {
            "five_hour": claude_window(block) if block else {},
            "seven_day": {},
            "updated_at": utc_now_iso(),
        }
        source = "ccusage_blocks" if block else "claude_project_jsonl" if effective_limit else "unavailable"

    if effective_limit and sessions:
        newest = max(_claude_event_time(s.get("updated_at")) for s in sessions)
        if newest > _claude_event_time(effective_limit.get("observed_at")):
            effective_limit = None

    # JSONL limit events are independent of official quota and ccusage reset time.
    # Only a strictly newer normal usage/session clears the event above.
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
