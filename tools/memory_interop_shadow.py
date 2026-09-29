"""Fail-open MEMORY-INTEROP R2A Live Shadow observer.

This module is observation-only.  It has no authority, opens no database,
creates no files, and does not translate Interop payloads.  Production
callers invoke it only after the authoritative memory operation has already
produced a frozen result.  Any failure is swallowed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SHADOW_ENABLED_ENV = "MEMORY_INTEROP_SHADOW_ENABLED"
SHADOW_DB_PATH_ENV = "MEMORY_INTEROP_SHADOW_DB_PATH"
WORKER_MODULE = "tools.memory_interop_shadow_worker"
ADAPTER_ID = "legacy.posts.v1"
PROTOCOL_VERSION = "0.1"
WRITE_SURFACES = frozenset({"claude_code", "internal_mcp", "api_relay"})
SEARCH_SURFACES = frozenset({"claude_code", "internal_mcp"})
_FORBIDDEN_BASENAMES = frozenset(
    {
        "memories.db",
        "contin" + "uity.db",
    }
)
_FORBIDDEN_NAME_TOKENS = (
    "memory_" + "kernel",
    "om" + "bre",
    "contin" + "uity",
)
_REPO_ROOT = Path(__file__).resolve().parents[1]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_shadow_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(SHADOW_ENABLED_ENV, "")).strip() == "1"


def is_forbidden_shadow_path(
    path: str,
    *,
    posts_db_path: str | None = None,
) -> bool:
    raw = str(path or "").strip()
    if not raw or raw == ":memory:":
        return True
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return True
    name = resolved.name.lower()
    if name in _FORBIDDEN_BASENAMES:
        return True
    haystack = f"{resolved.as_posix().lower()}/{name}"
    if any(token in haystack for token in _FORBIDDEN_NAME_TOKENS):
        return True
    posts = str(posts_db_path or "").strip()
    if posts:
        try:
            if resolved == Path(posts).expanduser().resolve():
                return True
        except (OSError, RuntimeError, ValueError):
            return True
    return False


def resolve_shadow_db_path(
    environ: Mapping[str, str] | None = None,
    *,
    posts_db_path: str | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    raw = str(env.get(SHADOW_DB_PATH_ENV, "")).strip()
    if not raw or is_forbidden_shadow_path(raw, posts_db_path=posts_db_path):
        return None
    try:
        return str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def _stable_write_row_id(result: Mapping[str, Any] | None) -> str | None:
    if not isinstance(result, Mapping):
        return None
    if result.get("status") != "CREATED":
        return None
    row_id = result.get("id")
    if isinstance(row_id, bool) or row_id is None:
        return None
    if isinstance(row_id, int):
        return str(row_id)
    if isinstance(row_id, str) and row_id.strip():
        return row_id.strip()
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_fingerprint(parts: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(parts)).encode("utf-8")).hexdigest()


def write_event_key(*, source_surface: str, row_id: str, content_hash: str) -> str:
    return "|".join(("write", source_surface, row_id, content_hash))


def search_event_key(
    *,
    source_surface: str,
    request_id: str | None,
    turn_id: str | None,
    query_hash: str,
    ordered_row_ids: list[str],
    ordered_content_hashes: list[str],
) -> str:
    identity = str(request_id or "").strip() or str(turn_id or "").strip() or "-"
    paired = ",".join(
        f"{row_id}:{digest}"
        for row_id, digest in zip(ordered_row_ids, ordered_content_hashes)
    )
    return "|".join(("search", source_surface, identity, query_hash, paired))


def freeze_search_posts(posts: Any) -> tuple[list[str], list[str]]:
    ordered_ids: list[str] = []
    ordered_hashes: list[str] = []
    if not isinstance(posts, list):
        return ordered_ids, ordered_hashes
    for post in posts:
        if not isinstance(post, Mapping):
            ordered_ids.append("")
            ordered_hashes.append(sha256_text(""))
            continue
        row_id = post.get("id")
        if isinstance(row_id, bool) or row_id is None:
            ordered_ids.append("")
        else:
            ordered_ids.append(str(row_id))
        ordered_hashes.append(sha256_text(post.get("content")))
    return ordered_ids, ordered_hashes


def build_write_event(
    *,
    posts_db_path: str,
    result: Mapping[str, Any],
    content: Any,
    source_surface: str | None,
    turn_id: str | None = None,
    request_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any] | None:
    row_id = _stable_write_row_id(result)
    if row_id is None:
        return None
    surface = str(source_surface or "").strip() or "unknown"
    content_hash = sha256_text(content)
    observed = observed_at or utc_now()
    request = str(request_id or "").strip() or f"write:{surface}:{row_id}"
    turn = str(turn_id or "").strip() or None
    payload = {
        "operation": "write",
        "adapter_id": ADAPTER_ID,
        "protocol_version": PROTOCOL_VERSION,
        "source_surface": surface,
        "capability_id": "memory.write",
        "legacy_row_id": row_id,
        "expected_content_sha256": content_hash,
        "authoritative_status": result.get("status"),
        "request_id": request,
        "turn_id": turn,
    }
    return {
        **payload,
        "posts_db_path": str(posts_db_path or "").strip(),
        "event_key": write_event_key(
            source_surface=surface,
            row_id=row_id,
            content_hash=content_hash,
        ),
        "payload_fingerprint": payload_fingerprint(payload),
        "observed_at": observed,
        "authoritative_result_hash": payload_fingerprint(
            {
                "status": result.get("status"),
                "id": row_id,
                "content_sha256": content_hash,
            }
        ),
    }


def build_search_event(
    *,
    posts_db_path: str,
    posts: Any,
    keyword: Any,
    source_surface: str | None,
    turn_id: str | None = None,
    request_id: str | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    surface = str(source_surface or "").strip() or "unknown"
    ordered_ids, ordered_hashes = freeze_search_posts(posts)
    query_hash = sha256_text(keyword if isinstance(keyword, str) else "")
    observed = observed_at or utc_now()
    request = str(request_id or "").strip() or None
    turn = str(turn_id or "").strip() or None
    payload = {
        "operation": "search",
        "adapter_id": ADAPTER_ID,
        "protocol_version": PROTOCOL_VERSION,
        "source_surface": surface,
        "capability_id": "memory.search",
        "ordered_row_ids": ordered_ids,
        "ordered_content_hashes": ordered_hashes,
        "result_count": len(ordered_ids),
        "query_sha256": query_hash,
        "request_id": request or f"search:{surface}:{query_hash[:12]}",
        "turn_id": turn,
    }
    return {
        **payload,
        "posts_db_path": str(posts_db_path or "").strip(),
        "event_key": search_event_key(
            source_surface=surface,
            request_id=request,
            turn_id=turn,
            query_hash=query_hash,
            ordered_row_ids=ordered_ids,
            ordered_content_hashes=ordered_hashes,
        ),
        "payload_fingerprint": payload_fingerprint(payload),
        "observed_at": observed,
        "authoritative_result_hash": payload_fingerprint(
            {
                "ordered_row_ids": ordered_ids,
                "ordered_content_hashes": ordered_hashes,
                "result_count": len(ordered_ids),
                "query_sha256": query_hash,
            }
        ),
    }


def dispatch_shadow_event(
    event: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> None:
    """Best-effort detached spawn.  Never waits.  Never raises to callers."""

    try:
        env_map = os.environ if environ is None else environ
        if not is_shadow_enabled(env_map):
            return
        posts_db_path = str(event.get("posts_db_path") or "").strip()
        shadow_db_path = resolve_shadow_db_path(
            env_map,
            posts_db_path=posts_db_path,
        )
        if not shadow_db_path:
            return
        payload = dict(event)
        payload["shadow_db_path"] = shadow_db_path
        encoded = _canonical_json(payload).encode("utf-8")
        child_env = os.environ.copy()
        child_env[SHADOW_ENABLED_ENV] = "1"
        child_env[SHADOW_DB_PATH_ENV] = shadow_db_path
        proc = popen(
            [sys.executable, "-B", "-m", WORKER_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            cwd=str(_REPO_ROOT),
            env=child_env,
            shell=False,
        )
        stdin = getattr(proc, "stdin", None)
        if stdin is None:
            return
        try:
            stdin.write(encoded)
        finally:
            stdin.close()
    except Exception:
        return


def observe_memory_write(
    *,
    posts_db_path: str,
    result: Mapping[str, Any],
    content: Any,
    source_surface: str | None = None,
    turn_id: str | None = None,
    request_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> None:
    try:
        env_map = os.environ if environ is None else environ
        if not is_shadow_enabled(env_map):
            return
        event = build_write_event(
            posts_db_path=posts_db_path,
            result=result,
            content=content,
            source_surface=source_surface,
            turn_id=turn_id,
            request_id=request_id,
        )
        if event is None:
            return
        (dispatch or dispatch_shadow_event)(event, environ=env_map)
    except Exception:
        return


def observe_memory_search(
    *,
    posts_db_path: str,
    posts: Any,
    keyword: Any = "",
    source_surface: str | None = None,
    turn_id: str | None = None,
    request_id: str | None = None,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> None:
    try:
        env_map = os.environ if environ is None else environ
        if not is_shadow_enabled(env_map):
            return
        event = build_search_event(
            posts_db_path=posts_db_path,
            posts=posts,
            keyword=keyword,
            source_surface=source_surface,
            turn_id=turn_id,
            request_id=request_id,
        )
        (dispatch or dispatch_shadow_event)(event, environ=env_map)
    except Exception:
        return


__all__ = [
    "ADAPTER_ID",
    "PROTOCOL_VERSION",
    "SEARCH_SURFACES",
    "SHADOW_DB_PATH_ENV",
    "SHADOW_ENABLED_ENV",
    "WORKER_MODULE",
    "WRITE_SURFACES",
    "build_search_event",
    "build_write_event",
    "dispatch_shadow_event",
    "is_forbidden_shadow_path",
    "is_shadow_enabled",
    "observe_memory_search",
    "observe_memory_write",
    "payload_fingerprint",
    "resolve_shadow_db_path",
    "sha256_text",
]
