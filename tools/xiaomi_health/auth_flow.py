"""QR login flow shared by the operator CLI and isolated canary."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

from .client import XiaomiHealthClient, XiaomiProviderError
from .qr import render_qr_svg
from .store import XiaomiCredentialStore


def write_qr_svg(path: str, login_url: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(fd, 0, 0)
        content = render_qr_svg(login_url).encode("utf-8")
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def authenticate(
    client: XiaomiHealthClient,
    store: XiaomiCredentialStore,
    *,
    qr_path: str,
    timeout_seconds: int = 300,
    on_qr_ready: Callable[[], None] | None = None,
) -> bool:
    Path(qr_path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = client.start_qr_login()
    write_qr_svg(qr_path, session["login_url"])
    try:
        if on_qr_ready:
            on_qr_ready()
        deadline = min(float(session["expires_at"]), time.time() + max(1, min(timeout_seconds, 300)))
        while time.time() < deadline:
            try:
                state = client.poll_qr_login(session)
            except XiaomiProviderError as exc:
                if exc.code == "timeout":
                    continue
                raise
            if state == "success":
                return True
            if state == "expired":
                return False
        return False
    finally:
        try:
            os.unlink(qr_path)
        except FileNotFoundError:
            pass

