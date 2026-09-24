"""Private JSON adapter used only by Internal MCP's Xiaomi capabilities."""
from __future__ import annotations

import json
import sys
from typing import Any

from .client import XiaomiHealthClient, XiaomiProviderError
from .store import DEFAULT_CREDENTIAL_PATH, SOURCE, XiaomiCredentialStore


OPERATIONS = {
    "health_status": "status",
    "health_latest": "latest",
    "health_steps": "steps",
    "health_sleep": "sleep",
    "health_heart_rate": "heart_rate",
}
SAFE_ERRORS = frozenset({"auth_expired", "timeout", "api_error", "malformed_response", "unavailable"})


def run(operation: str, *, days: int | None = None, store: XiaomiCredentialStore | None = None, client: XiaomiHealthClient | None = None) -> dict[str, Any]:
    if operation not in OPERATIONS:
        return {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    store = store or XiaomiCredentialStore(DEFAULT_CREDENTIAL_PATH)
    client = client or XiaomiHealthClient(store)
    if operation == "health_status":
        return store.status()
    if operation == "health_latest":
        method = client.get_latest
    else:
        metric = OPERATIONS[operation]
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 30:
            return {"status": "FAIL", "provider": SOURCE, "metric": metric, "days": days, "records": [], "error_code": "malformed_response"}
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
        days = payload.get("days") if isinstance(payload, dict) else None
        result = run(operation, days=days)
    except Exception:
        result = {"status": "FAIL", "provider": SOURCE, "error_code": "unavailable"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

