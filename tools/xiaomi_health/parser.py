"""Strict, credential-free normalization for Xiaomi daily health records."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone, timedelta
from typing import Any


UTC_PLUS_8 = timezone(timedelta(hours=8))
METRIC_FIELDS = {
    "steps": ("steps", "step", "step_count", "stepcount", "total_steps", "count", "value"),
    "sleep": ("asleep_minutes", "time_asleep_minutes", "sleep_minutes", "total_sleep_minutes", "total_sleep", "total_sleep_time", "sleep_duration", "duration_minutes", "duration", "value"),
    "heart_rate": ("bpm", "heart_rate", "avg_hrm", "avg_heart_rate", "average_heart_rate", "resting_heart_rate", "value"),
}
UNITS = {"steps": "steps", "sleep": "minutes", "heart_rate": "bpm"}
DETAIL_FIELDS = {
    "sleep": ("asleep_minutes", "time_asleep_minutes", "sleep_minutes", "total_sleep_minutes", "sleep_duration", "duration_minutes", "duration", "awake_minutes", "awake_duration", "sleep_awake_duration", "deep_sleep", "light_sleep", "rem_sleep", "sleep_score", "score"),
    "heart_rate": ("bpm", "heart_rate", "avg_hrm", "avg_heart_rate", "average_heart_rate", "min_heart_rate", "max_heart_rate", "resting_heart_rate"),
}


class MalformedHealthResponse(ValueError):
    pass


def _timestamp(value: Any) -> tuple[int, str, str] | None:
    try:
        if isinstance(value, str) and not value.strip().isdigit():
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC_PLUS_8)
            seconds = int(parsed.timestamp())
        else:
            seconds = int(float(value))
            if abs(seconds) > 10_000_000_000:
                seconds //= 1000
        if seconds <= 0:
            return None
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return seconds, dt.isoformat(timespec="seconds").replace("+00:00", "Z"), dt.astimezone(UTC_PLUS_8).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _embedded(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        try:
            return float(candidate)
        except ValueError:
            return None


def _number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n):
        return None
    return int(n) if n.is_integer() else n


def _metric_value(metric: str, payload: Any) -> float | int | None:
    if isinstance(payload, dict):
        if metric == "sleep":
            for key in ("asleep_minutes", "time_asleep_minutes", "sleep_minutes", "total_sleep_minutes", "total_sleep", "total_sleep_time"):
                value = _number(payload.get(key))
                if value is not None:
                    return value
            duration = None
            for key in ("duration_minutes", "sleep_duration", "duration"):
                duration = _number(payload.get(key))
                if duration is not None:
                    break
            awake = None
            for key in ("awake_minutes", "awake_duration", "sleep_awake_duration"):
                awake = _number(payload.get(key))
                if awake is not None:
                    break
            if duration is not None:
                return max(0, duration - (awake or 0))
        for key in METRIC_FIELDS[metric]:
            if key in payload:
                number = _number(payload[key])
                if number is not None:
                    return number
        return None
    return _number(payload)


def _safe_details(metric: str, payload: Any) -> dict[str, float | int]:
    if metric not in DETAIL_FIELDS or not isinstance(payload, dict):
        return {}
    output: dict[str, float | int] = {}
    for key in DETAIL_FIELDS[metric]:
        if key in payload:
            value = _number(payload[key])
            if value is not None:
                output[key] = value
    return output


def parse_series_response(response: Any, metric: str, *, days: int, now: datetime | None = None) -> list[dict[str, Any]]:
    if metric not in METRIC_FIELDS:
        raise MalformedHealthResponse("unsupported health metric")
    if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
        raise MalformedHealthResponse("malformed Xiaomi response")
    data_list = response["result"].get("data_list")
    if not isinstance(data_list, list):
        raise MalformedHealthResponse("malformed Xiaomi data list")
    output: list[dict[str, Any]] = []
    for item in data_list:
        if not isinstance(item, dict):
            raise MalformedHealthResponse("malformed Xiaomi data row")
        timestamp = _timestamp(item.get("time"))
        if timestamp is None:
            raise MalformedHealthResponse("malformed Xiaomi data timestamp")
        seconds, sampled_at, data_date = timestamp
        if not isinstance(item.get("value"), (str, int, float, dict)):
            raise MalformedHealthResponse("malformed Xiaomi data value")
        payload = _embedded(item.get("value"))
        value = _metric_value(metric, payload)
        record: dict[str, Any] = {
            "sampledAt": sampled_at,
            "dataDate": data_date,
            "value": value,
            "unit": UNITS[metric],
        }
        details = _safe_details(metric, payload)
        if details:
            record["details"] = details
        output.append(record)
    output.sort(key=lambda row: row["sampledAt"])
    if len(output) > days:
        output = output[-days:]
    return output


def latest_date(records: list[dict[str, Any]]) -> str | None:
    dates = [row.get("dataDate") for row in records if isinstance(row.get("dataDate"), str)]
    return max(dates) if dates else None
