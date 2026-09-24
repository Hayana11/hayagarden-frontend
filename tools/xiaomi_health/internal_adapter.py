"""Private JSON adapter behind Internal MCP's single Xiaomi health capability."""
from __future__ import annotations

import json
import sys
from typing import Any

from .client import XiaomiHealthClient, XiaomiProviderError
from .store import DEFAULT_CREDENTIAL_PATH, SOURCE, XiaomiCredentialStore


OPERATIONS = frozenset({"get_health"})
METRICS = frozenset({"all", "status", "steps", "sleep", "heart_rate"})
SAFE_ERRORS = frozenset({"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"})


def run(
    operation: str,
    *,
    metric: str = "all",
    days: int = 7,
    store: XiaomiCredentialStore | None = None,
    client: XiaomiHealthClient | None = None,
) -> dict[str, Any]:
    if operation not in OPERATIONS:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    if not isinstance(metric, str) or metric not in METRICS:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "malformed_response"}
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "malformed_response"}
    store = store or XiaomiCredentialStore(DEFAULT_CREDENTIAL_PATH)
    if metric == "status":
        return store.status()
    client = client or XiaomiHealthClient(store)
    if metric == "all":
        method = lambda: client.get_latest(days)
    else:
        method = lambda: client.get_series(metric, days)
    try:
        return method()
    except XiaomiProviderError as exc:
        return {"status": "FAIL", "provider": SOURCE, "error_code": exc.code if exc.code in SAFE_ERRORS else "unavailable"}
    except Exception:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}

def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        operation = payload.get("operation") if isinstance(payload, dict) else None
        metric = payload.get("metric", "all") if isinstance(payload, dict) else "all"
        days = payload.get("days", 7) if isinstance(payload, dict) else 7
        result = run(operation, metric=metric, days=days)
    except Exception:
        result = {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

