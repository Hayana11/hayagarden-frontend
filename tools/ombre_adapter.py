"""Stable boundary between Haya Garden and Ombre Brain.

The frontend used to import Ombre Brain's ``server`` module from several hot
paths.  This adapter keeps those implementation details in one place and gives
us a migration seam for Ombre Brain 2.x without changing callers again.

No function in this module mutates frontend SQLite.  Automatic handoff and the
safe emotion snapshot are intentionally read-only and never call ``touch``.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import importlib
import json
import logging
import os
import sys
import threading
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
import time
from typing import Any, Callable, Optional

_LOG = logging.getLogger("hayagarden.ombre_adapter")
_DEFAULT_ROOT = "/opt/ombre-brain"
_DEFAULT_SNAPSHOT_URL = "http://127.0.0.1:8000/emotion_snapshot"
_SERVER_INSTANCE = None
_SERVER_ERROR: Optional[BaseException] = None
_SERVER_LOCK = threading.Lock()
_SERVER_READY = threading.Event()
_WARMUP_STARTED = False
_HTTP_COOKIE_JAR = http.cookiejar.CookieJar()
_HTTP_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_HTTP_COOKIE_JAR))
_HTTP_LOGIN_LOCK = threading.Lock()
_HTTP_LOGGED_IN = False


def _backend() -> str:
    return os.environ.get("OMBRE_ADAPTER_BACKEND", "legacy_module").strip().lower()


def _http_base() -> str:
    return os.environ.get("OMBRE_HTTP_BASE_URL", "http://127.0.0.1:18001").rstrip("/")


def _http_headers() -> dict[str, str]:
    token = os.environ.get("OMBRE_MCP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _http_login(timeout: float) -> bool:
    global _HTTP_LOGGED_IN
    password = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "").strip()
    if not password:
        return False
    with _HTTP_LOGIN_LOCK:
        if _HTTP_LOGGED_IN:
            return True
        body = json.dumps({"password": password}).encode("utf-8")
        request = urllib.request.Request(
            _http_base() + "/auth/login",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        _HTTP_LOGGED_IN = bool(payload.get("ok"))
        return _HTTP_LOGGED_IN


def _http_json(path: str, *, params: Optional[dict[str, Any]] = None, timeout: float = 5.0) -> Any:
    global _HTTP_LOGGED_IN
    url = _http_base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=_http_headers())
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code != 401 or path.startswith("/auth/"):
            raise
        # A sidecar restart invalidates the old session cookie.  Force one
        # fresh login instead of trusting the process-local cached flag.
        _HTTP_LOGGED_IN = False
        if not _http_login(timeout):
            raise
        retry = urllib.request.Request(url, headers=_http_headers())
        with _HTTP_OPENER.open(retry, timeout=timeout) as response:
            return json.loads(response.read())


async def _mcp_call(tool_name: str, arguments: dict[str, Any], *, timeout: float) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = os.environ.get("OMBRE_MCP_URL", _http_base() + "/mcp")
    async with streamablehttp_client(
        url,
        headers=_http_headers(),
        timeout=timeout,
        sse_read_timeout=timeout,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments=arguments)
    chunks = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            chunks.append(str(text))
    return "\n".join(chunks)


def _load_server():
    """Load the configured Ombre server lazily.

    Kept as a small function so tests and future HTTP/MCP backends can replace
    it without importing a production vault.
    """
    global _SERVER_INSTANCE, _SERVER_ERROR
    if _SERVER_INSTANCE is not None:
        return _SERVER_INSTANCE
    with _SERVER_LOCK:
        if _SERVER_INSTANCE is not None:
            return _SERVER_INSTANCE
        try:
            root = os.environ.get("OMBRE_BRAIN_ROOT", _DEFAULT_ROOT).strip() or _DEFAULT_ROOT
            if root not in sys.path:
                sys.path.insert(0, root)
            logging.getLogger("ombre_brain").setLevel(logging.WARNING)
            _SERVER_INSTANCE = importlib.import_module("server")
            _SERVER_ERROR = None
            return _SERVER_INSTANCE
        except BaseException as exc:
            _SERVER_ERROR = exc
            raise
        finally:
            _SERVER_READY.set()


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    except Exception:
        pass
    finally:
        loop.close()


def _run_async(
    coroutine_factory: Callable[[], Any],
    *,
    async_timeout: float,
    wall_timeout: float,
    default: Any,
) -> Any:
    """Run one coroutine in a daemon thread with a real wall-clock timeout."""
    result = [default]
    done = threading.Event()

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result[0] = loop.run_until_complete(
                asyncio.wait_for(coroutine_factory(), timeout=async_timeout)
            )
        except Exception:
            result[0] = default
        finally:
            _close_loop(loop)
            done.set()

    threading.Thread(target=worker, daemon=True, name="ombre-adapter").start()
    done.wait(timeout=max(0.05, wall_timeout))
    return result[0]


def warmup_async() -> None:
    """Warm the selected backend without blocking gateway import."""
    global _WARMUP_STARTED
    if _WARMUP_STARTED:
        return
    _WARMUP_STARTED = True

    def worker() -> None:
        try:
            if _backend() == "http":
                _http_json("/health", timeout=3.0)
                _SERVER_READY.set()
            else:
                _load_server()
            _LOG.info("Ombre backend warmup complete")
        except Exception as exc:
            _LOG.warning("Ombre warmup failed: %s", exc)
            _SERVER_READY.set()

    threading.Thread(target=worker, daemon=True, name="ombre-warmup").start()


def wait_until_ready(timeout: float = 20.0) -> bool:
    """Start warmup if needed and wait without touching any memory bucket."""
    if _backend() == "http":
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() <= deadline:
            try:
                payload = _http_json("/health", timeout=min(2.0, max(0.2, deadline - time.monotonic())))
                return isinstance(payload, dict) and payload.get("status") == "ok"
            except Exception:
                time.sleep(0.1)
        return False
    warmup_async()
    _SERVER_READY.wait(timeout=max(0.0, float(timeout)))
    return _SERVER_INSTANCE is not None


def _metadata_domains(meta: dict) -> list[str]:
    value = meta.get("domain", [])
    if isinstance(value, str):
        return [value]
    return [str(item) for item in (value or [])]


def _timestamp(meta: dict) -> float:
    raw = meta.get("last_active") or meta.get("created") or ""
    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _clip(text: Any, limit: int = 420) -> str:
    compact = " ".join(str(text or "").split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


async def _safe_handoff_async(server: Any) -> str:
    """Build handoff from buckets without LLM calls, permanent leakage or touch."""
    manager = getattr(server, "bucket_mgr", None)
    if manager is None:
        return ""
    buckets = await manager.list_all(include_archive=False)
    sections: dict[str, list[dict]] = {
        "self_anchor": [],
        "user_portrait": [],
        "relationship": [],
    }
    recent: list[dict] = []

    for bucket in buckets or []:
        meta = bucket.get("metadata", {}) or {}
        if meta.get("resolved") or meta.get("deleted_at") or meta.get("digested"):
            continue
        domains = _metadata_domains(meta)
        matched = False
        for key in sections:
            if key in domains:
                sections[key].append(bucket)
                matched = True
        if matched:
            continue
        # Recent continuity must be ordinary dynamic memory only.  Permanent,
        # pinned and protected records are identity/reference material, not news.
        if (
            meta.get("type") == "dynamic"
            and not meta.get("pinned")
            and not meta.get("protected")
            and not meta.get("dont_surface")
        ):
            recent.append(bucket)

    labels = (
        ("self_anchor", "自我"),
        ("user_portrait", "你"),
        ("relationship", "我们"),
    )
    parts: list[str] = []
    for key, label in labels:
        candidates = sorted(
            sections[key],
            key=lambda item: (_timestamp(item.get("metadata", {}) or {}), str(item.get("id", ""))),
            reverse=True,
        )
        if candidates:
            bucket = candidates[0]
            parts.append(f"[id:{bucket.get('id', '')}][{label}] {_clip(bucket.get('content'))}")

    recent.sort(
        key=lambda item: (
            _timestamp(item.get("metadata", {}) or {}),
            int((item.get("metadata", {}) or {}).get("importance") or 0),
            str(item.get("id", "")),
        ),
        reverse=True,
    )
    for bucket in recent[:2]:
        meta = bucket.get("metadata", {}) or {}
        parts.append(
            f"[id:{bucket.get('id', '')}][近期·I{int(meta.get('importance') or 0)}] "
            f"{_clip(bucket.get('content'))}"
        )
    return "\n---\n".join(parts)


def _http_handoff(timeout: float) -> str:
    buckets = _http_json("/api/buckets", params={"sort": "created_desc"}, timeout=timeout)
    sections = {"self_anchor": [], "user_portrait": [], "relationship": []}
    recent = []
    for meta in buckets if isinstance(buckets, list) else []:
        if meta.get("resolved") or meta.get("digested") or meta.get("dont_surface"):
            continue
        domains = meta.get("domain") or []
        if isinstance(domains, str):
            domains = [domains]
        matched = False
        for key in sections:
            if key in domains:
                sections[key].append(meta)
                matched = True
        if not matched and meta.get("type") == "dynamic" and not meta.get("pinned"):
            recent.append(meta)

    def detail(item: dict) -> str:
        payload = _http_json(f"/api/bucket/{item['id']}", timeout=timeout)
        return _clip(payload.get("content", "")) if isinstance(payload, dict) else ""

    parts = []
    for key, label in (("self_anchor", "自我"), ("user_portrait", "你"), ("relationship", "我们")):
        if sections[key]:
            item = sections[key][0]
            parts.append(f"[id:{item['id']}][{label}] {detail(item)}")
    recent.sort(key=lambda item: (item.get("last_active_epoch_ms") or 0, item.get("importance") or 0), reverse=True)
    for item in recent[:2]:
        parts.append(f"[id:{item['id']}][近期·I{int(item.get('importance') or 0)}] {detail(item)}")
    return "\n---\n".join(parts)


def get_handoff(*, timeout: float = 3.0, wall_timeout: float = 5.0) -> Optional[str]:
    """Return compact opening-window continuity.

    ``OMBRE_HANDOFF_MODE=safe`` uses the adapter's read-only builder.  The
    default ``legacy`` mode preserves production behaviour until the migration
    flag is deliberately changed; if a new Ombre version has no handoff tool,
    it automatically falls back to the safe builder.
    """
    if _backend() == "http":
        try:
            return _http_handoff(timeout=max(timeout, wall_timeout))
        except Exception:
            return None

    async def call() -> str:
        server = _load_server()
        mode = os.environ.get("OMBRE_HANDOFF_MODE", "legacy").strip().lower()
        legacy = getattr(server, "handoff", None)
        if mode != "safe" and callable(legacy):
            return await legacy()
        return await _safe_handoff_async(server)

    return _run_async(call, async_timeout=timeout, wall_timeout=wall_timeout, default=None)


def search_memories(
    query: str,
    *,
    limit: int = 2,
    timeout: float = 4.0,
    wall_timeout: Optional[float] = None,
    touch: bool = True,
) -> list[tuple[str, str]]:
    """Search Ombre directly; explicit search may opt into recall touch."""
    query = str(query or "").strip()
    if not query:
        return []
    if _backend() == "http":
        try:
            matches = _http_json("/api/search", params={"q": query}, timeout=timeout)
            output = []
            for item in (matches or [])[: max(1, int(limit))]:
                detail = _http_json(f"/api/bucket/{item['id']}", timeout=timeout)
                content = str(detail.get("content") or item.get("content_preview") or "").strip()
                if content:
                    output.append((str(item.get("name") or "记忆桶"), content[:300]))
            return output
        except Exception:
            return []

    async def call() -> list[tuple[str, str]]:
        server = _load_server()
        manager = getattr(server, "bucket_mgr")
        matches = await manager.search(query, limit=max(1, int(limit)))
        output: list[tuple[str, str]] = []
        for bucket in matches or []:
            meta = bucket.get("metadata", {}) or {}
            content = str(bucket.get("content") or "").strip()
            if not content:
                continue
            output.append((str(meta.get("name") or "记忆桶"), content[:300]))
            if touch:
                try:
                    await manager.touch(bucket.get("id"))
                except Exception:
                    pass
        return output

    return _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=wall_timeout or timeout + 1.0,
        default=[],
    )


def surface_memories(*, timeout: float = 6.0, wall_timeout: float = 7.0) -> Optional[str]:
    is_http = _backend() == "http"
    async_timeout = max(timeout, 30.0) if is_http else timeout
    effective_wall = max(wall_timeout, async_timeout + 3.0) if is_http else wall_timeout

    async def call() -> str:
        if is_http:
            return await _mcp_call("breath", {}, timeout=async_timeout)
        server = _load_server()
        return await getattr(server, "breath")()

    return _run_async(
        call,
        async_timeout=async_timeout,
        wall_timeout=effective_wall,
        default=None,
    )


async def hold_memory_async(
    content: str,
    *,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
) -> Optional[str]:
    """Async write primitive shared by request paths and offline cleaners."""
    content = str(content or "").strip()
    if not content:
        return None
    arguments = {
        "content": content,
        "tags": str(tags or ""),
        "importance": max(1, min(10, int(importance))),
        "pinned": bool(pinned),
    }
    if _backend() == "http":
        return await _mcp_call("hold", arguments, timeout=30.0)
    server = _load_server()
    return await getattr(server, "hold")(**arguments)


def hold_memory(
    content: str,
    *,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    timeout: float = 3.0,
    wall_timeout: float = 4.0,
) -> Optional[str]:
    content = str(content or "").strip()
    if not content:
        return None

    async def call() -> Optional[str]:
        return await hold_memory_async(
            content,
            tags=tags,
            importance=importance,
            pinned=pinned,
        )

    is_http = _backend() == "http"
    async_timeout = max(timeout, 35.0) if is_http else timeout
    effective_wall = max(wall_timeout, async_timeout + 3.0) if is_http else wall_timeout
    return _run_async(
        call,
        async_timeout=async_timeout,
        wall_timeout=effective_wall,
        default=None,
    )


async def _safe_emotion_snapshot_async(server: Any, limit: int = 12) -> dict[str, Any]:
    manager = getattr(server, "bucket_mgr", None)
    if manager is None:
        return {"valence": None, "arousal": None, "count": 0}
    buckets = await manager.list_all(include_archive=False)
    candidates: list[dict] = []
    for bucket in buckets or []:
        meta = bucket.get("metadata", {}) or {}
        if (
            meta.get("type") != "dynamic"
            or meta.get("resolved")
            or meta.get("deleted_at")
            or meta.get("digested")
            or meta.get("dont_surface")
            or meta.get("pinned")
            or meta.get("protected")
        ):
            continue
        provenance = meta.get("provenance") or {}
        if isinstance(provenance, dict) and provenance.get("kind") == "test":
            continue
        candidates.append(bucket)
    candidates.sort(
        key=lambda item: (
            _timestamp(item.get("metadata", {}) or {}),
            int((item.get("metadata", {}) or {}).get("importance") or 0),
        ),
        reverse=True,
    )
    chosen = candidates[: max(1, int(limit))]
    if not chosen:
        return {"valence": None, "arousal": None, "count": 0}
    values: list[tuple[float, float]] = []
    for bucket in chosen:
        meta = bucket.get("metadata", {}) or {}
        try:
            values.append((float(meta.get("valence", 0.5)), float(meta.get("arousal", 0.3))))
        except (TypeError, ValueError):
            continue
    if not values:
        return {"valence": None, "arousal": None, "count": 0}
    return {
        "valence": round(sum(item[0] for item in values) / len(values), 3),
        "arousal": round(sum(item[1] for item in values) / len(values), 3),
        "count": len(values),
    }


def get_emotion_snapshot(*, timeout: float = 3.0) -> dict[str, Any]:
    """Read emotion calibration data.

    Default ``http`` mode preserves the deployed endpoint.  ``safe`` computes a
    read-only dynamic-memory sample and excludes permanent/pinned/test records.
    """
    mode = os.environ.get("OMBRE_EMOTION_MODE", "http").strip().lower()
    if _backend() == "http":
        try:
            buckets = _http_json("/api/buckets", params={"sort": "created_desc"}, timeout=timeout)
            candidates = []
            for item in buckets if isinstance(buckets, list) else []:
                if (
                    item.get("type") != "dynamic" or item.get("resolved") or item.get("digested")
                    or item.get("dont_surface") or item.get("pinned") or item.get("erasable_test_data")
                ):
                    continue
                candidates.append(item)
            chosen = candidates[:12]
            if not chosen:
                return {"valence": None, "arousal": None, "count": 0}
            return {
                "valence": round(sum(float(x.get("valence", 0.5)) for x in chosen) / len(chosen), 3),
                "arousal": round(sum(float(x.get("arousal", 0.3)) for x in chosen) / len(chosen), 3),
                "count": len(chosen),
            }
        except Exception:
            return {"valence": None, "arousal": None, "count": 0}
    if mode != "safe":
        url = os.environ.get("OMBRE_EMOTION_SNAPSHOT_URL", _DEFAULT_SNAPSHOT_URL)
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            valence = payload.get("valence")
            arousal = payload.get("arousal")
            if valence is not None and arousal is not None:
                return {
                    "valence": float(valence),
                    "arousal": float(arousal),
                    "count": int(payload.get("count") or 0),
                }
        except Exception:
            return {"valence": None, "arousal": None, "count": 0}

    async def call() -> dict[str, Any]:
        return await _safe_emotion_snapshot_async(_load_server())

    return _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=timeout + 1.0,
        default={"valence": None, "arousal": None, "count": 0},
    )
