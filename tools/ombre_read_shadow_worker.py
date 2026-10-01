"""Detached read-only Ombre shadow worker.

The worker receives a frozen event over stdin, performs only HTTP reads against
the pinned sidecar, and stores hashes, IDs, counts, and numeric diagnostics in
the dedicated receipt database. It never writes Ombre memory or logs payloads.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from typing import Any, Mapping

from tools import ombre_adapter
from tools.ombre_read_shadow import (
    DB_PATH_ENV,
    SLOT_FD_ENV,
    compare_emotion,
    compare_handoff,
    compare_record_lists,
    compare_search,
    freeze_record,
    freeze_records,
    freeze_search_results,
    sha256_text,
    utc_now,
)

RECEIPT_TABLE = "ombre_read_shadow_receipts"
_HELD_SLOT = None
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
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


def retain_slot_fd() -> None:
    global _HELD_SLOT
    raw = str(os.environ.get(SLOT_FD_ENV, "")).strip()
    if not raw:
        return
    try:
        _HELD_SLOT = os.fdopen(int(raw), "a+b", buffering=0)
    except Exception:
        _HELD_SLOT = None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error_code(code: str | None) -> str | None:
    allowed = {
        "shadow_unavailable",
        "shadow_timeout",
        "shadow_error",
        "auth_401",
        "malformed_response",
        "receipt_db_locked",
    }
    return code if code in allowed else None


def _unavailable(comparison: dict[str, Any], code: str) -> dict[str, Any]:
    result = dict(comparison)
    result["status"] = code
    result["metadata"] = dict(result.get("metadata") or {})
    result["metadata"]["error_code"] = code
    return result


def _exception_code(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, TimeoutError)):
        return "shadow_timeout"
    if getattr(exc, "code", None) == 401:
        return "auth_401"
    if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
        return "malformed_response"
    return "shadow_error"


def _record_compare(event: Mapping[str, Any]) -> dict[str, Any]:
    authoritative = event.get("authoritative")
    auth_list = authoritative if isinstance(authoritative, list) else []
    args = dict(event.get("shadow_args") or {})
    shadow = ombre_adapter.list_memory_records(**args)
    shadow_list = freeze_records(shadow)
    result = compare_record_lists(auth_list, shadow_list)
    if auth_list and not shadow_list:
        return _unavailable(result, "shadow_unavailable")
    return result


def _single_record_compare(event: Mapping[str, Any]) -> dict[str, Any]:
    authoritative = event.get("authoritative")
    auth_list = [authoritative] if isinstance(authoritative, Mapping) else []
    shadow = ombre_adapter.get_memory_record(str(event.get("bucket_id") or ""))
    shadow_list = [freeze_record(shadow)] if shadow is not None else []
    shadow_list = [item for item in shadow_list if item is not None]
    result = compare_record_lists(auth_list, shadow_list)
    if auth_list and not shadow_list:
        return _unavailable(result, "shadow_unavailable")
    return result


def _handoff_compare(event: Mapping[str, Any]) -> dict[str, Any]:
    shadow = ombre_adapter.get_handoff(timeout=3.0, wall_timeout=4.0)
    result = compare_handoff(dict(event.get("authoritative") or {}), shadow)
    if shadow is None:
        return _unavailable(result, "shadow_unavailable")
    return result


def _emotion_compare(event: Mapping[str, Any]) -> dict[str, Any]:
    shadow = ombre_adapter.get_emotion_snapshot(timeout=3.0)
    result = compare_emotion(dict(event.get("authoritative") or {}), shadow)
    if result["status"] == "shadow_unavailable":
        return result
    return result


def _search_compare(event: Mapping[str, Any]) -> dict[str, Any]:
    query = str(event.get("query") or "")
    shadow = ombre_adapter.search_memories(
        query,
        limit=int(event.get("limit") or 5),
        touch=False,
        timeout=4.0,
        wall_timeout=5.0,
    )
    result = compare_search(dict(event.get("authoritative") or {}), shadow)
    if event.get("authoritative") and not shadow:
        return _unavailable(result, "shadow_unavailable")
    result["metadata"] = dict(result.get("metadata") or {})
    result["metadata"]["query_sha256"] = sha256_text(query)
    return result


def _empty_comparison() -> dict[str, Any]:
    return {
        "status": "shadow_error",
        "authoritative_hash": "",
        "shadow_hash": "",
        "authoritative_count": 0,
        "shadow_count": 0,
        "authoritative_ids": [],
        "shadow_ids": [],
        "authoritative_content_hashes": [],
        "shadow_content_hashes": [],
        "exact_id_set": None,
        "exact_order": None,
        "overlap_count": 0,
        "overlap_ratio": 0.0,
        "top1_match": None,
        "metadata": {},
    }


def _write_receipt(
    db_path: str,
    event: Mapping[str, Any],
    comparison: Mapping[str, Any],
    *,
    observed_at: str,
    finished_at: str,
    elapsed_ms: int,
) -> None:
    metadata = dict(comparison.get("metadata") or {})
    metadata["observer"] = "ombre-read-shadow-r4a"
    metadata["operation"] = str(event.get("operation") or "")
    if event.get("operation") == "search":
        metadata["query_sha256"] = sha256_text(str(event.get("query") or ""))
    values = (
        uuid.uuid4().hex,
        str(event.get("operation") or ""),
        observed_at,
        finished_at,
        str(comparison.get("status") or "shadow_error"),
        _safe_error_code(str(metadata.get("error_code") or "")) if metadata.get("error_code") else None,
        int(elapsed_ms),
        comparison.get("authoritative_count"),
        comparison.get("shadow_count"),
        str(comparison.get("authoritative_hash") or ""),
        str(comparison.get("shadow_hash") or ""),
        _json(comparison.get("authoritative_ids") or []),
        _json(comparison.get("shadow_ids") or []),
        _json(comparison.get("authoritative_content_hashes") or []),
        _json(comparison.get("shadow_content_hashes") or []),
        None if comparison.get("exact_id_set") is None else int(bool(comparison.get("exact_id_set"))),
        None if comparison.get("exact_order") is None else int(bool(comparison.get("exact_order"))),
        comparison.get("overlap_count"),
        comparison.get("overlap_ratio"),
        None if comparison.get("top1_match") is None else int(bool(comparison.get("top1_match"))),
        _json(metadata),
    )
    try:
        target = Path(db_path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(target), timeout=1.0)
        try:
            connection.executescript(_SCHEMA)
            connection.execute(
                f"""
                INSERT INTO {RECEIPT_TABLE} (
                    receipt_id, operation, observed_at, finished_at, status,
                    error_code, elapsed_ms, authoritative_count, shadow_count,
                    authoritative_hash, shadow_hash,
                    ordered_authoritative_ids_json, ordered_shadow_ids_json,
                    ordered_authoritative_content_hashes_json,
                    ordered_shadow_content_hashes_json, exact_id_set, exact_order,
                    overlap_count, overlap_ratio, top1_match, metadata_json
                ) VALUES ({",".join("?" for _ in values)})
                """,
                values,
            )
            connection.commit()
        finally:
            connection.close()
    except Exception:
        return


def run_event(event: Mapping[str, Any]) -> None:
    operation = str(event.get("operation") or "")
    observed_at = str(event.get("observed_at") or utc_now())
    started = time.monotonic()
    comparison: dict[str, Any]
    try:
        if operation == "handoff":
            comparison = _handoff_compare(event)
        elif operation == "records":
            comparison = _record_compare(event)
        elif operation == "record":
            comparison = _single_record_compare(event)
        elif operation == "emotion":
            comparison = _emotion_compare(event)
        elif operation == "search":
            comparison = _search_compare(event)
        else:
            return
    except Exception as exc:
        comparison = _unavailable(_empty_comparison(), _exception_code(exc))
    elapsed_ms = int(max(0.0, time.monotonic() - started) * 1000)
    _write_receipt(
        str(event.get("receipt_db_path") or os.environ.get(DB_PATH_ENV) or ""),
        event,
        comparison,
        observed_at=observed_at,
        finished_at=utc_now(),
        elapsed_ms=elapsed_ms,
    )


def main() -> None:
    retain_slot_fd()
    try:
        raw = sys.stdin.buffer.read()
        event = json.loads(raw.decode("utf-8"))
        if isinstance(event, Mapping):
            run_event(event)
    except Exception:
        return


if __name__ == "__main__":
    main()
