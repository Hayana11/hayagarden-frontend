"""Fail-open Ombre 3.6.14 read-shadow dispatcher.

Legacy Ombre remains authoritative. This module freezes only safe observation
metadata, returns the authoritative object unchanged, and dispatches a detached
HTTP observer only when every explicit R4A gate permits it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import uuid
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

SHADOW_ENABLED_ENV = "OMBRE_READ_SHADOW_ENABLED"
HANDOFF_ENABLED_ENV = "OMBRE_READ_SHADOW_HANDOFF_ENABLED"
RECORDS_ENABLED_ENV = "OMBRE_READ_SHADOW_RECORDS_ENABLED"
EMOTION_ENABLED_ENV = "OMBRE_READ_SHADOW_EMOTION_ENABLED"
SEARCH_ENABLED_ENV = "OMBRE_READ_SHADOW_SEARCH_ENABLED"
DB_PATH_ENV = "OMBRE_READ_SHADOW_DB_PATH"
SLOT_FD_ENV = "OMBRE_READ_SHADOW_SLOT_FD"
BACKEND_ENV = "OMBRE_ADAPTER_BACKEND"
WORKER_MODULE = "tools.ombre_read_shadow_worker"
SHADOW_MAX_INFLIGHT_WORKERS = 2
EMOTION_TOLERANCE = 0.001
SEARCH_CANARY_QUERIES = frozenset({"记忆系统", "房间装修", "上下文压缩"})
_READ_OPERATIONS = frozenset({"handoff", "records", "record", "emotion", "search"})
_OPERATION_ENV = {
    "handoff": HANDOFF_ENABLED_ENV,
    "records": RECORDS_ENABLED_ENV,
    "record": RECORDS_ENABLED_ENV,
    "emotion": EMOTION_ENABLED_ENV,
    "search": SEARCH_ENABLED_ENV,
}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SLOT_ROOT = "ombre-read-shadow-r4a-slots"
_FORBIDDEN_ROOTS = (
    Path("/opt/ombre-brain"),
    Path("/var/lib/hayagarden/ombre-next-r0/vault"),
)
_FORBIDDEN_DB_NAMES = frozenset({"memories.db", "embeddings.db"})

_RECEIPT_TABLE = "ombre_read_shadow_receipts"
_RECEIPT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_RECEIPT_TABLE} (
    receipt_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    elapsed_ms INTEGER NOT NULL,
    authoritative_count INTEGER,
    shadow_count INTEGER,
    authoritative_hash TEXT,
    shadow_hash TEXT,
    ordered_authoritative_ids_json TEXT NOT NULL,
    ordered_shadow_ids_json TEXT NOT NULL,
    ordered_authoritative_content_hashes_json TEXT NOT NULL,
    ordered_shadow_content_hashes_json TEXT NOT NULL,
    exact_id_set INTEGER,
    exact_order INTEGER,
    overlap_count INTEGER,
    overlap_ratio REAL,
    top1_match INTEGER,
    metadata_json TEXT NOT NULL
)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def exact_one(environ: Mapping[str, str] | None, name: str) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(name, "")) == "1"


def shadow_enabled(environ: Mapping[str, str] | None = None) -> bool:
    return exact_one(environ, SHADOW_ENABLED_ENV)


def operation_enabled(
    operation: str,
    environ: Mapping[str, str] | None = None,
) -> bool:
    gate = _OPERATION_ENV.get(str(operation))
    return bool(gate and shadow_enabled(environ) and exact_one(environ, gate))


def legacy_backend_authoritative(
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get(BACKEND_ENV, "")).strip().lower() == "legacy_module"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_receipt_db_path(
    environ: Mapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environ is None else environ
    raw = str(env.get(DB_PATH_ENV, "")).strip()
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved.name.lower() in _FORBIDDEN_DB_NAMES:
        return None
    for root in _FORBIDDEN_ROOTS:
        if _is_under(resolved, root):
            return None
    production_root = str(env.get("PRODUCTION_OMBRE_ROOT", "")).strip()
    if production_root:
        try:
            if _is_under(resolved, Path(production_root).expanduser().resolve()):
                return None
        except (OSError, RuntimeError, ValueError):
            return None
    if resolved.suffix.lower() != ".db":
        return None
    return str(resolved)


class ShadowSlot:
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


def _slot_directory(db_path: str) -> Path:
    digest = hashlib.sha256(os.fsencode(db_path)).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / _SLOT_ROOT / digest


def try_acquire_shadow_slot(db_path: str) -> ShadowSlot | None:
    try:
        import fcntl
    except ImportError:
        return None
    try:
        directory = _slot_directory(db_path)
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(SHADOW_MAX_INFLIGHT_WORKERS):
            path = directory / f"slot-{index}.lock"
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            except OSError:
                os.close(fd)
                return None
            return ShadowSlot(fd, str(path))
    except Exception:
        return None
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def freeze_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, Mapping):
        return None
    bucket_id = str(record.get("id") or "").strip()
    if not bucket_id:
        return None
    content_hash = str(record.get("content_hash") or "").strip()
    if len(content_hash) != 64:
        content_hash = sha256_text(record.get("content"))
    domain = record.get("domain")
    tags = record.get("tags")
    return {
        "id": bucket_id,
        "content_hash": content_hash,
        "type": str(record.get("type") or ""),
        "domain": [str(item) for item in domain] if isinstance(domain, list) else [],
        "valence": _float(record.get("valence")),
        "arousal": _float(record.get("arousal")),
        "importance": _float(record.get("importance")),
        "last_active": str(record.get("last_active") or ""),
    }


def freeze_records(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, list):
        return []
    return [item for record in records if (item := freeze_record(record)) is not None]


def _record_semantic(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record.get("id"),
        "content_hash": record.get("content_hash"),
        "type": record.get("type"),
        "domain": record.get("domain") or [],
        "valence": record.get("valence"),
        "arousal": record.get("arousal"),
        "importance": record.get("importance"),
    }


def compare_record_lists(
    authoritative: list[dict[str, Any]],
    shadow: list[dict[str, Any]],
) -> dict[str, Any]:
    auth_ids = [str(item.get("id") or "") for item in authoritative]
    shadow_ids = [str(item.get("id") or "") for item in shadow]
    auth_by_id = {item["id"]: item for item in authoritative}
    shadow_by_id = {item["id"]: item for item in shadow}
    common = set(auth_by_id) & set(shadow_by_id)
    exact_set = set(auth_ids) == set(shadow_ids)
    exact_order = auth_ids == shadow_ids
    content_equal = (
        exact_set
        and all(
            auth_by_id[item]["content_hash"] == shadow_by_id[item]["content_hash"]
            for item in common
        )
    )
    semantic_equal = (
        content_equal
        and all(
            _record_semantic(auth_by_id[item]) == _record_semantic(shadow_by_id[item])
            for item in common
        )
    )
    lifecycle_drift = (
        semantic_equal
        and any(
            auth_by_id[item].get("last_active") != shadow_by_id[item].get("last_active")
            for item in common
        )
    )
    if lifecycle_drift:
        status = "lifecycle_only_drift"
    elif semantic_equal and exact_order:
        status = "exact"
    elif semantic_equal and exact_set:
        status = "same_set_different_order"
    elif common:
        status = "partial_overlap" if not exact_set else "divergent_valid"
    else:
        status = "divergent_valid"
    overlap = len(common)
    denominator = max(len(auth_ids), len(shadow_ids), 1)
    return {
        "status": status,
        "authoritative_hash": _hash_json(authoritative),
        "shadow_hash": _hash_json(shadow),
        "authoritative_count": len(auth_ids),
        "shadow_count": len(shadow_ids),
        "authoritative_ids": auth_ids,
        "shadow_ids": shadow_ids,
        "authoritative_content_hashes": [
            str(item.get("content_hash") or "") for item in authoritative
        ],
        "shadow_content_hashes": [
            str(item.get("content_hash") or "") for item in shadow
        ],
        "exact_id_set": exact_set,
        "exact_order": exact_order,
        "overlap_count": overlap,
        "overlap_ratio": overlap / denominator,
        "top1_match": bool(auth_ids and shadow_ids and auth_ids[0] == shadow_ids[0]),
        "metadata": {"last_active_drift": lifecycle_drift},
    }


def compare_handoff(
    authoritative: Mapping[str, Any],
    shadow_text: Any,
) -> dict[str, Any]:
    shadow_available = isinstance(shadow_text, str)
    shadow = {
        "available": shadow_available,
        "length": len(shadow_text) if shadow_available else 0,
        "sha256": sha256_text(shadow_text) if shadow_available else "",
    }
    exact = bool(authoritative.get("available")) and shadow_available and (
        authoritative.get("length") == shadow["length"]
        and authoritative.get("sha256") == shadow["sha256"]
    )
    return {
        "status": "exact" if exact else "divergent_valid" if shadow_available else "shadow_unavailable",
        "authoritative_hash": str(authoritative.get("sha256") or ""),
        "shadow_hash": shadow["sha256"],
        "authoritative_count": int(authoritative.get("length") or 0),
        "shadow_count": shadow["length"],
        "exact_id_set": None,
        "exact_order": exact,
        "overlap_count": None,
        "overlap_ratio": None,
        "top1_match": None,
        "metadata": {
            "authoritative_length": int(authoritative.get("length") or 0),
            "shadow_length": shadow["length"],
        },
    }


def freeze_search_results(results: Any) -> dict[str, Any]:
    rows = results if isinstance(results, list) else []
    names = []
    contents = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            names.append(sha256_text(str(row[0] or "")))
            contents.append(sha256_text(str(row[1] or "")))
        else:
            names.append(sha256_text(""))
            contents.append(sha256_text(""))
    return {
        "name_hashes": names,
        "content_hashes": contents,
        "result_hash": _hash_json({"names": names, "contents": contents}),
    }


def compare_search(
    authoritative: Mapping[str, Any],
    shadow_results: Any,
) -> dict[str, Any]:
    shadow = freeze_search_results(shadow_results)
    auth_names = list(authoritative.get("name_hashes") or [])
    shadow_names = list(shadow.get("name_hashes") or [])
    auth_contents = list(authoritative.get("content_hashes") or [])
    shadow_contents = list(shadow.get("content_hashes") or [])
    exact_set = set(auth_names) == set(shadow_names)
    exact_order = auth_names == shadow_names and auth_contents == shadow_contents
    pairs_equal = (
        exact_set
        and sorted(zip(auth_names, auth_contents))
        == sorted(zip(shadow_names, shadow_contents))
    )
    common = set(auth_names) & set(shadow_names)
    if exact_order:
        status = "exact"
    elif pairs_equal and exact_set:
        status = "same_set_different_order"
    elif common:
        status = "partial_overlap" if not exact_set else "divergent_valid"
    else:
        status = "divergent_valid"
    overlap = len(common)
    denominator = max(len(auth_names), len(shadow_names), 1)
    return {
        "status": status,
        "authoritative_hash": str(authoritative.get("result_hash") or ""),
        "shadow_hash": str(shadow.get("result_hash") or ""),
        "authoritative_count": len(auth_names),
        "shadow_count": len(shadow_names),
        "authoritative_ids": auth_names,
        "shadow_ids": shadow_names,
        "authoritative_content_hashes": auth_contents,
        "shadow_content_hashes": shadow_contents,
        "exact_id_set": exact_set,
        "exact_order": exact_order,
        "overlap_count": overlap,
        "overlap_ratio": overlap / denominator,
        "top1_match": bool(auth_names and shadow_names and auth_names[0] == shadow_names[0]),
        "metadata": {"result_kind": "name_and_content_hashes"},
    }


def compare_emotion(authoritative: Mapping[str, Any], shadow: Any) -> dict[str, Any]:
    shadow_map = shadow if isinstance(shadow, Mapping) else {}
    auth_count = int(authoritative.get("count") or 0)
    shadow_count = int(shadow_map.get("count") or 0)
    auth_valence = _float(authoritative.get("valence"))
    shadow_valence = _float(shadow_map.get("valence"))
    auth_arousal = _float(authoritative.get("arousal"))
    shadow_arousal = _float(shadow_map.get("arousal"))
    unavailable = (
        shadow is None
        or (shadow_valence is None and shadow_arousal is None and shadow_count == 0)
    )
    same = (
        not unavailable
        and auth_count == shadow_count
        and auth_valence is not None
        and shadow_valence is not None
        and auth_arousal is not None
        and shadow_arousal is not None
        and abs(auth_valence - shadow_valence) <= EMOTION_TOLERANCE
        and abs(auth_arousal - shadow_arousal) <= EMOTION_TOLERANCE
    )
    return {
        "status": "exact" if same else "shadow_unavailable" if unavailable else "divergent_valid",
        "authoritative_hash": _hash_json(dict(authoritative)),
        "shadow_hash": _hash_json(dict(shadow_map)),
        "authoritative_count": auth_count,
        "shadow_count": shadow_count,
        "exact_id_set": None,
        "exact_order": None,
        "overlap_count": None,
        "overlap_ratio": None,
        "top1_match": None,
        "metadata": {
            "authoritative_valence": auth_valence,
            "shadow_valence": shadow_valence,
            "authoritative_arousal": auth_arousal,
            "shadow_arousal": shadow_arousal,
            "tolerance": EMOTION_TOLERANCE,
        },
    }


def _dispatch(
    operation: str,
    event: dict[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> None:
    try:
        (dispatch or dispatch_shadow_event)(
            {"operation": operation, **event},
            environ=environ,
        )
    except Exception:
        return


def observe_handoff(
    authoritative_result: Any,
    *,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> Any:
    available = isinstance(authoritative_result, str)
    _dispatch(
        "handoff",
        {
            "authoritative": {
                "available": available,
                "length": len(authoritative_result) if available else 0,
                "sha256": sha256_text(authoritative_result) if available else "",
            }
        },
        environ=environ,
        dispatch=dispatch,
    )
    return authoritative_result


def observe_records(
    authoritative_result: Any,
    *,
    bucket_type: str | None = None,
    domain: str | None = None,
    min_arousal: float | None = None,
    include_content: bool = True,
    limit: int | None = None,
    sort: str = "last_active_desc",
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> Any:
    _dispatch(
        "records",
        {
            "authoritative": freeze_records(authoritative_result),
            "shadow_args": {
                "bucket_type": bucket_type,
                "domain": domain,
                "min_arousal": min_arousal,
                "include_content": bool(include_content),
                "limit": limit,
                "sort": sort,
            },
        },
        environ=environ,
        dispatch=dispatch,
    )
    return authoritative_result


def observe_record(
    authoritative_result: Any,
    bucket_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> Any:
    _dispatch(
        "record",
        {
            "bucket_id": str(bucket_id),
            "authoritative": freeze_record(authoritative_result),
        },
        environ=environ,
        dispatch=dispatch,
    )
    return authoritative_result


def observe_emotion(
    authoritative_result: Any,
    *,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> Any:
    source = authoritative_result if isinstance(authoritative_result, Mapping) else {}
    frozen = {
        "count": int(source.get("count") or 0),
        "valence": _float(source.get("valence")),
        "arousal": _float(source.get("arousal")),
    }
    _dispatch("emotion", {"authoritative": frozen}, environ=environ, dispatch=dispatch)
    return authoritative_result


def observe_search(
    authoritative_result: Any,
    query: str,
    *,
    limit: int = 5,
    touch: bool = False,
    environ: Mapping[str, str] | None = None,
    dispatch: Callable[..., Any] | None = None,
) -> Any:
    frozen = freeze_search_results(authoritative_result)
    _dispatch(
        "search",
        {
            "query": str(query),
            "limit": int(limit),
            "touch": bool(touch),
            "authoritative": frozen,
            "query_sha256": sha256_text(str(query)),
        },
        environ=environ,
        dispatch=dispatch,
    )
    return authoritative_result


def _event_receipt_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    authoritative = event.get("authoritative")
    if event.get("operation") == "search" and isinstance(authoritative, Mapping):
        ids = list(authoritative.get("name_hashes") or [])
        content_hashes = list(authoritative.get("content_hashes") or [])
        count = len(ids)
        result_hash = str(authoritative.get("result_hash") or "")
    elif isinstance(authoritative, list):
        ids = [str(item.get("id") or "") for item in authoritative if isinstance(item, Mapping)]
        content_hashes = [
            str(item.get("content_hash") or "") for item in authoritative if isinstance(item, Mapping)
        ]
        count = len(ids)
        result_hash = _hash_json(authoritative)
    elif isinstance(authoritative, Mapping):
        one_id = str(authoritative.get("id") or "")
        ids = [one_id] if one_id else []
        content_hashes = [str(authoritative.get("content_hash") or "")] if one_id else []
        count = int(authoritative.get("count") or 0)
        result_hash = _hash_json(dict(authoritative))
    else:
        ids, content_hashes, count, result_hash = [], [], 0, ""
    metadata = {"observer": "ombre-read-shadow-r4a"}
    if event.get("operation") == "search":
        metadata["query_sha256"] = sha256_text(str(event.get("query") or ""))
    return {
        "count": count,
        "hash": result_hash,
        "ids": ids,
        "content_hashes": content_hashes,
        "metadata": metadata,
    }


def _record_dispatch_status(
    db_path: str,
    event: Mapping[str, Any],
    *,
    status: str,
    error_code: str,
) -> None:
    """Best-effort non-blocking receipt for dropped/spawn-failed observations."""
    try:
        fields = _event_receipt_fields(event)
        connection = sqlite3.connect(db_path, timeout=0.0)
        try:
            connection.executescript(_RECEIPT_SCHEMA)
            connection.execute(
                f"""
                INSERT INTO {_RECEIPT_TABLE} (
                    receipt_id, operation, observed_at, finished_at, status,
                    error_code, elapsed_ms, authoritative_count, shadow_count,
                    authoritative_hash, shadow_hash,
                    ordered_authoritative_ids_json, ordered_shadow_ids_json,
                    ordered_authoritative_content_hashes_json,
                    ordered_shadow_content_hashes_json, exact_id_set, exact_order,
                    overlap_count, overlap_ratio, top1_match, metadata_json
                ) VALUES ({",".join("?" for _ in range(21))})
                """,
                (
                    uuid.uuid4().hex,
                    str(event.get("operation") or ""),
                    str(event.get("observed_at") or utc_now()),
                    utc_now(),
                    status,
                    error_code,
                    0,
                    fields["count"],
                    0,
                    fields["hash"],
                    "",
                    _canonical(fields["ids"]),
                    "[]",
                    _canonical(fields["content_hashes"]),
                    "[]",
                    None,
                    None,
                    0,
                    0.0,
                    None,
                    _canonical(fields["metadata"]),
                ),
            )
            connection.commit()
        finally:
            connection.close()
    except Exception:
        return


def dispatch_shadow_event(
    event: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., Any] | None = None,
) -> bool:
    env = dict(os.environ if environ is None else environ)
    operation = str(event.get("operation") or "")
    if operation not in _READ_OPERATIONS:
        return False
    if not operation_enabled(operation, env):
        return False
    if not legacy_backend_authoritative(env):
        return False
    if operation == "search":
        query = str(event.get("query") or "")
        if bool(event.get("touch")) or query not in SEARCH_CANARY_QUERIES:
            return False
    db_path = resolve_receipt_db_path(env)
    if not db_path:
        return False
    slot = try_acquire_shadow_slot(db_path)
    if slot is None:
        _record_dispatch_status(
            db_path,
            event,
            status="dropped_busy",
            error_code="dropped_busy",
        )
        return False
    child_env = dict(env)
    child_env[BACKEND_ENV] = "http"
    child_env[DB_PATH_ENV] = db_path
    child_env[SLOT_FD_ENV] = str(slot.fd)
    child_env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    payload = dict(event)
    payload["receipt_db_path"] = db_path
    try:
        spawn = popen or subprocess.Popen
        process = spawn(
            [sys.executable, "-B", "-m", WORKER_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
            close_fds=True,
            pass_fds=(slot.fd,),
            cwd=str(_REPO_ROOT),
            env=child_env,
        )
        if process.stdin is not None:
            process.stdin.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            process.stdin.close()
        return True
    except Exception:
        _record_dispatch_status(
            db_path,
            event,
            status="shadow_error",
            error_code="shadow_error",
        )
        return False
    finally:
        slot.close()


__all__ = [
    "DB_PATH_ENV",
    "EMOTION_TOLERANCE",
    "HANDOFF_ENABLED_ENV",
    "RECORDS_ENABLED_ENV",
    "EMOTION_ENABLED_ENV",
    "SEARCH_ENABLED_ENV",
    "SEARCH_CANARY_QUERIES",
    "SHADOW_ENABLED_ENV",
    "SHADOW_MAX_INFLIGHT_WORKERS",
    "compare_emotion",
    "compare_handoff",
    "compare_record_lists",
    "compare_search",
    "dispatch_shadow_event",
    "exact_one",
    "freeze_record",
    "freeze_records",
    "freeze_search_results",
    "legacy_backend_authoritative",
    "observe_emotion",
    "observe_handoff",
    "observe_record",
    "observe_records",
    "observe_search",
    "operation_enabled",
    "resolve_receipt_db_path",
    "shadow_enabled",
    "sha256_text",
    "try_acquire_shadow_slot",
]
