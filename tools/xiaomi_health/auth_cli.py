"""Create or replace Xiaomi Health credentials through a one-shot QR login."""
from __future__ import annotations

import argparse
import os

from .auth_flow import authenticate
from .client import XiaomiHealthClient
from .store import DEFAULT_CREDENTIAL_PATH, XiaomiCredentialStore


QR_PATH = "/run/hayagarden/xiaomi-health-login.svg"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot Xiaomi Health QR login")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        print("XIAOMI_HEALTH_LOGIN=FAIL reason=root_required")
        return 1
    store = XiaomiCredentialStore(DEFAULT_CREDENTIAL_PATH)
    client = XiaomiHealthClient(store)
    try:
        def ready() -> None:
            print("XIAOMI_HEALTH_QR_READY", flush=True)
        if os.path.lexists(QR_PATH):
            print("XIAOMI_HEALTH_LOGIN=FAIL reason=qr_path_busy")
            return 1
        success = authenticate(client, store, qr_path=QR_PATH, timeout_seconds=args.timeout_seconds, on_qr_ready=ready)
        print(f"XIAOMI_HEALTH_LOGIN={'PASS' if success else 'FAIL'}")
        if success:
            status = store.status()
            print(f"XIAOMI_HEALTH_AUTH_STATE={status['auth_state']}")
        return 0 if success else 1
    except Exception:
        print("XIAOMI_HEALTH_LOGIN=FAIL reason=provider_error")
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
