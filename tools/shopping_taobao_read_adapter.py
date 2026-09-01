"""Read-only Taobao/Tmall adapter backed only by the persistent shop daemon.

This module deliberately has no Playwright/browser fallback.  The single
persistent browser is the source of truth for logged-in shopping reads; when
it is unavailable the failure stays visible instead of spawning another
Chromium process.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from urllib.parse import urlparse

DEFAULT_SHOP_DAEMON_URL = "http://127.0.0.1:8787"
_ALLOWED_HOST_SUFFIXES = ("taobao.com", "tmall.com")


def _allowed_url(raw_url: object) -> str:
    url = str(raw_url or "").strip()
    if not url:
        raise ValueError("url is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        raise ValueError("only taobao.com/tmall.com pages are allowed")
    return url


def read_taobao_page(url: object, *, timeout: int = 60) -> dict[str, object]:
    target = _allowed_url(url)
    base = str(os.environ.get("SHOP_DAEMON_URL") or DEFAULT_SHOP_DAEMON_URL).strip().rstrip("/")
    payload = json.dumps({"site": "taobao", "url": target}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + "/browse",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(5, min(int(timeout), 120))) as response:
            result = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            detail = ""
        raise RuntimeError(f"shop daemon HTTP {exc.code}: {detail}".rstrip()) from exc
    except Exception as exc:
        raise RuntimeError(f"shop daemon unavailable: {exc}") from exc

    if not isinstance(result, Mapping):
        raise RuntimeError("shop daemon returned a non-object response")
    if result.get("ok") is not True:
        raise RuntimeError("shop daemon browse failed: " + str(result.get("error") or "unknown")[:200])

    text = str(result.get("text") or "")
    return {
        "status": "OK",
        "url": str(result.get("finalUrl") or result.get("url") or target),
        "text": text[:8000],
        "need_login": bool(result.get("need_login")),
    }


def _main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    if not isinstance(request, Mapping) or request.get("operation") != "read_taobao_page":
        raise ValueError("unsupported operation")
    result = read_taobao_page(request.get("url"), timeout=request.get("timeout") or 60)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
