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

# HTTP backend is opt-in.  Its dashboard REST and MCP authentication
# boundaries are documented in docs/unified-memory-adapter.md.
# - explicit search intentionally does not touch hits
# - MCP calls require the disposable/runtime mcp dependency


def _backend() -> str:
    return os.environ.get("OMBRE_ADAPTER_BACKEND", "legacy_module").strip().lower()


def _observe_read(
    observer_name: str,
    authoritative_result: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Best-effort R4A observation after an authoritative legacy read."""
    if _backend() != "legacy_module":
        return authoritative_result
    try:
        observer_module = importlib.import_module("tools.ombre_read_shadow")
        observer = getattr(observer_module, observer_name)
        observer(authoritative_result, *args, **kwargs)
    except BaseException:
        return authoritative_result
    return authoritative_result


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


def _http_json(
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    timeout: float = 5.0,
    deadline: Optional[float] = None,
) -> Any:
    global _HTTP_LOGGED_IN
    if deadline is not None:
        timeout = min(timeout, max(0.05, deadline - time.monotonic()))
    if timeout <= 0:
        raise TimeoutError("http deadline exceeded")
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
        if deadline is not None:
            timeout = min(timeout, max(0.05, deadline - time.monotonic()))
        if timeout <= 0:
            raise TimeoutError("http deadline exceeded")
        retry = urllib.request.Request(url, headers=_http_headers())
        with _HTTP_OPENER.open(retry, timeout=timeout) as response:
            return json.loads(response.read())


async def _mcp_call(tool_name: str, arguments: dict[str, Any], *, timeout: float) -> str:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise RuntimeError(
            "HTTP backend requires the optional `mcp` package; "
            "install it only in disposable shadow environments"
        ) from exc

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


def _run_sync(
    worker: Callable[[], Any],
    *,
    wall_timeout: float,
    default: Any,
) -> Any:
    """Run one synchronous worker in a daemon thread with a wall-clock timeout."""
    result = [default]
    done = threading.Event()

    def target() -> None:
        try:
            result[0] = worker()
        except Exception:
            result[0] = default
        finally:
            done.set()

    threading.Thread(target=target, daemon=True, name="ombre-adapter-sync").start()
    done.wait(timeout=max(0.05, wall_timeout))
    return result[0]


def _warmup_jieba_only() -> None:
    """Match legacy gateway warmup: tokenizer only, no server/vault import."""
    root = os.environ.get("OMBRE_BRAIN_ROOT", _DEFAULT_ROOT).strip() or _DEFAULT_ROOT
    if root not in sys.path:
        sys.path.insert(0, root)
    import jieba

    jieba.initialize()


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
            else:
                _warmup_jieba_only()
            _SERVER_READY.set()
            _LOG.info("Ombre backend warmup complete")
        except Exception as exc:
            _LOG.warning("Ombre warmup failed: %s", exc)
            _SERVER_READY.set()

    threading.Thread(target=worker, daemon=True, name="ombre-warmup").start()


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
_NORMALIZED_RECORD_FIELDS = (
    "id",
    "name",
    "type",
    "domain",
    "tags",
    "valence",
    "arousal",
    "importance",
    "created",
    "last_active",
    "content",
)


def _record_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """Return one metadata view for legacy buckets and HTTP summaries/details."""
    nested = item.get("metadata")
    meta = dict(nested) if isinstance(nested, dict) else {}
    for key in (
        "id", "name", "type", "domain", "tags", "valence", "arousal",
        "importance", "created", "last_active", "deleted_at", "tombstone",
        "resolved", "digested", "dont_surface", "pinned", "protected",
        "provenance",
    ):
        if key not in meta and key in item:
            meta[key] = item[key]
    return meta


def _is_terminal_record(item: dict[str, Any]) -> bool:
    meta = _record_metadata(item)
    return (
        str(meta.get("type") or "").strip().lower() == "archived"
        or bool(meta.get("deleted_at"))
        or bool(meta.get("tombstone"))
    )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in (value or [])]


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalized_record(item: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
    meta = _record_metadata(item)
    record = {
        "id": str(item.get("id") or meta.get("id") or ""),
        "name": str(meta.get("name") or item.get("name") or item.get("id") or ""),
        "type": str(meta.get("type") or item.get("type") or ""),
        "domain": _as_list(meta.get("domain")),
        "tags": _as_list(meta.get("tags")),
        "valence": _number(meta.get("valence"), 0.5),
        "arousal": _number(meta.get("arousal"), 0.3),
        "importance": _number(meta.get("importance"), 0.0),
        "created": str(meta.get("created") or ""),
        "last_active": str(meta.get("last_active") or ""),
        "content": str(item.get("content") or "") if include_content else "",
    }
    return {key: record[key] for key in _NORMALIZED_RECORD_FIELDS}


def _record_matches(
    record: dict[str, Any],
    *,
    bucket_type: Optional[str],
    domain: Optional[str],
    min_arousal: Optional[float],
) -> bool:
    if bucket_type and record["type"] != bucket_type:
        return False
    if domain and domain not in record["domain"]:
        return False
    if min_arousal is not None and record["arousal"] < float(min_arousal):
        return False
    return True


def _record_sort_key(record: dict[str, Any]) -> tuple[float, float, str]:
    return (
        _timestamp({"last_active": record.get("last_active")}),
        _timestamp({"created": record.get("created")}),
        str(record.get("id") or ""),
    )


def _sort_records(records: list[dict[str, Any]], sort: str) -> None:
    descending = str(sort or "last_active_desc").endswith("_desc")
    records.sort(key=lambda record: str(record.get("id") or ""))
    records.sort(
        key=lambda record: _timestamp({"created": record.get("created")}),
        reverse=descending,
    )
    records.sort(
        key=lambda record: _timestamp({"last_active": record.get("last_active")}),
        reverse=descending,
    )


def list_memory_records(
    *,
    bucket_type: Optional[str] = None,
    domain: Optional[str] = None,
    min_arousal: Optional[float] = None,
    include_content: bool = False,
    limit: Optional[int] = None,
    sort: str = "last_active_desc",
    timeout: float = 8.0,
    wall_timeout: Optional[float] = None,
) -> list[dict[str, Any]]:
    """List active Ombre records through the selected backend only."""
    requested_type = str(bucket_type or "").strip()

    def select(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records = []
        for item in raw_items:
            if not isinstance(item, dict) or _is_terminal_record(item):
                continue
            record = _normalized_record(item, include_content=include_content)
            if not record["id"]:
                continue
            if _record_matches(
                record,
                bucket_type=requested_type or None,
                domain=domain,
                min_arousal=min_arousal,
            ):
                records.append(record)
        _sort_records(records, sort)
        if limit is not None:
            return records[: max(0, int(limit))]
        return records

    if _backend() == "http":
        def worker() -> list[dict[str, Any]]:
            deadline = time.monotonic() + max(0.05, float(wall_timeout or timeout + 2.0))
            summaries = _http_json(
                "/api/buckets",
                params={"sort": "created_desc"},
                timeout=min(timeout, max(0.05, deadline - time.monotonic())),
                deadline=deadline,
            )
            candidates = select(list(summaries or []) if isinstance(summaries, list) else [])
            if not include_content:
                return candidates
            enriched: list[dict[str, Any]] = []
            for candidate in candidates:
                detail = _http_json(
                    "/api/bucket/" + urllib.parse.quote(candidate["id"], safe=""),
                    timeout=min(timeout, max(0.05, deadline - time.monotonic())),
                    deadline=deadline,
                )
                if not isinstance(detail, dict) or _is_terminal_record(detail):
                    continue
                enriched_item = dict(detail)
                enriched_item.setdefault("id", candidate["id"])
                record = _normalized_record(enriched_item, include_content=True)
                if _record_matches(
                    record,
                    bucket_type=requested_type or None,
                    domain=domain,
                    min_arousal=min_arousal,
                ):
                    enriched.append(record)
            _sort_records(enriched, sort)
            return enriched[: max(0, int(limit))] if limit is not None else enriched

        result = _run_sync(
            worker,
            wall_timeout=wall_timeout or timeout + 2.0,
            default=[],
        )
        return _observe_read(
            "observe_records",
            result,
            bucket_type=bucket_type,
            domain=domain,
            min_arousal=min_arousal,
            include_content=include_content,
            limit=limit,
            sort=sort,
        )

    async def call() -> list[dict[str, Any]]:
        server = _load_server()
        manager = getattr(server, "bucket_mgr", None)
        if manager is None:
            return []
        buckets = await manager.list_all(include_archive=False)
        return select(list(buckets or []))

    result = _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=wall_timeout or timeout + 1.0,
        default=[],
    )
    return _observe_read(
        "observe_records",
        result,
        bucket_type=bucket_type,
        domain=domain,
        min_arousal=min_arousal,
        include_content=include_content,
        limit=limit,
        sort=sort,
    )


def get_memory_record(
    bucket_id: str,
    *,
    timeout: float = 8.0,
    wall_timeout: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Read one active record by Ombre ID; path-shaped input is not accepted."""
    normalized_id = str(bucket_id or "").strip()
    if not normalized_id or "/" in normalized_id or chr(92) in normalized_id:
        return None

    if _backend() == "http":
        def worker() -> Optional[dict[str, Any]]:
            payload = _http_json(
                "/api/bucket/" + urllib.parse.quote(normalized_id, safe=""),
                timeout=timeout,
            )
            if not isinstance(payload, dict) or _is_terminal_record(payload):
                return None
            record = _normalized_record(payload, include_content=True)
            return record if record["id"] == normalized_id else None

        result = _run_sync(
            worker,
            wall_timeout=wall_timeout or timeout + 1.0,
            default=None,
        )
        return _observe_read("observe_record", result, normalized_id)

    async def call() -> Optional[dict[str, Any]]:
        server = _load_server()
        manager = getattr(server, "bucket_mgr", None)
        if manager is None:
            return None
        item = await manager.get(normalized_id)
        if not isinstance(item, dict) or _is_terminal_record(item):
            return None
        record = _normalized_record(item, include_content=True)
        return record if record["id"] == normalized_id else None

    result = _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=wall_timeout or timeout + 1.0,
        default=None,
    )
    return _observe_read("observe_record", result, normalized_id)


def _clamp_bipolar(value: Any) -> float:
    return max(-1.0, min(1.0, float(value)))


def _clamp_unit(value: Any) -> float:
    return max(0.0, min(1.0, float(value)))


def update_memory_emotion(
    bucket_id: str,
    valence: float,
    arousal: float,
    *,
    timeout: float = 30.0,
    wall_timeout: Optional[float] = None,
) -> Any:
    """Update emotion by ID using the backend's explicit trace/update path."""
    normalized_id = str(bucket_id or "").strip()
    if not normalized_id or "/" in normalized_id or chr(92) in normalized_id:
        raise ValueError("valid bucket_id required")
    stored_valence = round((_clamp_bipolar(valence) + 1.0) / 2.0, 3)
    stored_arousal = round(_clamp_unit(arousal), 3)

    if _backend() == "http":
        async def call() -> str:
            return await _mcp_call(
                "trace",
                {
                    "bucket_id": normalized_id,
                    "valence": stored_valence,
                    "arousal": stored_arousal,
                },
                timeout=timeout,
            )

        return _run_async(
            call,
            async_timeout=timeout,
            wall_timeout=wall_timeout or timeout + 2.0,
            default=None,
        )

    async def call_legacy() -> Any:
        server = _load_server()
        manager = getattr(server, "bucket_mgr", None)
        if manager is None:
            return False
        return await manager.update(
            normalized_id,
            valence=stored_valence,
            arousal=stored_arousal,
        )

    return _run_async(
        call_legacy,
        async_timeout=timeout,
        wall_timeout=wall_timeout or timeout + 1.0,
        default=False,
    )

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


def _http_handoff(*, timeout: float, wall_timeout: float) -> str:
    """HTTP handoff builder.

    Ombre 3.6.14 ``/api/buckets`` calls ``list_all(include_archive=True)``.
    Migrated terminal buckets are marked ``metadata.type == "archived"``;
    terminal markers are filtered here before any detail fetch.  Directory
    paths are intentionally not used to infer lifecycle state.
    """
    deadline = time.monotonic() + max(0.05, float(wall_timeout))

    def req_timeout() -> float:
        return min(timeout, max(0.05, deadline - time.monotonic()))

    buckets = _http_json(
        "/api/buckets",
        params={"sort": "created_desc"},
        timeout=req_timeout(),
        deadline=deadline,
    )
    sections = {"self_anchor": [], "user_portrait": [], "relationship": []}
    recent = []
    for meta in buckets if isinstance(buckets, list) else []:
        meta_type = str(meta.get("type") or "").strip().lower()
        if (
            meta_type == "archived"
            or meta.get("resolved")
            or meta.get("digested")
            or meta.get("dont_surface")
            or meta.get("deleted_at")
            or meta.get("tombstone")
        ):
            continue
        domains = meta.get("domain") or []
        if isinstance(domains, str):
            domains = [domains]
        matched = False
        for key in sections:
            if key in domains:
                sections[key].append(meta)
                matched = True
        if (
            not matched
            and meta.get("type") == "dynamic"
            and not meta.get("pinned")
        ):
            recent.append(meta)

    def detail(item: dict) -> str:
        payload = _http_json(
            f"/api/bucket/{item['id']}",
            timeout=req_timeout(),
            deadline=deadline,
        )
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
        result = _run_sync(
            lambda: _http_handoff(timeout=timeout, wall_timeout=wall_timeout),
            wall_timeout=wall_timeout,
            default=None,
        )
        return _observe_read("observe_handoff", result)

    async def call() -> str:
        server = _load_server()
        mode = os.environ.get("OMBRE_HANDOFF_MODE", "legacy").strip().lower()
        legacy = getattr(server, "handoff", None)
        if mode != "safe" and callable(legacy):
            return await legacy()
        return await _safe_handoff_async(server)

    result = _run_async(call, async_timeout=timeout, wall_timeout=wall_timeout, default=None)
    return _observe_read("observe_handoff", result)


def search_memories(
    query: str,
    *,
    limit: int = 2,
    timeout: float = 4.0,
    wall_timeout: Optional[float] = None,
    touch: bool = True,
) -> list[tuple[str, str]]:
    """Search Ombre directly.

    HTTP retrieval is intentionally read-only: ``touch=True`` remains a
    backward-compatible no-op because Ombre >=3.6.0 separates retrieval from
    explicit reinforcement.  Legacy-module touch behavior is unchanged.
    """
    query = str(query or "").strip()
    if not query:
        return []
    if _backend() == "http":
        def worker() -> list[tuple[str, str]]:
            deadline = time.monotonic() + max(0.05, float(wall_timeout or timeout + 1.0))

            def req_timeout() -> float:
                return min(timeout, max(0.05, deadline - time.monotonic()))

            matches = _http_json(
                "/api/search",
                params={"q": query},
                timeout=req_timeout(),
                deadline=deadline,
            )
            output: list[tuple[str, str]] = []
            for item in (matches or [])[: max(1, int(limit))]:
                detail = _http_json(
                    f"/api/bucket/{item['id']}",
                    timeout=req_timeout(),
                    deadline=deadline,
                )
                content = str(detail.get("content") or item.get("content_preview") or "").strip()
                if content:
                    output.append((str(item.get("name") or "记忆桶"), content[:300]))
            if touch:
                _LOG.debug(
                    "HTTP search keeps touch=%s as a read-only no-op; "
                    "modern Ombre reinforcement is explicit",
                    touch,
                )
            return output

        result = _run_sync(
            worker,
            wall_timeout=wall_timeout or timeout + 1.0,
            default=[],
        )
        return _observe_read("observe_search", result, query, limit=limit, touch=touch)

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

    result = _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=wall_timeout or timeout + 1.0,
        default=[],
    )
    return _observe_read("observe_search", result, query, limit=limit, touch=touch)


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
                result = {"valence": None, "arousal": None, "count": 0}
            else:
                result = {
                    "valence": round(sum(float(x.get("valence", 0.5)) for x in chosen) / len(chosen), 3),
                    "arousal": round(sum(float(x.get("arousal", 0.3)) for x in chosen) / len(chosen), 3),
                    "count": len(chosen),
                }
            return _observe_read("observe_emotion", result)
        except Exception:
            return _observe_read("observe_emotion", {"valence": None, "arousal": None, "count": 0})
    if mode != "safe":
        url = os.environ.get("OMBRE_EMOTION_SNAPSHOT_URL", _DEFAULT_SNAPSHOT_URL)
        try:
            request = urllib.request.Request(url)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read())
            valence = payload.get("valence")
            arousal = payload.get("arousal")
            if valence is not None and arousal is not None:
                return _observe_read("observe_emotion", {
                    "valence": float(valence),
                    "arousal": float(arousal),
                    "count": int(payload.get("count") or 0),
                })
        except Exception:
            return _observe_read("observe_emotion", {"valence": None, "arousal": None, "count": 0})

    async def call() -> dict[str, Any]:
        return await _safe_emotion_snapshot_async(_load_server())

    result = _run_async(
        call,
        async_timeout=timeout,
        wall_timeout=timeout + 1.0,
        default={"valence": None, "arousal": None, "count": 0},
    )
    return _observe_read("observe_emotion", result)
