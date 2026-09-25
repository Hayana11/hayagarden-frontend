"""Private JSON adapter behind Internal MCP's single Xiaomi health capability."""
from __future__ import annotations

import json
import sys
from typing import Any

from .client import XiaomiHealthClient, XiaomiProviderError
from .store import DEFAULT_CREDENTIAL_PATH, SOURCE, XiaomiCredentialStore


OPERATIONS = frozenset({"get_health"})
METRICS = frozenset({"all", "status", "steps", "sleep", "heart_rate", "cycle"})
SAFE_ERRORS = frozenset({"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"})
ALL_UPSTREAM_TIMEOUT_SECONDS = 6.0
MAX_UPSTREAM_REQUESTS_FOR_ALL = 5


def run(
    operation: str,
    *,
    metric: str = "all",
    days: int | None = None,
    store: XiaomiCredentialStore | None = None,
    client: XiaomiHealthClient | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    if not isinstance(metric, str) or metric not in METRICS:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "malformed_response"}
    if days is None:
        days = 180 if metric == "cycle" else 7
    maximum_days = 365 if metric == "cycle" else 30
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= maximum_days:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "malformed_response"}
    store = store or XiaomiCredentialStore(DEFAULT_CREDENTIAL_PATH)
    if metric == "status":
        return store.status()
    client = client or XiaomiHealthClient(store)
    if metric == "all":
        try:
            latest = client.get_latest_partial(days, request_timeout=ALL_UPSTREAM_TIMEOUT_SECONDS)
        except XiaomiProviderError as exc:
            return {"status": "FAIL", "provider": SOURCE, "error_code": exc.code if exc.code in SAFE_ERRORS else "unavailable"}
        except Exception:
            return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
        if not isinstance(latest, dict):
            return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
        try:
            cycle = client.get_cycle(180, request_timeout=ALL_UPSTREAM_TIMEOUT_SECONDS)
        except XiaomiProviderError as exc:
            cycle = _safe_cycle_failure(exc.code)
        except Exception:
            cycle = _safe_cycle_failure("unavailable")
        return _compose_all(latest, cycle)
    method = (lambda: client.get_cycle(days)) if metric == "cycle" else (lambda: client.get_series(metric, days))
    try:
        return method()
    except XiaomiProviderError as exc:
        return {"status": "FAIL", "provider": SOURCE, "error_code": exc.code if exc.code in SAFE_ERRORS else "unavailable"}
    except Exception:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}


def _safe_cycle_failure(code: str) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "provider": SOURCE,
        "source": SOURCE,
        "metric": "cycle",
        "days": 180,
        "events": [],
        "periods": [],
        "symptoms": [],
        "predictions": None,
        "error_code": code if code in SAFE_ERRORS else "unavailable",
    }


def _safe_component_status(entry: Any, *, fallback: str | None = None) -> dict[str, str]:
    if isinstance(entry, dict):
        status = entry.get("status") if entry.get("status") in {"PASS", "EMPTY", "FAIL"} else "FAIL"
        output = {"status": status}
        if status == "FAIL":
            output["error_code"] = entry.get("error_code") if entry.get("error_code") in SAFE_ERRORS else "unavailable"
        return output
    status = fallback if fallback in {"PASS", "EMPTY", "FAIL"} else "FAIL"
    output = {"status": status}
    if status == "FAIL":
        output["error_code"] = "unavailable"
    return output


def _cycle_has_data(cycle: Any) -> bool:
    if not isinstance(cycle, dict):
        return False
    return any(isinstance(cycle.get(key), list) and cycle.get(key) for key in ("events", "periods", "symptoms"))


def _compose_all(latest: dict[str, Any], cycle: dict[str, Any]) -> dict[str, Any]:
    raw_status = latest.get("metric_status") if isinstance(latest.get("metric_status"), dict) else {}
    metric_status = {}
    for name in ("steps", "sleep", "heart_rate"):
        record = latest.get(name)
        if isinstance(record, dict):
            metric_status[name] = {"status": "PASS"}
        else:
            metric_status[name] = _safe_component_status(raw_status.get(name), fallback="EMPTY")
    metric_status["cycle"] = _safe_component_status(
        {"status": cycle.get("status"), "error_code": cycle.get("error_code")},
        fallback="FAIL",
    )
    has_data = any(isinstance(latest.get(name), dict) for name in ("steps", "sleep", "heart_rate")) or _cycle_has_data(cycle)
    any_fail = any(item.get("status") == "FAIL" for item in metric_status.values())
    if has_data:
        status = "PASS"
    elif any_fail:
        status = "FAIL"
    else:
        status = "EMPTY"
    payload = {key: value for key, value in latest.items() if key != "metric_status"}
    result = {
        **payload,
        "cycle": cycle,
        "metric_status": metric_status,
        "status": status,
        "partial": bool(has_data and any_fail),
    }
    if status == "FAIL":
        result["error_code"] = next(
            (item["error_code"] for item in metric_status.values() if item.get("status") == "FAIL" and item.get("error_code") in SAFE_ERRORS),
            "unavailable",
        )
    return result

def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        operation = payload.get("operation") if isinstance(payload, dict) else None
        metric = payload.get("metric", "all") if isinstance(payload, dict) else "all"
        days = payload.get("days") if isinstance(payload, dict) else None
        result = run(operation, metric=metric, days=days)
    except Exception:
        result = {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


