"""Detached MEMORY-INTEROP R2A Live Shadow worker.

Reads a frozen observation event from stdin, translates already-stored
legacy posts through the R1 adapter, and writes a receipt to the dedicated
shadow SQLite file.  It never writes legacy posts, Kernel, or Ombre, never
calls a provider, and never participates in the authoritative result.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.memory_interop import (
    MEMORY_INTEROP_PROTOCOL_VERSION,
    InteropRequestContext,
)
from tools.memory_interop_legacy import (
    LEGACY_POSTS_ADAPTER_ID,
    legacy_post_to_submission_envelope,
    legacy_posts_readonly,
    legacy_rows_to_context_bundle,
)
from tools.memory_interop_shadow import (
    ADAPTER_ID,
    PROTOCOL_VERSION,
    is_forbidden_shadow_path,
    sha256_text,
)

RECEIPT_TABLE = "memory_interop_shadow_receipts"
_RECEIPT_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {RECEIPT_TABLE} (
    receipt_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    payload_fingerprint TEXT NOT NULL,
    operation TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    source_surface TEXT NOT NULL,
    request_id TEXT,
    turn_id TEXT,
    capability_id TEXT,
    observed_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL,
    authoritative_result_hash TEXT,
    interop_result_hash TEXT,
    ordered_source_refs_json TEXT,
    result_count INTEGER,
    error_code TEXT,
    elapsed_ms INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_shadow_receipts_event_key
    ON {RECEIPT_TABLE} (event_key);
"""
_STATUSES = frozenset(
    {
        "ok",
        "source_changed",
        "missing_row",
        "hash_mismatch",
        "translation_failed",
        "receipt_failed",
        "conflict",
        "duplicate",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _receipt_id(event_key: str, payload_fingerprint: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memory-interop-shadow:{event_key}:{payload_fingerprint}",
        )
    )


def _bundle_fingerprint(bundle: Any) -> str:
    payload = {
        "contributor_adapter_ids": list(bundle.contributor_adapter_ids),
        "item_ids": [item.item_id for item in bundle.items],
        "source_refs": [list(item.source_refs) for item in bundle.items],
        "content_hashes": [sha256_text(item.content) for item in bundle.items],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _open_receipt_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_RECEIPT_SCHEMA)
    return conn


def _store_receipt(conn: sqlite3.Connection, receipt: Mapping[str, Any]) -> str:
    event_key = str(receipt["event_key"])
    fingerprint = str(receipt["payload_fingerprint"])
    existing = conn.execute(
        f"SELECT receipt_id, payload_fingerprint, status FROM {RECEIPT_TABLE} "
        "WHERE event_key = ? ORDER BY finished_at ASC",
        (event_key,),
    ).fetchall()
    matches = [row for row in existing if row["payload_fingerprint"] == fingerprint]
    if matches:
        receipt["status"] = "duplicate" if receipt.get("status") == "ok" else receipt.get("status")
        return str(matches[0]["receipt_id"])
    conflicts = [row for row in existing if row["payload_fingerprint"] != fingerprint]
    status = str(receipt["status"])
    if status not in _STATUSES:
        status = "receipt_failed"
        receipt["status"] = status
    stored_key = event_key
    error_code = receipt.get("error_code")
    if conflicts:
        status = "conflict"
        error_code = "event_conflict"
        stored_key = f"{event_key}|conflict|{fingerprint}"
        receipt["status"] = status
        receipt["error_code"] = error_code
    receipt_id = str(receipt.get("receipt_id") or _receipt_id(stored_key, fingerprint))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {RECEIPT_TABLE} (
            receipt_id, event_key, payload_fingerprint, operation,
            protocol_version, adapter_id, source_surface, request_id, turn_id,
            capability_id, observed_at, finished_at, status,
            authoritative_result_hash, interop_result_hash,
            ordered_source_refs_json, result_count, error_code, elapsed_ms,
            metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            receipt_id,
            stored_key,
            fingerprint,
            receipt["operation"],
            receipt["protocol_version"],
            receipt["adapter_id"],
            receipt["source_surface"],
            receipt.get("request_id"),
            receipt.get("turn_id"),
            receipt.get("capability_id"),
            receipt["observed_at"],
            receipt["finished_at"],
            status,
            receipt.get("authoritative_result_hash"),
            receipt.get("interop_result_hash"),
            receipt.get("ordered_source_refs_json"),
            receipt.get("result_count"),
            error_code,
            receipt.get("elapsed_ms"),
            _canonical_json(receipt.get("metadata") or {}),
        ),
    )
    conn.commit()
    return receipt_id


def _fetch_rows(posts_db_path: str, row_ids: list[str]) -> list[dict[str, Any] | None]:
    with legacy_posts_readonly(posts_db_path) as conn:
        found: list[dict[str, Any] | None] = []
        for row_id in row_ids:
            row = conn.execute(
                "SELECT * FROM posts WHERE id = ?",
                (row_id,),
            ).fetchone()
            found.append(None if row is None else {str(key): row[key] for key in row.keys()})
        return found


def _request_context(event: Mapping[str, Any], *, capability_id: str) -> InteropRequestContext:
    adapter_id = str(event.get("adapter_id") or ADAPTER_ID)
    observed_at = str(event.get("observed_at") or _utc_now())
    return InteropRequestContext(
        protocol_version=str(event.get("protocol_version") or PROTOCOL_VERSION),
        request_id=str(event.get("request_id") or "shadow-request"),
        trigger_kind="live_shadow",
        adapter_id=adapter_id,
        requested_at=observed_at,
        turn_id=event.get("turn_id"),
        capability_id=capability_id,
        metadata={
            "source_surface": event.get("source_surface"),
            "observation_only": True,
        },
    )


def _classify_rows(
    rows: list[dict[str, Any] | None],
    expected_hashes: list[str],
) -> tuple[str | None, str | None]:
    for index, row in enumerate(rows):
        if row is None:
            return "missing_row", "missing_row"
        content = row.get("content")
        actual = sha256_text(content if isinstance(content, str) else "")
        expected = expected_hashes[index] if index < len(expected_hashes) else ""
        if actual != expected:
            return "hash_mismatch", "hash_mismatch"
    return None, None


def _translate_write(event: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    observed_at = str(event.get("observed_at") or _utc_now())
    envelope = legacy_post_to_submission_envelope(
        row,
        request_context=_request_context(event, capability_id="memory.write"),
        submission_id=f"shadow-write-{event.get('legacy_row_id')}",
        idempotency_key=str(event.get("event_key")),
        submitted_at=observed_at,
        observed_at=observed_at,
    )
    row_id = str(row.get("id"))
    return envelope.semantic_fingerprint(), {
        "ordered_source_refs": [f"legacy://posts/{row_id}"],
        "result_count": 1,
        "legacy_row_id": row_id,
    }


def _translate_search(
    event: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    observed_at = str(event.get("observed_at") or _utc_now())
    bundle = legacy_rows_to_context_bundle(
        rows,
        request_context=_request_context(event, capability_id="memory.search"),
        bundle_id=f"shadow-search-{event.get('request_id') or 'anon'}",
        generated_at=observed_at,
        selection_query="",
        selection_limit=len(rows),
        order="authoritative",
        bundle_metadata={
            "legacy_search": {
                "mode": "explicit_rows",
                "order": "authoritative",
                "query_sha256": event.get("query_sha256"),
            }
        },
    )
    refs = [item.source_refs[0] for item in bundle.items if item.source_refs]
    item_ids = [item.item_id.removeprefix("legacy.posts.") for item in bundle.items]
    expected_ids = [str(row.get("id")) for row in rows]
    if item_ids != expected_ids:
        raise ValueError("context bundle order drifted from authoritative ids")
    return _bundle_fingerprint(bundle), {
        "ordered_source_refs": refs,
        "result_count": len(rows),
    }


def _base_receipt(event: Mapping[str, Any], *, started: float) -> dict[str, Any]:
    metadata = {
        "observation_only": True,
        "kernel_commit": False,
        "query_sha256": event.get("query_sha256"),
        "expected_content_sha256": event.get("expected_content_sha256"),
    }
    return {
        "receipt_id": _receipt_id(
            str(event.get("event_key") or ""),
            str(event.get("payload_fingerprint") or ""),
        ),
        "event_key": event.get("event_key"),
        "payload_fingerprint": event.get("payload_fingerprint"),
        "operation": event.get("operation"),
        "protocol_version": event.get("protocol_version") or MEMORY_INTEROP_PROTOCOL_VERSION,
        "adapter_id": event.get("adapter_id") or ADAPTER_ID,
        "source_surface": event.get("source_surface") or "unknown",
        "request_id": event.get("request_id"),
        "turn_id": event.get("turn_id"),
        "capability_id": event.get("capability_id"),
        "observed_at": event.get("observed_at") or _utc_now(),
        "finished_at": _utc_now(),
        "status": "receipt_failed",
        "authoritative_result_hash": event.get("authoritative_result_hash"),
        "interop_result_hash": None,
        "ordered_source_refs_json": None,
        "result_count": event.get("result_count"),
        "error_code": None,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "metadata": metadata,
    }


def process_event(event: Mapping[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    receipt = _base_receipt(event, started=started)
    posts_db_path = str(event.get("posts_db_path") or "").strip()
    shadow_db_path = str(event.get("shadow_db_path") or "").strip()
    if not shadow_db_path or is_forbidden_shadow_path(
        shadow_db_path,
        posts_db_path=posts_db_path,
    ):
        receipt["error_code"] = "invalid_shadow_db"
        receipt["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return receipt
    if str(event.get("adapter_id") or ADAPTER_ID) != LEGACY_POSTS_ADAPTER_ID:
        receipt["status"] = "translation_failed"
        receipt["error_code"] = "adapter_id_mismatch"
    operation = str(event.get("operation") or "")
    try:
        if receipt["status"] != "translation_failed":
            if operation == "write":
                row_id = str(event.get("legacy_row_id") or "")
                rows = _fetch_rows(posts_db_path, [row_id]) if row_id else [None]
                status, error = _classify_rows(
                    rows,
                    [str(event.get("expected_content_sha256") or "")],
                )
                if status:
                    receipt["status"] = status
                    receipt["error_code"] = error
                    if rows and rows[0] is not None:
                        receipt["ordered_source_refs_json"] = _canonical_json(
                            [f"legacy://posts/{rows[0].get('id')}"]
                        )
                    elif status == "missing_row":
                        receipt["ordered_source_refs_json"] = _canonical_json(
                            [f"legacy://posts/{row_id}"]
                        )
                else:
                    fingerprint, extra = _translate_write(event, rows[0])
                    receipt["status"] = "ok"
                    receipt["interop_result_hash"] = fingerprint
                    receipt["ordered_source_refs_json"] = _canonical_json(
                        extra["ordered_source_refs"]
                    )
                    receipt["result_count"] = extra["result_count"]
                    receipt["metadata"]["legacy_row_id"] = extra["legacy_row_id"]
            elif operation == "search":
                ordered_ids = [
                    str(item) for item in (event.get("ordered_row_ids") or [])
                ]
                expected_hashes = [
                    str(item) for item in (event.get("ordered_content_hashes") or [])
                ]
                rows = _fetch_rows(posts_db_path, ordered_ids)
                status, error = _classify_rows(rows, expected_hashes)
                if status:
                    receipt["status"] = status
                    receipt["error_code"] = error
                    present_refs = [
                        f"legacy://posts/{row.get('id')}"
                        for row in rows
                        if row is not None
                    ]
                    receipt["ordered_source_refs_json"] = _canonical_json(present_refs)
                    receipt["result_count"] = len(ordered_ids)
                else:
                    present = [row for row in rows if row is not None]
                    fingerprint, extra = _translate_search(event, present)
                    receipt["status"] = "ok"
                    receipt["interop_result_hash"] = fingerprint
                    receipt["ordered_source_refs_json"] = _canonical_json(
                        extra["ordered_source_refs"]
                    )
                    receipt["result_count"] = extra["result_count"]
            else:
                receipt["status"] = "translation_failed"
                receipt["error_code"] = "unknown_operation"
    except Exception as exc:
        receipt["status"] = "translation_failed"
        receipt["error_code"] = type(exc).__name__
    receipt["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    receipt["finished_at"] = _utc_now()
    try:
        conn = _open_receipt_db(shadow_db_path)
        try:
            receipt["receipt_id"] = _store_receipt(conn, receipt)
        finally:
            conn.close()
    except Exception:
        receipt["status"] = "receipt_failed"
        receipt["error_code"] = receipt.get("error_code") or "receipt_failed"
    return receipt


def main() -> int:
    try:
        raw = sys.stdin.read()
        event = json.loads(raw or "{}")
        if not isinstance(event, dict):
            return 0
        process_event(event)
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    if str(Path(__file__).resolve().parents[1]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
