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


def _latest_heart_rate(payload: Any) -> tuple[float | int, tuple[int, str, str] | None] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("latest_hr"), dict):
        return None
    latest = payload["latest_hr"]
    bpm = _number(latest.get("bpm"))
    if bpm is None:
        return None
    return bpm, _timestamp(latest.get("time"))


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
        if metric == "heart_rate":
            latest = _latest_heart_rate(payload)
            if latest is not None:
                value, latest_timestamp = latest
                if latest_timestamp is not None:
                    _, sampled_at, data_date = latest_timestamp
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


_MENSTRUATION_STATUS = {
    1: "period_start",
    2: "period_end",
    3: "period_start_end",
}
_HP_ENUM = {0: "little", 1: "normal", 2: "much"}
_MOOD_ENUM = {0: "happy", 1: "normal", 2: "uncomfortable"}
_PAIN_ENUM = {0: "light", 1: "normal", 2: "heavy"}


def _cycle_epoch(value: Any, field: str) -> str:
    if type(value) is not int or value <= 0:
        raise MalformedHealthResponse(f"invalid cycle {field}")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError) as exc:
        raise MalformedHealthResponse(f"invalid cycle {field}") from exc


def _cycle_value(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MalformedHealthResponse("malformed cycle value") from exc
    if not isinstance(value, dict):
        raise MalformedHealthResponse("malformed cycle value")
    return value


def parse_menstruation_rows(rows: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize recorded Xiaomi cycle events and pair explicit start/end events."""
    if not isinstance(rows, list):
        raise MalformedHealthResponse("malformed menstruation rows")
    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or "value" not in row:
            raise MalformedHealthResponse("malformed menstruation row")
        value = _cycle_value(row["value"])
        status = value.get("status")
        if type(status) is not int or status not in _MENSTRUATION_STATUS:
            raise MalformedHealthResponse("unknown menstruation status")
        timestamp = _cycle_epoch(value.get("date_time"), "date_time")
        updated_at = _cycle_epoch(value.get("update_time"), "update_time")
        events.append({
            "type": _MENSTRUATION_STATUS[status],
            "timestamp": timestamp,
            "updated_at": updated_at,
        })

    events.sort(key=lambda event: event["timestamp"])
    periods: list[dict[str, Any]] = []
    unmatched_starts: list[dict[str, Any]] = []
    for event in events:
        if event["type"] == "period_start":
            unmatched_starts.append({
                "start": event["timestamp"],
                "end": None,
                "open": True,
                "source": "recorded",
            })
        elif event["type"] == "period_start_end":
            periods.append({
                "start": event["timestamp"],
                "end": event["timestamp"],
                "open": False,
                "source": "recorded",
            })
        elif unmatched_starts:
            period = unmatched_starts.pop()
            period["end"] = event["timestamp"]
            period["open"] = False
            periods.append(period)

    periods.extend(unmatched_starts)
    periods.sort(key=lambda period: (period["start"], period["end"] or ""))
    return {"events": events, "periods": periods}


def _cycle_enum(value: Any, values: dict[int, str]) -> str | None:
    return values.get(value) if type(value) is int else None


def parse_menstrual_symptoms_rows(rows: Any) -> list[dict[str, Any]]:
    """Normalize only the published Xiaomi symptom enum fields; unknown values become null."""
    if not isinstance(rows, list):
        raise MalformedHealthResponse("malformed menstrual symptom rows")
    output: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or "value" not in row:
            raise MalformedHealthResponse("malformed menstrual symptom row")
        value = _cycle_value(row["value"])
        output.append({
            "timestamp": _cycle_epoch(value.get("date_time"), "date_time"),
            "hp": _cycle_enum(value.get("hp"), _HP_ENUM),
            "mood": _cycle_enum(value.get("mood"), _MOOD_ENUM),
            "pain": _cycle_enum(value.get("pain"), _PAIN_ENUM),
        })
    output.sort(key=lambda symptom: symptom["timestamp"])
    return output

