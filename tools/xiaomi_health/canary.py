"""One-shot, no-listener Xiaomi account canary. Always deletes its credentials."""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Any

from .auth_flow import authenticate
from .client import XiaomiHealthClient, XiaomiProviderError
from .parser import latest_date
from .store import CANARY_CREDENTIAL_PATH, XiaomiCredentialStore


QR_PATH = "/run/hayagarden/xiaomi-health-canary.svg"
MAX_LOGIN_SECONDS = 300


def _run_canary(*, timeout_seconds: int = MAX_LOGIN_SECONDS) -> tuple[int, dict[str, Any]]:
    store = XiaomiCredentialStore(CANARY_CREDENTIAL_PATH)
    client = XiaomiHealthClient(store)
    result: dict[str, Any] = {
        "auth": "FAIL",
        "health_status": "FAIL",
        "latest": "FAIL",
        "steps": {"status": "FAIL", "rows": 0, "latest_date": None},
        "sleep": {"status": "FAIL", "rows": 0, "latest_date": None},
        "heart_rate": {"status": "FAIL", "rows": 0, "latest_date": None},
    }
    owns_artifacts = False
    try:
        if os.geteuid() != 0:
            raise XiaomiProviderError("unavailable")
        Path(CANARY_CREDENTIAL_PATH).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(os.path.lexists(path) for path in (CANARY_CREDENTIAL_PATH, CANARY_CREDENTIAL_PATH + ".lock", QR_PATH)):
            raise XiaomiProviderError("unavailable")
        owns_artifacts = True
        if authenticate(client, store, qr_path=QR_PATH, timeout_seconds=timeout_seconds, on_qr_ready=lambda: print("CANARY_QR_READY", flush=True)):
            result["auth"] = "PASS"
        if result["auth"] == "PASS":
            credential_stat = os.stat(CANARY_CREDENTIAL_PATH, follow_symlinks=False)
            if credential_stat.st_uid != 0 or credential_stat.st_gid != 0 or stat.S_IMODE(credential_stat.st_mode) != 0o600:
                raise XiaomiProviderError("unavailable")
        if result["auth"] != "PASS":
            return 1, result

        result["health_status"] = "PASS" if client.health_status().get("connected") is True else "FAIL"
        try:
            latest = client.get_latest()
            result["latest"] = "PASS" if any(latest.get(key) is not None for key in ("steps", "sleep", "heart_rate")) else "EMPTY"
        except XiaomiProviderError:
            result["latest"] = "FAIL"
        for metric in ("steps", "sleep", "heart_rate"):
            try:
                series = client.get_series(metric, 2)
                records = series["records"]
                result[metric] = {
                    "status": "PASS" if records else "EMPTY",
                    "rows": len(records),
                    "latest_date": latest_date(records),
                }
            except XiaomiProviderError:
                result[metric] = {"status": "FAIL", "rows": 0, "latest_date": None}
        exit_code = 0 if result["health_status"] == "PASS" and result["latest"] in {"PASS", "EMPTY"} and all(result[m]["status"] in {"PASS", "EMPTY"} for m in ("steps", "sleep", "heart_rate")) else 1
        return exit_code, result
    except Exception:
        return 1, result
    finally:
        if owns_artifacts:
            try:
                store.delete()
            except Exception:
                pass
            for path in (QR_PATH,):
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot Xiaomi Health Cloud canary")
    parser.add_argument("--timeout-seconds", type=int, default=MAX_LOGIN_SECONDS)
    args = parser.parse_args(argv)
    code, result = _run_canary(timeout_seconds=args.timeout_seconds)
    print(f"CANARY_AUTH={result['auth']}")
    print(f"CANARY_HEALTH_STATUS={result['health_status']}")
    print(f"CANARY_LATEST={result['latest']}")
    for metric in ("steps", "sleep", "heart_rate"):
        item = result[metric]
        print(f"CANARY_{metric.upper()}={item['status']} ROWS={item['rows']} LATEST_DATE={item['latest_date'] or 'null'}")
    cleaned = not os.path.exists(CANARY_CREDENTIAL_PATH) and not os.path.exists(QR_PATH)
    print(f"CANARY_CREDENTIAL_CLEANED={'YES' if cleaned else 'NO'}")
    return code if cleaned else 1


if __name__ == "__main__":
    raise SystemExit(main())
