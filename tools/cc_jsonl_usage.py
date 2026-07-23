"""Read-only Claude session JSONL usage replay.

Claude Code can emit the same assistant usage row more than once. Billing
and cache accounting therefore use requestId as the identity key and never
sum raw JSONL rows directly.
"""
from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def claude_project_slug(cwd: str) -> str:
    """Match Claude Code's project-directory encoding."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(str(cwd or "")))


def session_jsonl_path(
    cwd: str,
    session_id: Optional[str],
    *,
    claude_home: Optional[str] = None,
) -> Optional[Path]:
    if not session_id:
        return None
    home = Path(claude_home) if claude_home else Path.home() / ".claude"
    return home / "projects" / claude_project_slug(cwd) / (str(session_id) + ".jsonl")


def snapshot_session_jsonl(
    cwd: str,
    session_id: Optional[str],
    *,
    claude_home: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    path = session_jsonl_path(cwd, session_id, claude_home=claude_home)
    if path is None:
        return None
    try:
        offset = path.stat().st_size
    except OSError:
        offset = 0
    return {"path": str(path), "offset": int(offset)}


def _request_record(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if row.get("type") != "assistant":
        return None
    request_id = row.get("requestId")
    if request_id is None:
        request_id = row.get("request_id")
    request_id = str(request_id or "").strip()
    if not request_id:
        return None
    message = row.get("message")
    if not isinstance(message, Mapping):
        return None
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return None
    detail = usage.get("cache_creation")
    if not isinstance(detail, Mapping):
        detail = {}
    return {
        "request_id": request_id,
        "input_tokens": _as_int(usage.get("input_tokens")),
        "output_tokens": _as_int(usage.get("output_tokens")),
        "cache_read": _as_int(usage.get("cache_read_input_tokens")),
        "cache_creation": _as_int(usage.get("cache_creation_input_tokens")),
        "cache_creation_5m": _as_int(detail.get("ephemeral_5m_input_tokens")),
        "cache_creation_1h": _as_int(detail.get("ephemeral_1h_input_tokens")),
        "model": str(message.get("model") or row.get("model") or "").strip() or None,
        "timestamp": row.get("timestamp"),
    }


_NUMERIC_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read",
    "cache_creation",
    "cache_creation_5m",
    "cache_creation_1h",
)


def _accounting_tuple(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(record.get(key) for key in _NUMERIC_FIELDS) + (record.get("model"),)


def _ttl_buckets_valid(record: Mapping[str, Any]) -> bool:
    creation = _as_int(record.get("cache_creation"))
    bucket_sum = _as_int(record.get("cache_creation_5m")) + _as_int(record.get("cache_creation_1h"))
    return creation > 0 and bucket_sum == creation


def _authoritative_score(record: Mapping[str, Any]) -> tuple[int, int, str]:
    valid = 1 if _ttl_buckets_valid(record) else 0
    total = sum(_as_int(record.get(key)) for key in _NUMERIC_FIELDS)
    timestamp = str(record.get("timestamp") or "")
    return valid, total, timestamp


def _pick_authoritative_duplicate(
    first: Mapping[str, Any],
    later: Mapping[str, Any],
) -> dict[str, Any]:
    """Pick one whole record; never merge numeric fields across conflicts."""
    if _authoritative_score(later) > _authoritative_score(first):
        return dict(later)
    if _authoritative_score(later) < _authoritative_score(first):
        return dict(first)
    return dict(later)


def pick_authoritative_duplicate_record(
    first: Mapping[str, Any],
    later: Mapping[str, Any],
) -> dict[str, Any]:
    """Public helper for wake/jsonl duplicate requestId resolution."""
    return _pick_authoritative_duplicate(first, later)


def replay_jsonl_lines(
    lines: Iterable[str],
    *,
    known_request_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    known = {str(x) for x in (known_request_ids or ()) if str(x)}
    unique: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    invalid_rows = 0
    assistant_usage_rows = 0
    duplicate_rows = 0
    conflicting_duplicates = 0

    for raw in lines:
        try:
            row = json.loads(raw)
        except Exception:
            invalid_rows += 1
            continue
        if not isinstance(row, Mapping):
            invalid_rows += 1
            continue
        record = _request_record(row)
        if record is None:
            continue
        assistant_usage_rows += 1
        request_id = record["request_id"]
        if request_id in known:
            duplicate_rows += 1
            continue
        if request_id in unique:
            duplicate_rows += 1
            previous = unique[request_id]
            if _accounting_tuple(previous) != _accounting_tuple(record):
                conflicting_duplicates += 1
                unique[request_id] = _pick_authoritative_duplicate(previous, record)
            continue
        unique[request_id] = record

    records = list(unique.values())
    totals = {
        key: sum(_as_int(record.get(key)) for record in records)
        for key in _NUMERIC_FIELDS
    }
    models = sorted({str(r["model"]) for r in records if r.get("model")})
    return {
        "request_ids": list(unique.keys()),
        "request_count": len(records),
        "records": records,
        "totals": totals,
        "models": models,
        "assistant_usage_rows": assistant_usage_rows,
        "duplicate_rows_ignored": duplicate_rows,
        "conflicting_duplicate_rows": conflicting_duplicates,
        "invalid_json_rows": invalid_rows,
    }


def replay_jsonl_path(
    path: os.PathLike[str] | str,
    *,
    offset: int = 0,
    known_request_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    p = Path(path)
    start = max(0, int(offset or 0))
    with p.open("r", encoding="utf-8", errors="replace") as handle:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if start > size:
            start = 0
        handle.seek(start)
        result = replay_jsonl_lines(handle, known_request_ids=known_request_ids)
        result["path"] = str(p)
        result["start_offset"] = start
        result["end_offset"] = handle.tell()
        return result


def replay_session_jsonl(
    cwd: str,
    session_id: Optional[str],
    *,
    cursor: Optional[Mapping[str, Any]] = None,
    claude_home: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    path = session_jsonl_path(cwd, session_id, claude_home=claude_home)
    if path is None or not path.is_file():
        return None
    offset = 0
    if cursor and str(cursor.get("path") or "") == str(path):
        offset = _as_int(cursor.get("offset"))
    return replay_jsonl_path(path, offset=offset)


def replay_jsonl_paths(paths: Sequence[os.PathLike[str] | str]) -> dict[str, Any]:
    """Replay history across files while deduping requestId globally."""
    valid_paths = [Path(raw_path) for raw_path in paths]
    valid_paths = [path for path in valid_paths if path.is_file()]

    def _lines():
        for path in valid_paths:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                yield from handle

    result = replay_jsonl_lines(_lines())
    result["file_count"] = len(valid_paths)
    return result


def attach_jsonl_usage(
    usage: Mapping[str, Any],
    replay: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Add request identity and TTL buckets without replacing stream totals."""
    out = dict(usage or {})
    if not replay or not replay.get("request_count"):
        return out
    totals = dict(replay.get("totals") or {})
    records = list(replay.get("records") or [])
    request_ids = list(replay.get("request_ids") or [])
    out["request_ids"] = request_ids
    out["request_count"] = len(request_ids)
    out["cache_creation_5m"] = _as_int(totals.get("cache_creation_5m"))
    out["cache_creation_1h"] = _as_int(totals.get("cache_creation_1h"))

    rounds = [dict(row) for row in (out.get("rounds") or []) if isinstance(row, Mapping)]
    if len(rounds) == len(records):
        for round_row, record in zip(rounds, records):
            round_row["request_id"] = record.get("request_id")
            round_row["cache_creation_5m"] = _as_int(record.get("cache_creation_5m"))
            round_row["cache_creation_1h"] = _as_int(record.get("cache_creation_1h"))
            if record.get("model"):
                round_row["model"] = record.get("model")
        out["rounds"] = rounds

    stream_totals = {
        key: _as_int(out.get(key))
        for key in ("input_tokens", "output_tokens", "cache_read", "cache_creation")
    }
    jsonl_totals = {key: _as_int(totals.get(key)) for key in stream_totals}
    out["jsonl_usage"] = {
        "source": "claude_session_jsonl",
        "request_count": len(request_ids),
        "duplicate_rows_ignored": _as_int(replay.get("duplicate_rows_ignored")),
        "conflicting_duplicate_rows": _as_int(replay.get("conflicting_duplicate_rows")),
        "invalid_json_rows": _as_int(replay.get("invalid_json_rows")),
        "cache_creation_5m": out["cache_creation_5m"],
        "cache_creation_1h": out["cache_creation_1h"],
        "stream_totals_match": stream_totals == jsonl_totals,
    }
    models = list(replay.get("models") or [])
    if len(models) == 1:
        out["_obs_model"] = models[0]
    elif models:
        out["_obs_model"] = json.dumps(models, ensure_ascii=False, separators=(",", ":"))
    return out
