"""Provider-neutral adapter behind Internal MCP's unchanged health capability."""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from typing import Any

from .client import XiaomiHealthClient, XiaomiProviderError
from .store import DEFAULT_CREDENTIAL_PATH, SOURCE, XiaomiCredentialStore
from tools import health_store

OPERATIONS = frozenset({"get_health"})
METRICS = frozenset({"all", "status", "steps", "sleep", "heart_rate", "cycle"})
HEALTH_METRICS = ("steps", "sleep", "heart_rate")
SAFE_ERRORS = frozenset({"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"})
ALL_UPSTREAM_TIMEOUT_SECONDS = 6.0
MAX_UPSTREAM_REQUESTS_FOR_ALL = 5
LOCAL_DB_PATH = os.environ.get("HEALTH_DB_PATH", "/opt/frontend/health.db")


def run(
    operation: str,
    *,
    metric: str = "all",
    days: int | None = None,
    store: XiaomiCredentialStore | None = None,
    client: XiaomiHealthClient | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        return _failure("unavailable")
    if not isinstance(metric, str) or metric not in METRICS:
        return _failure("malformed_response")
    days_omitted = days is None
    if days is None:
        days = 180 if metric == "cycle" else 7
    maximum_days = 365 if metric == "cycle" else 30
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= maximum_days:
        return _failure("malformed_response")

    store = store or XiaomiCredentialStore(DEFAULT_CREDENTIAL_PATH)
    if metric == "status":
        return _compose_status(store)

    client = client or XiaomiHealthClient(store)
    if metric == "cycle":
        return _cloud_call(lambda: client.get_cycle(days), metric="cycle")

    if metric == "all":
        return _run_all(client, days, days_omitted=days_omitted)
    if metric == "heart_rate":
        return _run_heart_rate(client, days, days_omitted=days_omitted)
    local = _local_metric(metric, days)
    if metric == "sleep":
        local = _with_latest_sleep_heart_rate(local)
    if _local_usable(local):
        return local
    cloud = _cloud_call(lambda: client.get_series(metric, days), metric=metric)
    return _resolve_metric(local, cloud)


def _failure(code: str) -> dict[str, Any]:
    return {"status": "FAIL", "provider": SOURCE, "source": SOURCE, "error_code": code}


def _local_metric(metric: str, days: int) -> dict[str, Any]:
    if not os.path.exists(LOCAL_DB_PATH):
        return {
            "status": "UNAVAILABLE", "provider": "health_connect", "source": "health_connect",
            "metric": metric, "days": days, "records": [], "stale": True,
        }
    return health_store.get_local_metric(LOCAL_DB_PATH, metric, days)


def _local_usable(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == "PASS" and bool(result.get("records"))


def _local_hr_usable(result: Any) -> bool:
    if not isinstance(result, dict) or result.get("status") != "PASS":
        return False
    if result.get("view") == "snapshot":
        return result.get("value") is not None
    if result.get("view") == "daily":
        return bool(result.get("records"))
    return bool(result.get("records"))


def _compact_heart_rate(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("view") == "snapshot":
        return {
            "sampledAt": result.get("sampledAt"),
            "dataDate": result.get("dataDate"),
            "value": result.get("value"),
            "unit": "bpm",
            "source": result.get("source") or "health_connect",
            "provider": result.get("provider") or result.get("source") or "health_connect",
            "stale": result.get("stale") is True,
            "ageSeconds": result.get("ageSeconds"),
            "lastHour": result.get("lastHour") or {"min": None, "max": None, "avg": None, "samples": 0},
            "view": "snapshot",
        }
    return {
        "view": "daily",
        "days": result.get("days"),
        "records": list(result.get("records") or []),
        "source": result.get("source") or "health_connect",
        "provider": result.get("provider") or result.get("source") or "health_connect",
        "stale": result.get("stale") is True,
        "unit": "bpm",
    }


def _attach_sleep_heart_rate(row: dict[str, Any]) -> dict[str, Any]:
    details = row.get("details") if isinstance(row.get("details"), dict) else {}
    start = details.get("startAt") or details.get("start_at")
    end = details.get("endAt") or details.get("end_at")
    if not start or not end:
        return row
    summary = health_store.get_sleep_heart_rate(LOCAL_DB_PATH, start, end)
    if not summary:
        return row
    attached = dict(row)
    attached["heartRate"] = summary
    return attached


def _with_latest_sleep_heart_rate(result: dict[str, Any]) -> dict[str, Any]:
    records = result.get("records")
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return result
    updated = dict(result)
    updated["records"] = [_attach_sleep_heart_rate(records[0]), *records[1:]]
    return updated


def _local_heart_rate(days: int, *, days_omitted: bool) -> dict[str, Any]:
    if not os.path.exists(LOCAL_DB_PATH):
        return (
            health_store.get_heart_rate_snapshot(LOCAL_DB_PATH)
            if days_omitted
            else {"status": "UNAVAILABLE", "view": "daily", "days": days, "records": [], "source": "health_connect"}
        )
    try:
        if days_omitted:
            return health_store.get_heart_rate_snapshot(LOCAL_DB_PATH)
        return health_store.get_heart_rate_daily(LOCAL_DB_PATH, days)
    except (OSError, ValueError):
        return {
            "status": "UNAVAILABLE",
            "view": "snapshot" if days_omitted else "daily",
            "days": None if days_omitted else days,
            "records": [],
            "source": "health_connect",
        }


def _cloud_heart_rate_snapshot(cloud: dict[str, Any]) -> dict[str, Any]:
    records = cloud.get("records") if isinstance(cloud.get("records"), list) else []
    latest = next((row for row in records if isinstance(row, dict) and row.get("value") is not None), None)
    sampled_at = latest.get("sampledAt") if latest else cloud.get("sampledAt")
    age_seconds = None
    if isinstance(sampled_at, str):
        try:
            sampled = _dt.datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
            now = _dt.datetime.now(_dt.timezone.utc)
            if sampled.tzinfo is None:
                sampled = sampled.replace(tzinfo=_dt.timezone.utc)
            age_seconds = max(0, int((now - sampled).total_seconds()))
        except ValueError:
            age_seconds = None
    stale = age_seconds is None or age_seconds > health_store.HEART_RATE_STALE_AFTER_SECONDS
    source = cloud.get("source") or cloud.get("provider") or SOURCE
    return {
        "status": cloud.get("status") or ("PASS" if latest else "EMPTY"),
        "provider": source,
        "source": source,
        "metric": "heart_rate",
        "view": "snapshot",
        "value": latest.get("value") if latest else cloud.get("value"),
        "unit": "bpm",
        "sampledAt": sampled_at,
        "dataDate": (latest.get("dataDate") if latest else None) or cloud.get("dataDate"),
        "ageSeconds": age_seconds,
        "stale": stale,
        "lastHour": {"min": None, "max": None, "avg": None, "samples": 0},
        **({"error_code": cloud["error_code"]} if cloud.get("error_code") else {}),
    }


def _summarize_cloud_heart_rate_daily(cloud: dict[str, Any], days: int) -> dict[str, Any]:
    rows = cloud.get("records") if isinstance(cloud.get("records"), list) else []
    by_date: dict[str, list[float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get("dataDate") or row.get("data_date")
        value = row.get("value")
        if not isinstance(date, str) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        by_date.setdefault(date, []).append(float(value))
    records = []
    for date in sorted(by_date, reverse=True)[:days]:
        values = by_date[date]
        records.append({
            "dataDate": date,
            "min": min(values),
            "max": max(values),
            "sampleCount": len(values),
        })
    source = cloud.get("source") or cloud.get("provider") or SOURCE
    status = cloud.get("status")
    if records:
        status = "PASS"
    elif status not in {"PASS", "EMPTY", "FAIL"}:
        status = "EMPTY"
    return {
        "status": status,
        "provider": source,
        "source": source,
        "metric": "heart_rate",
        "view": "daily",
        "days": days,
        "records": records,
        "stale": cloud.get("stale") is True,
        **({"error_code": cloud["error_code"]} if cloud.get("error_code") else {}),
    }


def _run_heart_rate(client: XiaomiHealthClient, days: int, *, days_omitted: bool) -> dict[str, Any]:
    local = _local_heart_rate(days, days_omitted=days_omitted)
    if _local_hr_usable(local):
        return local
    cloud = _cloud_call(lambda: client.get_series("heart_rate", days), metric="heart_rate")
    if cloud.get("status") == "FAIL":
        return cloud
    if days_omitted:
        snapshot = _cloud_heart_rate_snapshot(cloud)
        return snapshot if snapshot.get("value") is not None else cloud
    return _summarize_cloud_heart_rate_daily(cloud, days)


def _cloud_call(call: Any, *, metric: str) -> dict[str, Any]:
    try:
        result = call()
        if not isinstance(result, dict):
            return _failure("unavailable")
        result = dict(result)
        result.setdefault("provider", SOURCE)
        result.setdefault("source", SOURCE)
        result.setdefault("metric", metric)
        result.setdefault("stale", False)
        return result
    except XiaomiProviderError as exc:
        return _failure(exc.code if exc.code in SAFE_ERRORS else "unavailable") | {"metric": metric}
    except Exception:
        return _failure("unavailable") | {"metric": metric}


def _resolve_metric(local: dict[str, Any], cloud: dict[str, Any]) -> dict[str, Any]:
    # EMPTY is not a value: it explicitly permits cloud fallback.
    if _local_usable(local):
        return local
    if isinstance(cloud, dict):
        return cloud
    return _failure("unavailable")


def _cloud_latest(client: XiaomiHealthClient, days: int) -> dict[str, Any]:
    try:
        result = client.get_latest_partial(days, request_timeout=ALL_UPSTREAM_TIMEOUT_SECONDS)
        if not isinstance(result, dict):
            return _failure("unavailable")
        result = dict(result)
        result.setdefault("provider", SOURCE)
        result.setdefault("source", SOURCE)
        return result
    except XiaomiProviderError as exc:
        return _failure(exc.code if exc.code in SAFE_ERRORS else "unavailable")
    except Exception:
        return _failure("unavailable")


def _local_latest(metric: str, result: dict[str, Any]) -> dict[str, Any] | None:
    if not _local_usable(result):
        return None
    row = result["records"][0]
    return {
        **row,
        "source": "health_connect",
        "provider": "health_connect",
        "stale": result.get("stale") is True,
        "cached": False,
    }


def _cloud_metric_status(latest: dict[str, Any], metric: str) -> dict[str, Any]:
    if isinstance(latest.get(metric), dict):
        return {"status": "PASS", "source": SOURCE, "stale": False}
    raw = latest.get("metric_status")
    raw = raw.get(metric) if isinstance(raw, dict) else None
    status = raw.get("status") if isinstance(raw, dict) else "EMPTY"
    if latest.get("status") == "FAIL" and status == "EMPTY":
        status = "FAIL"
    if status not in {"PASS", "EMPTY", "FAIL"}:
        status = "FAIL"
    output = {"status": status, "source": SOURCE, "stale": False}
    if status == "FAIL":
        output["error_code"] = raw.get("error_code") if isinstance(raw, dict) else "unavailable"
    return output


def _run_all(client: XiaomiHealthClient, days: int, *, days_omitted: bool) -> dict[str, Any]:
    locals_by_metric = {
        metric: (
            _local_heart_rate(days, days_omitted=days_omitted)
            if metric == "heart_rate"
            else _local_metric(metric, days)
        )
        for metric in HEALTH_METRICS
    }
    needs_cloud = [
        metric
        for metric, value in locals_by_metric.items()
        if not (_local_hr_usable(value) if metric == "heart_rate" else _local_usable(value))
    ]
    latest = _cloud_latest(client, days) if needs_cloud else {"status": "EMPTY", "metric_status": {}}

    metrics: dict[str, Any] = {}
    metric_status: dict[str, Any] = {}
    sources: set[str] = set()
    for metric in HEALTH_METRICS:
        if metric == "heart_rate":
            local_hr = locals_by_metric[metric]
            if _local_hr_usable(local_hr):
                metrics[metric] = _compact_heart_rate(local_hr)
                metric_status[metric] = {
                    "status": "PASS",
                    "source": "health_connect",
                    "stale": local_hr.get("stale") is True,
                }
                sources.add("health_connect")
                continue
            cloud_value = latest.get(metric) if isinstance(latest, dict) else None
            if isinstance(cloud_value, dict):
                if days_omitted:
                    metrics[metric] = {
                        **_cloud_heart_rate_snapshot({"records": [cloud_value], **cloud_value}),
                        "source": SOURCE,
                        "provider": SOURCE,
                    }
                else:
                    metrics[metric] = {
                        **_summarize_cloud_heart_rate_daily({"records": [cloud_value], **cloud_value}, days),
                        "source": SOURCE,
                        "provider": SOURCE,
                    }
            metric_status[metric] = _cloud_metric_status(latest, metric)
            sources.add(SOURCE)
            continue
        local_value = _local_latest(metric, locals_by_metric[metric])
        if local_value is not None:
            if metric == "sleep":
                local_value = _attach_sleep_heart_rate(local_value)
            metrics[metric] = local_value
            metric_status[metric] = {
                "status": "PASS",
                "source": "health_connect",
                "stale": locals_by_metric[metric].get("stale") is True,
            }
            sources.add("health_connect")
        else:
            cloud_value = latest.get(metric) if isinstance(latest, dict) else None
            if isinstance(cloud_value, dict):
                attached = {**cloud_value, "source": SOURCE, "provider": SOURCE, "stale": False}
                if metric == "sleep":
                    attached = _attach_sleep_heart_rate(attached)
                metrics[metric] = attached
            metric_status[metric] = _cloud_metric_status(latest, metric)
            sources.add(SOURCE)

    cycle = _cloud_call(lambda: client.get_cycle(180, request_timeout=ALL_UPSTREAM_TIMEOUT_SECONDS), metric="cycle")
    metric_status["cycle"] = {
        "status": cycle.get("status", "FAIL"),
        "source": SOURCE,
        "stale": cycle.get("stale") is True,
        **({"error_code": cycle["error_code"]} if cycle.get("error_code") else {}),
    }
    payload = {
        key: value
        for key, value in (latest.items() if isinstance(latest, dict) else [])
        if key not in {"metric_status", "steps", "sleep", "heart_rate", "error_code"}
    }
    payload.update(metrics)
    payload["cycle"] = cycle
    payload["metric_status"] = metric_status

    any_data = bool(metrics) or _cycle_has_data(cycle)
    any_fail = any(item.get("status") == "FAIL" for item in metric_status.values())
    payload["status"] = "PASS" if any_data else ("FAIL" if any_fail else "EMPTY")
    payload["partial"] = bool(any_data and any_fail)
    provider = next(iter(sources)) if len(sources) == 1 else "mixed"
    payload["provider"] = provider
    payload["source"] = provider
    payload.pop("error_code", None)
    if payload["status"] == "FAIL":
        payload["error_code"] = next(
            (item.get("error_code") for item in metric_status.values() if item.get("status") == "FAIL"),
            "unavailable",
        )
    return payload


def _cycle_has_data(cycle: Any) -> bool:
    return isinstance(cycle, dict) and any(
        isinstance(cycle.get(key), list) and cycle.get(key)
        for key in ("events", "periods", "symptoms")
    )


def _compose_status(store: XiaomiCredentialStore) -> dict[str, Any]:
    cloud = store.status()
    local = None
    if os.path.exists(LOCAL_DB_PATH):
        try:
            local = health_store.get_status(LOCAL_DB_PATH)
        except Exception:
            local = None
    local_available = bool(local and local.get("available"))
    cloud_valid = isinstance(cloud, dict) and cloud.get("auth_state") == "valid"
    provider = "mixed" if local_available and cloud_valid else ("health_connect" if local_available else SOURCE)
    return {
        "status": "PASS" if local_available or cloud_valid else "EMPTY",
        "provider": provider,
        "source": provider,
        "connected": cloud.get("connected") is True if isinstance(cloud, dict) else False,
        "auth_state": cloud.get("auth_state", "unavailable") if isinstance(cloud, dict) else "unavailable",
        "last_success_at": cloud.get("last_success_at") if isinstance(cloud, dict) else None,
        "last_error": cloud.get("last_error") if isinstance(cloud, dict) else None,
        "mobile": local or {"available": False, "metrics": {}},
        "xiaomi_fitness_cloud": cloud,
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = run(
            payload.get("operation") if isinstance(payload, dict) else None,
            metric=payload.get("metric", "all") if isinstance(payload, dict) else "all",
            days=payload.get("days") if isinstance(payload, dict) else None,
        )
    except Exception:
        result = _failure("unavailable")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
