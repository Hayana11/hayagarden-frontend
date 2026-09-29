"""Fail-open MEMORY-INTEROP R2A Live Shadow observer.

This module is observation-only.  It has no authority, opens no database
on the authoritative path, and does not translate Interop payloads.
Production callers invoke it only after the authoritative memory operation
has already produced a frozen result.  Any failure is swallowed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SHADOW_ENABLED_ENV = "MEMORY_INTEROP_SHADOW_ENABLED"
SHADOW_SEARCH_ENABLED_ENV = "MEMORY_INTEROP_SHADOW_SEARCH_ENABLED"
SHADOW_WRITE_ENABLED_ENV = "MEMORY_INTEROP_SHADOW_WRITE_ENABLED"
SHADOW_DB_PATH_ENV = "MEMORY_INTEROP_SHADOW_DB_PATH"
SHADOW_SLOT_FD_ENV = "MEMORY_INTEROP_SHADOW_SLOT_FD"
SHADOW_MAX_INFLIGHT_WORKERS = 2
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
_SLOT_ROOT_NAME = "haya-memory-interop-shadow-slots"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _exact_one(environ: Mapping[str, str] | None, name: str) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(name, "")).strip() == "1"


def is_shadow_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return _exact_one(environ, SHADOW_ENABLED_ENV)


def is_shadow_search_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return is_shadow_enabled(environ) and _exact_one(environ, SHADOW_SEARCH_ENABLED_ENV)


def is_shadow_write_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return is_shadow_enabled(environ) and _exact_one(environ, SHADOW_WRITE_ENABLED_ENV)


def is_allowed_write_surface(surface: Any) -> bool:
    return str(surface or "").strip() in WRITE_SURFACES


def is_allowed_search_surface(surface: Any) -> bool:
    return str(surface or "").strip() in SEARCH_SURFACES


def _same_existing_file(left: str, right: str) -> bool | None:
    """Return True if both paths name the same inode.

    False means distinct (or one path does not exist yet).  None means the
    comparison itself failed and Shadow must not proceed.
    """

    try:
        if not (os.path.exists(left) and os.path.exists(right)):
            return False
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None


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
            posts_resolved = Path(posts).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return True
        if resolved == posts_resolved:
            return True
        same = _same_existing_file(str(resolved), str(posts_resolved))
        if same is not False:
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


def slot_namespace_dir(shadow_db_path: str) -> Path:
    digest = hashlib.sha256(os.fsencode(str(shadow_db_path))).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / _SLOT_ROOT_NAME / digest


class ShadowSlot:
    """Cross-process advisory lock slot.  Closing the fd releases this copy."""

    def __init__(self, fd: int, path: str):
        self.fd = fd
        self.path = path
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self.fd)
        except OSError:
            pass


def try_acquire_shadow_slot(shadow_db_path: str) -> ShadowSlot | None:
    """Non-blocking cross-process slot.  None means drop the sample."""

    try:
        import fcntl
    except ImportError:
        return None
    path = str(shadow_db_path or "").strip()
    if not path:
        return None
    try:
        directory = slot_namespace_dir(path)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(SHADOW_MAX_INFLIGHT_WORKERS):
            slot_path = directory / f"slot-{index}.lock"
            fd = os.open(str(slot_path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            except OSError:
                os.close(fd)
                return None
            return ShadowSlot(fd, str(slot_path))
        return None
    except Exception:
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
    surface = str(source_surface or "").strip()
    if surface not in WRITE_SURFACES:
        return None
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
) -> dict[str, Any] | None:
    surface = str(source_surface or "").strip()
    if surface not in SEARCH_SURFACES:
        return None
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


def _surface_allowed_for_event(event: Mapping[str, Any]) -> bool:
    operation = str(event.get("operation") or "")
    surface = event.get("source_surface")
    if operation == "write":
        return is_allowed_write_surface(surface)
    if operation == "search":
        return is_allowed_search_surface(surface)
    return False


def _operation_enabled_for_event(
    event: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> bool:
    operation = str(event.get("operation") or "")
    if operation == "write":
        return is_shadow_write_enabled(environ)
    if operation == "search":
        return is_shadow_search_enabled(environ)
    return False


def dispatch_shadow_event(
    event: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] | None = None,
) -> None:
    """Best-effort detached spawn.  Never waits.  Never raises to callers."""

    slot: ShadowSlot | None = None
    try:
        env_map = os.environ if environ is None else environ
        if not is_shadow_enabled(env_map):
            return
        if not _operation_enabled_for_event(event, env_map):
            return
        if not _surface_allowed_for_event(event):
            return
        posts_db_path = str(event.get("posts_db_path") or "").strip()
        shadow_db_path = resolve_shadow_db_path(
            env_map,
            posts_db_path=posts_db_path,
        )
        if not shadow_db_path:
            return
        slot = try_acquire_shadow_slot(shadow_db_path)
        if slot is None:
            return
        payload = dict(event)
        payload["shadow_db_path"] = shadow_db_path
        encoded = _canonical_json(payload).encode("utf-8")
        child_env = os.environ.copy()
        child_env[SHADOW_ENABLED_ENV] = "1"
        child_env[SHADOW_DB_PATH_ENV] = shadow_db_path
        child_env[SHADOW_SLOT_FD_ENV] = str(slot.fd)
        spawn = popen if popen is not None else subprocess.Popen
        proc = spawn(
            [sys.executable, "-B", "-m", WORKER_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(slot.fd,),
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
    finally:
        if slot is not None:
            slot.close()


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
        if not is_shadow_write_enabled(env_map):
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
        if not is_shadow_search_enabled(env_map):
            return
        event = build_search_event(
            posts_db_path=posts_db_path,
            posts=posts,
            keyword=keyword,
            source_surface=source_surface,
            turn_id=turn_id,
            request_id=request_id,
        )
        if event is None:
            return
        (dispatch or dispatch_shadow_event)(event, environ=env_map)
    except Exception:
        return


__all__ = [
    "ADAPTER_ID",
    "PROTOCOL_VERSION",
    "SEARCH_SURFACES",
    "SHADOW_DB_PATH_ENV",
    "SHADOW_ENABLED_ENV",
    "SHADOW_MAX_INFLIGHT_WORKERS",
    "SHADOW_SEARCH_ENABLED_ENV",
    "SHADOW_SLOT_FD_ENV",
    "SHADOW_WRITE_ENABLED_ENV",
    "WORKER_MODULE",
    "WRITE_SURFACES",
    "ShadowSlot",
    "build_search_event",
    "build_write_event",
    "dispatch_shadow_event",
    "is_allowed_search_surface",
    "is_allowed_write_surface",
    "is_forbidden_shadow_path",
    "is_shadow_enabled",
    "is_shadow_search_enabled",
    "is_shadow_write_enabled",
    "observe_memory_search",
    "observe_memory_write",
    "payload_fingerprint",
    "resolve_shadow_db_path",
    "sha256_text",
    "slot_namespace_dir",
    "try_acquire_shadow_slot",
]
