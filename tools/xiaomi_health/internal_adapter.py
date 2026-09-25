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
            latest = client.get_latest(days)
        except XiaomiProviderError as exc:
            return {"status": "FAIL", "provider": SOURCE, "error_code": exc.code if exc.code in SAFE_ERRORS else "unavailable"}
        except Exception:
            return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
        if not isinstance(latest, dict):
            return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
        try:
            cycle = client.get_cycle(180)
        except XiaomiProviderError as exc:
            cycle = _safe_cycle_failure(exc.code)
        except Exception:
            cycle = _safe_cycle_failure("unavailable")
        return {**latest, "cycle": cycle}
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


