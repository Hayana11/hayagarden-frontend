"""Read-only Taobao/Tmall adapter backed only by the persistent shop daemon.

This module deliberately has no Playwright/browser fallback.  The single
persistent browser is the source of truth for logged-in shopping reads; when
it is unavailable the failure stays visible instead of spawning another
Chromium process.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from urllib.parse import urlparse

DEFAULT_SHOP_DAEMON_URL = "http://127.0.0.1:8787"
DAEMON_RESPONSE_MAX_BYTES = 512 * 1024
_ALLOWED_HOST_SUFFIXES = ("taobao.com", "tmall.com")


def _allowed_url(raw_url: object) -> str:
    url = str(raw_url or "").strip()
    if not url:
        raise ValueError("url is required")
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError as exc:
        raise ValueError("invalid URL") from exc
    if parsed.scheme.lower() != "https":
        raise ValueError("only HTTPS URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP/localhost URLs are not allowed")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_HOST_SUFFIXES):
        raise ValueError("only taobao.com/tmall.com pages are allowed")
    return url


def _read_bounded_response(response: object, *, limit: int = DAEMON_RESPONSE_MAX_BYTES) -> bytes:
    headers = getattr(response, "headers", None)
    raw_length = headers.get("Content-Length") if headers is not None else None
    if raw_length is not None:
        try:
            content_length = int(str(raw_length).strip())
        except (TypeError, ValueError) as exc:
            raise RuntimeError("daemon response has invalid Content-Length") from exc
        if content_length < 0:
            raise RuntimeError("daemon response has invalid Content-Length")
        if content_length > limit:
            raise RuntimeError(f"daemon response exceeds {limit} bytes")
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RuntimeError(f"daemon response exceeds {limit} bytes")
    return body


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
            body = _read_bounded_response(response)
            result = json.loads(body.decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = _read_bounded_response(exc).decode("utf-8", "replace")[:200]
        except Exception as detail_error:
            detail = str(detail_error)
        raise RuntimeError(f"shop daemon HTTP {exc.code}: {detail}".rstrip()) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("shop daemon returned invalid JSON") from exc
    except Exception as exc:
        raise RuntimeError(f"shop daemon unavailable: {exc}") from exc

    if not isinstance(result, Mapping):
        raise RuntimeError("shop daemon returned a non-object response")
    if result.get("ok") is not True:
        raise RuntimeError("shop daemon browse failed: " + str(result.get("error") or "unknown")[:200])

    resolved_url = result.get("finalUrl") or result.get("url") or target
    resolved_url = _allowed_url(resolved_url)
    text = str(result.get("text") or "")
    return {
        "status": "OK",
        "url": resolved_url,
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
