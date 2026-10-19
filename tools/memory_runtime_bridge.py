"""Narrow first-phase memory runtime bridge.

This module keeps legacy posts authoritative and treats Ombre as a best-effort
compatibility projection. It owns provenance, deterministic idempotency,
persistent receipts, safe automatic-candidate shadowing, and read-after-write
observations. It intentionally does not import or write the Memory Kernel.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("memory_runtime")
_RECEIPT_SCHEMA_VERSION = 1
_MAX_PROVENANCE_VALUE = 240
_MAX_CANDIDATE_CHARS = 1200
_MIN_CANDIDATE_CHARS = 20

_AUTO_INTENT_RE = re.compile(
    r"(记住|记得|请保存|以后|将来|偏好|喜欢|不喜欢|习惯|希望|决定|计划|不要|需要)",
    re.IGNORECASE,
)
_SENSITIVE_RE = re.compile(
    r"(password|passwd|api[_ -]?key|token|secret|bearer|验证码|密码|口令|"
    r"身份证|银行卡|私钥|助记词)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[0-9A-Za-z一-龥_]{2,}")

_HANDOFF_FALLBACK_SOURCES = ("save_memory", "save_marker")
_HANDOFF_FALLBACK_MAX_ITEMS = 3
_HANDOFF_FALLBACK_MAX_CHARS = 900
_HANDOFF_SOURCE_LABELS = {
    "save_memory": "【legacy save_memory】",
    "save_marker": "【legacy SAVE marker】",
}


def _now() -> str:
    return __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat(timespec="milliseconds")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def content_sha256(content: str) -> str:
    return _sha256((content or "").strip())


def _bounded(value: Any) -> str:
    return str(value or "").strip()[:_MAX_PROVENANCE_VALUE]


def normalize_provenance(source: str, provenance: dict[str, Any] | None = None) -> dict[str, str]:
    raw = provenance if isinstance(provenance, dict) else {}
    result = {"source": _bounded(source) or "unknown"}
    for key in (
        "request_id",
        "session_id",
        "turn_id",
        "window_id",
        "surface",
        "actor",
    ):
        value = _bounded(raw.get(key))
        if value:
            result[key] = value
    return result


def deterministic_idempotency_key(
    content: str,
    *,
    source: str,
    provenance: dict[str, Any] | None = None,
) -> str:
    normalized = normalize_provenance(source, provenance)
    identity = "|".join(
        [
            normalized.get("source", ""),
            normalized.get("request_id", ""),
            normalized.get("session_id", ""),
            normalized.get("turn_id", ""),
            normalized.get("window_id", ""),
            content_sha256(content),
        ]
    )
    return "mrtk-" + _sha256(identity)


def stage_receipt_id(receipt_id: str, stage: str) -> str:
    return "mre-" + _sha256(f"{receipt_id}|{stage}")


def receipt_db_path(posts_db_path: str | None = None) -> str:
    configured = os.environ.get("MEMORY_RUNTIME_RECEIPT_DB_PATH", "").strip()
    if configured:
        return configured
    posts_path = str(posts_db_path or "/opt/frontend/memories.db")
    if os.path.abspath(posts_path) == "/opt/frontend/memories.db":
        return "/var/lib/hayagarden/memory-runtime-receipts.db"
    return posts_path + ".runtime-receipts.db"


def _connect(path: str) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_runtime_receipts (
            receipt_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            turn_id TEXT NOT NULL DEFAULT '',
            window_id TEXT NOT NULL DEFAULT '',
            content_sha256 TEXT NOT NULL,
            legacy_memory_id INTEGER,
            ombre_status TEXT NOT NULL DEFAULT 'NOT_ATTEMPTED',
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_runtime_receipt_events (
            event_id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            legacy_memory_id INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(receipt_id, stage)
        );
        CREATE INDEX IF NOT EXISTS idx_memory_runtime_receipts_legacy_id
            ON memory_runtime_receipts(legacy_memory_id);
        CREATE INDEX IF NOT EXISTS idx_memory_runtime_events_receipt
            ON memory_runtime_receipt_events(receipt_id);
        """
    )
    conn.commit()
    return conn


def verify_receipt_store(path: str | None = None) -> dict[str, Any]:
    """Verify the receipt DB can enable WAL and commit a real transaction."""
    target = str(path or receipt_db_path())
    conn: sqlite3.Connection | None = None
    probe_id = "preflight-" + uuid.uuid4().hex
    try:
        conn = _connect(target)
        journal_mode = str(
            conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
        if journal_mode != "wal":
            raise RuntimeError("wal_not_enabled")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO memory_runtime_receipts (
                receipt_id, idempotency_key, status, source,
                request_id, session_id, turn_id, window_id,
                content_sha256, legacy_memory_id, ombre_status,
                error_code, created_at, updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                probe_id,
                probe_id,
                "PREFLIGHT",
                "receipt_preflight",
                "",
                "",
                "",
                "",
                content_sha256(probe_id),
                None,
                "NOT_ATTEMPTED",
                "",
                _now(),
                _now(),
                _RECEIPT_SCHEMA_VERSION,
            ),
        )
        conn.commit()
        conn.close()
        conn = _connect(target)
        committed = conn.execute(
            "SELECT status FROM memory_runtime_receipts WHERE receipt_id=?",
            (probe_id,),
        ).fetchone()
        if committed is None or committed[0] != "PREFLIGHT":
            raise RuntimeError("receipt_commit_not_observable")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM memory_runtime_receipts WHERE receipt_id=?",
            (probe_id,),
        )
        conn.commit()
        return {
            "status": "VERIFIED",
            "path": target,
            "journal_mode": journal_mode,
        }
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        _LOG.warning(
            "memory_runtime_receipt_preflight_failed error_type=%s",
            type(exc).__name__,
        )
        return {
            "status": "FAILED",
            "path": target,
            "error_code": type(exc).__name__,
        }
    finally:
        if conn is not None:
            conn.close()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _reserve(
    *,
    posts_db_path: str,
    idempotency_key: str,
    source: str,
    provenance: dict[str, str],
    content_hash: str,
    status: str = "PENDING",
) -> tuple[dict[str, Any], bool]:
    path = receipt_db_path(posts_db_path)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM memory_runtime_receipts WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return dict(existing), False
        receipt_id = "mrr-" + uuid.uuid4().hex
        now = _now()
        conn.execute(
            """
            INSERT INTO memory_runtime_receipts (
                receipt_id, idempotency_key, status, source,
                request_id, session_id, turn_id, window_id,
                content_sha256, created_at, updated_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                idempotency_key,
                status,
                source,
                provenance.get("request_id", ""),
                provenance.get("session_id", ""),
                provenance.get("turn_id", ""),
                provenance.get("window_id", ""),
                content_hash,
                now,
                now,
                _RECEIPT_SCHEMA_VERSION,
            ),
        )
        conn.commit()
        return (
            {
                "receipt_id": receipt_id,
                "idempotency_key": idempotency_key,
                "status": status,
                "source": source,
                "content_sha256": content_hash,
                "legacy_memory_id": None,
                "ombre_status": "NOT_ATTEMPTED",
            },
            True,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update_receipt(
    posts_db_path: str,
    receipt_id: str,
    *,
    status: str | None = None,
    legacy_memory_id: int | None = None,
    ombre_status: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any] | None:
    conn = _connect(receipt_db_path(posts_db_path))
    try:
        fields = ["updated_at=?"]
        values: list[Any] = [_now()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if legacy_memory_id is not None:
            fields.append("legacy_memory_id=?")
            values.append(int(legacy_memory_id))
        if ombre_status is not None:
            fields.append("ombre_status=?")
            values.append(str(ombre_status)[:80])
        if error_code is not None:
            fields.append("error_code=?")
            values.append(str(error_code)[:160])
        values.append(receipt_id)
        conn.execute(
            f"UPDATE memory_runtime_receipts SET {', '.join(fields)} WHERE receipt_id=?",
            values,
        )
        conn.commit()
        return _row_dict(
            conn.execute(
                "SELECT * FROM memory_runtime_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        )
    finally:
        conn.close()


def _receipt(posts_db_path: str, receipt_id: str) -> dict[str, Any] | None:
    conn = _connect(receipt_db_path(posts_db_path))
    try:
        return _row_dict(
            conn.execute(
                "SELECT * FROM memory_runtime_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        )
    finally:
        conn.close()


def _event(
    posts_db_path: str,
    receipt_id: str,
    stage: str,
    status: str,
    *,
    legacy_memory_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    event_id = stage_receipt_id(receipt_id, stage)
    conn = _connect(receipt_db_path(posts_db_path))
    try:
        metadata_json = json.dumps(
            metadata or {}, ensure_ascii=False, sort_keys=True
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO memory_runtime_receipt_events (
                event_id, receipt_id, stage, status, legacy_memory_id,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                receipt_id,
                stage,
                status,
                legacy_memory_id,
                metadata_json,
                _now(),
            ),
        )
        if status == "VERIFIED":
            conn.execute(
                """
                UPDATE memory_runtime_receipt_events
                SET status='VERIFIED',
                    legacy_memory_id=COALESCE(?, legacy_memory_id),
                    metadata_json=?
                WHERE event_id=? AND status <> 'VERIFIED'
                """,
                (legacy_memory_id, metadata_json, event_id),
            )
        conn.commit()
    finally:
        conn.close()
    _LOG.info(
        "memory_runtime_receipt receipt_id=%s event_id=%s stage=%s status=%s",
        receipt_id,
        event_id,
        stage,
        status,
    )
    return event_id


def _find_exact_legacy(posts_db_path: str, content: str) -> int | None:
    conn = sqlite3.connect(posts_db_path, timeout=10)
    try:
        row = conn.execute(
            "SELECT id FROM posts WHERE content=? ORDER BY id ASC LIMIT 1",
            (content,),
        ).fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _write_legacy(
    posts_db_path: str,
    content: str,
    *,
    type: str,
    author: str,
    layer: str,
    tags: str,
    importance: int,
    pinned: int,
) -> int | None:
    from tools import memory_tool

    previous = memory_tool.DB_PATH
    memory_tool.DB_PATH = posts_db_path
    try:
        return memory_tool.save_memory(
            content,
            type=type,
            author=author,
            layer=layer,
            tags=tags,
            importance=importance,
            pinned=pinned,
        )
    finally:
        memory_tool.DB_PATH = previous


_OMBRE_DIAGNOSTIC_STATUSES = frozenset(
    {"WRITTEN", "TIMEOUT", "EXCEPTION", "RETURNED_NONE", "BACKEND_UNAVAILABLE"}
)


def _write_ombre(
    content: str,
    *,
    tags: str,
    importance: int,
    pinned: bool,
    receipt_id: str | None = None,
    posts_db_path: str | None = None,
) -> tuple[str, dict[str, Any]]:
    def record_late_completion(observation: dict[str, Any]) -> None:
        final_status = str(
            observation.get("final_status")
            or observation.get("status")
            or "EXCEPTION"
        )
        metadata = {
            "correlation_id": str(
                observation.get("correlation_id") or receipt_id or ""
            ),
            "final_status": final_status,
            "final_inner_stage": str(
                observation.get("final_inner_stage")
                or observation.get("inner_stage")
                or "unknown"
            ),
            "error_type": str(observation.get("error_type") or ""),
            "total_elapsed_ms": observation.get("total_elapsed_ms"),
        }
        log = _LOG.info if final_status == "WRITTEN" else _LOG.warning
        log(
            "memory_runtime_ombre_late_completion receipt_id=%s "
            "final_status=%s final_inner_stage=%s error_type=%s "
            "total_elapsed_ms=%s",
            receipt_id or "",
            metadata["final_status"],
            metadata["final_inner_stage"],
            metadata["error_type"],
            metadata["total_elapsed_ms"],
        )
        if posts_db_path and receipt_id:
            try:
                _event(
                    posts_db_path,
                    receipt_id,
                    "ombre_late_completion",
                    "OBSERVED",
                    metadata=metadata,
                )
            except Exception as exc:
                _LOG.warning(
                    "memory_runtime_ombre_late_completion_event_failed "
                    "receipt_id=%s error_type=%s",
                    receipt_id,
                    exc.__class__.__name__,
                    exc_info=True,
                )

    try:
        from tools import ombre_adapter

        raw_diagnostic = ombre_adapter.hold_memory_diagnostic(
            content,
            tags=tags,
            importance=importance,
            pinned=pinned,
            timeout=3.0,
            wall_timeout=4.0,
            correlation_id=receipt_id,
            late_completion_observer=record_late_completion,
        )
        if not isinstance(raw_diagnostic, dict):
            raise TypeError("invalid_ombre_diagnostic")
        status = raw_diagnostic.get("status")
        if status not in _OMBRE_DIAGNOSTIC_STATUSES:
            raise ValueError("invalid_ombre_diagnostic_status")
        diagnostic = {
            "status": str(status),
            "backend": str(raw_diagnostic.get("backend") or ""),
            "stage": str(raw_diagnostic.get("stage") or ""),
            "outer_stage": str(
                raw_diagnostic.get("outer_stage")
                or raw_diagnostic.get("stage")
                or ""
            ),
            "inner_stage": str(
                raw_diagnostic.get("inner_stage")
                or raw_diagnostic.get("stage")
                or ""
            ),
            "server_cached_before_call": bool(
                raw_diagnostic.get("server_cached_before_call", False)
            ),
            "worker_alive_at_return": bool(
                raw_diagnostic.get("worker_alive_at_return", False)
            ),
            "async_timeout_ms": raw_diagnostic.get("async_timeout_ms"),
            "wall_timeout_ms": raw_diagnostic.get("wall_timeout_ms"),
            "failure_class": str(
                raw_diagnostic.get("failure_class")
                or (status if status != "WRITTEN" else "")
            ),
            "root_cause": str(
                raw_diagnostic.get("root_cause")
                or ("unknown" if status != "WRITTEN" else "")
            ),
            "error_type": str(raw_diagnostic.get("error_type") or ""),
            "elapsed_ms": raw_diagnostic.get("elapsed_ms"),
        }
        attribution_fields = (
            "missing_module",
            "pid",
            "python_executable",
            "cwd",
            "ombre_root",
            "ombre_root_in_sys_path",
            "server_spec_origin",
            "server_in_sys_modules",
            "server_module_file",
        )
        for field in attribution_fields:
            if field not in raw_diagnostic:
                continue
            value = raw_diagnostic[field]
            if field in {
                "missing_module",
                "python_executable",
                "cwd",
                "ombre_root",
                "server_spec_origin",
                "server_module_file",
            }:
                diagnostic[field] = str(value or "")[:500]
            elif field in {
                "ombre_root_in_sys_path",
                "server_in_sys_modules",
            }:
                diagnostic[field] = bool(value)
            else:
                diagnostic[field] = value


    except Exception as exc:
        try:
            backend = str(ombre_adapter._backend())
        except Exception:
            backend = ""
        diagnostic = {
            "status": "EXCEPTION",
            "backend": backend,
            "stage": "adapter",
            "outer_stage": "adapter",
            "inner_stage": "adapter",
            "server_cached_before_call": False,
            "worker_alive_at_return": False,
            "async_timeout_ms": None,
            "wall_timeout_ms": None,
            "failure_class": "EXCEPTION",
            "root_cause": "unknown",
            "error_type": type(exc).__name__,
            "elapsed_ms": 0.0,
        }
        _LOG.warning(
            "memory_runtime_ombre_diagnostic_error receipt_id=%s error_type=%s",
            receipt_id or "",
            type(exc).__name__,
        )

    log = _LOG.info if diagnostic["status"] == "WRITTEN" else _LOG.warning
    log(
        "memory_runtime_ombre_diagnostic receipt_id=%s backend=%s "
        "status=%s stage=%s outer_stage=%s inner_stage=%s "
        "failure_class=%s root_cause=%s error_type=%s elapsed_ms=%s",
        receipt_id or "",
        diagnostic["backend"],
        diagnostic["status"],
        diagnostic["stage"],
        diagnostic["outer_stage"],
        diagnostic["inner_stage"],
        diagnostic["failure_class"],
        diagnostic["root_cause"],
        diagnostic["error_type"],
        diagnostic["elapsed_ms"],
    )
    legacy_status = "WRITTEN" if diagnostic["status"] == "WRITTEN" else "FAILED"
    return legacy_status, diagnostic


def _result(row: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
    return {
        "status": status or row.get("status") or "UNKNOWN",
        "receipt_id": row.get("receipt_id"),
        "idempotency_key": row.get("idempotency_key"),
        "legacy_memory_id": row.get("legacy_memory_id"),
        "ombre_status": row.get("ombre_status", "NOT_ATTEMPTED"),
        "error_code": row.get("error_code", ""),
        "content_sha256": row.get("content_sha256"),
    }


def persist_memory(
    posts_db_path: str,
    *,
    content: Any,
    source: str,
    provenance: dict[str, Any] | None = None,
    idempotency_content: str | None = None,
    type: str = "MEMORY",
    author: str = "fyodor",
    layer: str = "recent",
    tags: str = "",
    importance: int = 0,
    pinned: int = 0,
    write_ombre: bool = True,
) -> dict[str, Any]:
    if not isinstance(content, str):
        return {"status": "INVALID_CONTENT"}
    text = content.strip()
    if not text:
        return {"status": "INVALID_CONTENT"}

    prov = normalize_provenance(source, provenance)
    content_hash = content_sha256(text)
    idempotency_basis = (
        idempotency_content.strip()
        if isinstance(idempotency_content, str) and idempotency_content.strip()
        else text
    )
    key = deterministic_idempotency_key(
        idempotency_basis, source=source, provenance=prov
    )
    try:
        row, created = _reserve(
            posts_db_path=posts_db_path,
            idempotency_key=key,
            source=prov["source"],
            provenance=prov,
            content_hash=content_hash,
        )
    except Exception as exc:
        _LOG.warning(
            "memory_runtime_receipt_unavailable error_type=%s",
            exc.__class__.__name__,
        )
        return {
            "status": "RECEIPT_FAILED",
            "receipt_id": None,
            "idempotency_key": key,
            "legacy_memory_id": None,
            "ombre_status": "NOT_ATTEMPTED",
            "error_code": "receipt_db_unavailable",
            "content_sha256": content_hash,
        }
    if not created:
        if row.get("status") == "COMMITTED":
            return _result(row, status="DEDUPLICATED")
        if row.get("status") == "PENDING":
            existing_id = _find_exact_legacy(posts_db_path, text)
            if existing_id is not None:
                row = _update_receipt(
                    posts_db_path,
                    row["receipt_id"],
                    status="COMMITTED",
                    legacy_memory_id=existing_id,
                ) or row
                _event(
                    posts_db_path,
                    row["receipt_id"],
                    "memory_persisted",
                    "VERIFIED",
                    legacy_memory_id=existing_id,
                    metadata={"recovered": True},
                )
                return _result(row, status="DEDUPLICATED")
            return _result(row, status="IN_PROGRESS")
        if row.get("status") == "FAILED":
            row = _update_receipt(
                posts_db_path,
                row["receipt_id"],
                status="PENDING",
                error_code="",
            ) or row
        else:
            return _result(row)

    try:
        legacy_id = _find_exact_legacy(posts_db_path, text)
        if legacy_id is None:
            legacy_id = _write_legacy(
                posts_db_path,
                text,
                type=type,
                author=author,
                layer=layer,
                tags=tags,
                importance=int(importance),
                pinned=int(bool(pinned)),
            )
        if legacy_id is None:
            raise RuntimeError("legacy_write_returned_no_id")

        ombre_status = "NOT_REQUESTED"
        ombre_diagnostic = None
        if write_ombre:
            ombre_status, ombre_diagnostic = _write_ombre(
                text,
                tags=tags,
                importance=int(importance),
                pinned=bool(pinned),
                receipt_id=row["receipt_id"],
                posts_db_path=posts_db_path,
            )
        row = _update_receipt(
            posts_db_path,
            row["receipt_id"],
            status="COMMITTED",
            legacy_memory_id=int(legacy_id),
            ombre_status=ombre_status,
            error_code="",
        ) or row
        event_metadata = {"ombre_status": ombre_status}
        if ombre_diagnostic is not None:
            event_metadata["ombre_diagnostic"] = ombre_diagnostic
        _event(
            posts_db_path,
            row["receipt_id"],
            "memory_persisted",
            "VERIFIED",
            legacy_memory_id=int(legacy_id),
            metadata=event_metadata,
        )
        return _result(row, status="CREATED")
    except Exception as exc:
        failed_row = row
        try:
            failed_row = _update_receipt(
                posts_db_path,
                row["receipt_id"],
                status="FAILED",
                error_code=exc.__class__.__name__,
            ) or row
        except Exception:
            _LOG.warning(
                "memory_runtime_receipt_update_failed receipt_id=%s",
                row.get("receipt_id"),
                exc_info=True,
            )
        try:
            _event(
                posts_db_path,
                row["receipt_id"],
                "memory_persisted",
                "FAILED",
                metadata={"error_type": exc.__class__.__name__},
            )
        except Exception:
            _LOG.warning(
                "memory_runtime_receipt_event_failed receipt_id=%s",
                row.get("receipt_id"),
                exc_info=True,
            )
        _LOG.warning(
            "memory_runtime_persist_failed receipt_id=%s error_type=%s",
            row.get("receipt_id"),
            exc.__class__.__name__,
        )
        return _result(failed_row, status="FAILED")


def observe_session_memo(
    posts_db_path: str,
    *,
    content: Any,
    provenance: dict[str, Any] | None = None,
    idempotency_content: str | None = None,
    tags: str = "memo,网页窗口,跨端",
    importance: int = 4,
    write_ombre: bool = True,
) -> dict[str, Any]:
    """Observe/project a session memo without writing legacy posts."""
    if not isinstance(content, str):
        return {"status": "INVALID_CONTENT", "legacy_write": "DISABLED"}
    text = content.strip()
    if not text:
        return {"status": "INVALID_CONTENT", "legacy_write": "DISABLED"}

    prov = normalize_provenance("session_memo", provenance)
    basis = (
        idempotency_content.strip()
        if isinstance(idempotency_content, str) and idempotency_content.strip()
        else text
    )
    key = deterministic_idempotency_key(
        basis, source=prov["source"], provenance=prov
    )
    content_hash = content_sha256(text)
    try:
        row, created = _reserve(
            posts_db_path=posts_db_path,
            idempotency_key=key,
            source=prov["source"],
            provenance=prov,
            content_hash=content_hash,
            status="OBSERVED",
        )
    except Exception as exc:
        _LOG.warning(
            "memory_runtime_session_receipt_unavailable error_type=%s",
            type(exc).__name__,
        )
        return {
            "status": "RECEIPT_FAILED",
            "receipt_id": None,
            "idempotency_key": key,
            "legacy_memory_id": None,
            "ombre_status": "NOT_ATTEMPTED",
            "error_code": "receipt_db_unavailable",
            "legacy_write": "DISABLED",
            "content_sha256": content_hash,
        }

    if not created:
        if row.get("status") == "OBSERVED":
            result = _result(row, status="DEDUPLICATED")
            result["legacy_write"] = "DISABLED"
            return result
        if row.get("status") == "PENDING":
            result = _result(row, status="IN_PROGRESS")
            result["legacy_write"] = "DISABLED"
            return result
        result = _result(row)
        result["legacy_write"] = "DISABLED"
        return result

    ombre_status = "NOT_REQUESTED"
    ombre_diagnostic = None
    if write_ombre:
        ombre_status, ombre_diagnostic = _write_ombre(
            text,
            tags=tags,
            importance=int(importance),
            pinned=False,
            receipt_id=row["receipt_id"],
            posts_db_path=posts_db_path,
        )
    try:
        row = _update_receipt(
            posts_db_path,
            row["receipt_id"],
            status="OBSERVED",
            ombre_status=ombre_status,
            error_code="",
        ) or row
        _event(
            posts_db_path,
            row["receipt_id"],
            "session_memo_observed",
            "OBSERVED",
            metadata={
                "legacy_write": "DISABLED",
                "ombre_status": ombre_status,
                **(
                    {"ombre_diagnostic": ombre_diagnostic}
                    if ombre_diagnostic is not None
                    else {}
                ),
            },
        )
    except Exception as exc:
        _LOG.warning(
            "memory_runtime_session_receipt_commit_failed receipt_id=%s error_type=%s",
            row.get("receipt_id"),
            type(exc).__name__,
        )
        return {
            "status": "RECEIPT_FAILED",
            "receipt_id": row.get("receipt_id"),
            "idempotency_key": key,
            "legacy_memory_id": None,
            "ombre_status": ombre_status,
            "error_code": "receipt_db_unavailable",
            "legacy_write": "DISABLED",
            "content_sha256": content_hash,
        }
    result = _result(row, status="OBSERVED")
    result["legacy_write"] = "DISABLED"
    return result


def _query_hash(query: str) -> str:
    return _sha256((query or "").strip())


def record_legacy_recall(
    posts_db_path: str,
    legacy_memory_ids: list[int],
    *,
    query: str = "",
    provenance: dict[str, Any] | None = None,
) -> list[str]:
    if not legacy_memory_ids:
        return []
    ids = sorted({int(value) for value in legacy_memory_ids if value is not None})
    conn = _connect(receipt_db_path(posts_db_path))
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT receipt_id, legacy_memory_id
            FROM memory_runtime_receipts
            WHERE status='COMMITTED' AND legacy_memory_id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    finally:
        conn.close()
    events = []
    for row in rows:
        events.append(
            _event(
                posts_db_path,
                row["receipt_id"],
                "memory_recalled_injected",
                "VERIFIED",
                legacy_memory_id=row["legacy_memory_id"],
                metadata={
                    "query_sha256": _query_hash(query),
                    "source": normalize_provenance(
                        "chat_recall", provenance
                    ).get("source"),
                },
            )
        )
    return events


def verify_read_after_write(
    posts_db_path: str,
    receipt_id: str,
    *,
    query: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = _receipt(posts_db_path, receipt_id)
    if row is None or row.get("status") != "COMMITTED":
        return {"status": "UNVERIFIED", "receipt_id": receipt_id}
    memory_id = row.get("legacy_memory_id")
    if memory_id is None:
        return {"status": "UNVERIFIED", "receipt_id": receipt_id}
    conn = sqlite3.connect(posts_db_path, timeout=10)
    try:
        found = conn.execute(
            "SELECT content, tags, resolved FROM posts WHERE id=?",
            (int(memory_id),),
        ).fetchone()
    except sqlite3.Error:
        found = None
    finally:
        conn.close()
    if not found or int(found[2] or 0):
        _event(posts_db_path, receipt_id, "memory_recalled_injected", "UNVERIFIED")
        return {"status": "UNVERIFIED", "receipt_id": receipt_id}
    haystack = f"{found[0] or ''} {found[1] or ''}"
    tokens = _TOKEN_RE.findall((query or "").strip())
    matched = bool(tokens) and all(token in haystack for token in tokens)
    if not matched:
        _event(posts_db_path, receipt_id, "memory_recalled_injected", "UNVERIFIED")
        return {"status": "UNVERIFIED", "receipt_id": receipt_id}
    event_id = record_legacy_recall(
        posts_db_path,
        [int(memory_id)],
        query=query,
        provenance=provenance,
    )
    return {
        "status": "VERIFIED",
        "receipt_id": receipt_id,
        "event_id": event_id[0] if event_id else None,
        "legacy_memory_id": int(memory_id),
    }


def _handoff_contains(handoff_text: str, stored_content: str) -> bool:
    handoff = " ".join(str(handoff_text or "").split())
    stored = " ".join(str(stored_content or "").split())
    if not stored:
        return False
    if stored in handoff:
        return True
    prefix = stored[:80].rstrip("…").strip()
    return len(prefix) >= 40 and prefix in handoff



def build_handoff_fallback(
    posts_db_path: str | None = None,
    *,
    existing_handoff: str = "",
    max_items: int = _HANDOFF_FALLBACK_MAX_ITEMS,
    max_chars: int = _HANDOFF_FALLBACK_MAX_CHARS,
) -> str:
    """Build bounded continuity from explicit committed legacy receipts only."""
    try:
        item_limit = min(_HANDOFF_FALLBACK_MAX_ITEMS, max(0, int(max_items)))
        char_budget = min(_HANDOFF_FALLBACK_MAX_CHARS, max(0, int(max_chars)))
    except (TypeError, ValueError):
        return ""
    if item_limit <= 0 or char_budget <= 0:
        return ""

    posts_path = os.environ.get(
        "MEMORY_RUNTIME_POSTS_DB_PATH", posts_db_path or "/opt/frontend/memories.db"
    )
    receipt_path = receipt_db_path(posts_path)
    if not Path(receipt_path).is_file() or not Path(posts_path).is_file():
        return ""

    try:
        receipt_conn = sqlite3.connect(receipt_path, timeout=10)
        receipt_conn.row_factory = sqlite3.Row
        try:
            committed = receipt_conn.execute(
                """
                SELECT source, legacy_memory_id
                FROM memory_runtime_receipts
                WHERE status='COMMITTED'
                  AND legacy_memory_id IS NOT NULL
                  AND source IN (?, ?)
                ORDER BY updated_at DESC, receipt_id DESC
                LIMIT 50
                """,
                _HANDOFF_FALLBACK_SOURCES,
            ).fetchall()
        finally:
            receipt_conn.close()

        legacy_conn = sqlite3.connect(posts_path, timeout=10)
        try:
            fallback_rows: list[str] = []
            seen: set[str] = set()
            for row in committed:
                if len(fallback_rows) >= item_limit:
                    break
                found = legacy_conn.execute(
                    "SELECT content, resolved FROM posts WHERE id=?",
                    (int(row["legacy_memory_id"]),),
                ).fetchone()
                if not found or int(found[1] or 0) != 0:
                    continue
                normalized = " ".join(str(found[0] or "").split())
                if not normalized:
                    continue
                identity = normalized.casefold()
                if identity in seen or _handoff_contains(existing_handoff, normalized):
                    continue
                seen.add(identity)
                label = _HANDOFF_SOURCE_LABELS.get(
                    str(row["source"]), "【legacy receipt】"
                )
                line = f"{label}: {normalized}"
                remaining = char_budget - (
                    sum(len(item) for item in fallback_rows)
                    + len(fallback_rows)
                )
                if remaining <= 0:
                    break
                if len(line) > remaining:
                    if remaining <= 1:
                        break
                    line = line[: remaining - 1].rstrip() + "…"
                fallback_rows.append(line)
            return "\n".join(fallback_rows)
        finally:
            legacy_conn.close()
    except Exception as exc:
        _LOG.warning(
            "memory_runtime_handoff_fallback_failed error_type=%s",
            type(exc).__name__,
        )
        return ""


def record_handoff_observation(
    handoff_text: str,
    *,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = str(handoff_text or "").strip()
    if not text:
        return {"status": "EMPTY"}
    posts_path = os.environ.get(
        "MEMORY_RUNTIME_POSTS_DB_PATH", "/opt/frontend/memories.db"
    )
    prov = normalize_provenance("new_window_handoff", provenance)
    key = deterministic_idempotency_key(
        text, source=prov["source"], provenance=prov
    )
    row, _ = _reserve(
        posts_db_path=posts_path,
        idempotency_key=key,
        source=prov["source"],
        provenance=prov,
        content_hash=content_sha256(text),
        status="OBSERVED",
    )
    handoff_hash = content_sha256(text)
    event_id = _event(
        posts_path,
        row["receipt_id"],
        "new_window_handoff_observed",
        "OBSERVED",
        metadata={"handoff_sha256": handoff_hash},
    )

    matched_receipt_ids: list[str] = []
    recalled_event_ids: list[str] = []
    receipt_conn = _connect(receipt_db_path(posts_path))
    try:
        committed = receipt_conn.execute(
            """
            SELECT receipt_id, legacy_memory_id
            FROM memory_runtime_receipts
            WHERE status='COMMITTED'
              AND legacy_memory_id IS NOT NULL
              AND source IN (?, ?)
            ORDER BY updated_at DESC, receipt_id DESC
            LIMIT 50
            """,
            _HANDOFF_FALLBACK_SOURCES,
        ).fetchall()
    finally:
        receipt_conn.close()

    legacy_conn = sqlite3.connect(posts_path, timeout=10)
    try:
        for committed_row in committed:
            memory_id = int(committed_row["legacy_memory_id"])
            found = legacy_conn.execute(
                "SELECT content FROM posts WHERE id=?",
                (memory_id,),
            ).fetchone()
            if not found or not _handoff_contains(text, found[0]):
                continue
            recalled_event_ids.append(
                _event(
                    posts_path,
                    str(committed_row["receipt_id"]),
                    "new_window_handoff_recalled",
                    "VERIFIED",
                    legacy_memory_id=memory_id,
                    metadata={
                        "handoff_sha256": handoff_hash,
                        "window_id": prov.get("window_id", ""),
                    },
                )
            )
            matched_receipt_ids.append(str(committed_row["receipt_id"]))
    except sqlite3.Error:
        _LOG.warning("memory_runtime_handoff_match_failed", exc_info=True)
    finally:
        legacy_conn.close()

    return {
        "status": "OBSERVED",
        "receipt_id": row["receipt_id"],
        "event_id": event_id,
        "content_sha256": handoff_hash,
        "matched_receipt_ids": matched_receipt_ids,
        "recalled_event_ids": recalled_event_ids,
    }


def verify_handoff_recall(
    handoff_text: str,
    *,
    memory_receipt_id: str,
    expected_content: str,
    provenance: dict[str, Any] | None = None,
    posts_db_path: str | None = None,
) -> dict[str, Any]:
    text = str(handoff_text or "")
    row = _receipt(posts_db_path or "/opt/frontend/memories.db", memory_receipt_id)
    if row is None or row.get("status") != "COMMITTED":
        return {"status": "UNVERIFIED", "receipt_id": memory_receipt_id}
    if not expected_content or expected_content not in text:
        event_id = _event(
            posts_db_path or "/opt/frontend/memories.db",
            memory_receipt_id,
            "new_window_handoff_recalled",
            "UNVERIFIED",
            metadata={"handoff_sha256": content_sha256(text)},
        )
        return {
            "status": "UNVERIFIED",
            "receipt_id": memory_receipt_id,
            "event_id": event_id,
        }
    event_id = _event(
        posts_db_path or "/opt/frontend/memories.db",
        memory_receipt_id,
        "new_window_handoff_recalled",
        "VERIFIED",
        metadata={
            "handoff_sha256": content_sha256(text),
            "window_id": normalize_provenance(
                "new_window_handoff", provenance
            ).get("window_id", ""),
        },
    )
    return {
        "status": "VERIFIED",
        "receipt_id": memory_receipt_id,
        "event_id": event_id,
    }


def derive_auto_candidate(
    user_message: str,
    assistant_message: str,
) -> tuple[str | None, str]:
    user_text = str(user_message or "").strip()
    assistant_text = str(assistant_message or "").strip()
    combined = f"用户：{user_text}\n助手：{assistant_text}".strip()
    if not user_text or not assistant_text:
        return None, "missing_turn_text"
    if len(combined) < _MIN_CANDIDATE_CHARS:
        return None, "too_short"
    if len(combined) > _MAX_CANDIDATE_CHARS:
        combined = combined[:_MAX_CANDIDATE_CHARS].rstrip() + "…"
    if _SENSITIVE_RE.search(combined):
        return None, "sensitive_pattern"
    if not _AUTO_INTENT_RE.search(user_text):
        return None, "no_safe_intent"
    return combined, "safe_intent"


def observe_auto_candidate(
    posts_db_path: str,
    *,
    user_message: str,
    assistant_message: str,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate, reason = derive_auto_candidate(user_message, assistant_message)
    if candidate is None:
        return {"status": "REJECTED", "reason": reason}
    raw_mode = os.environ.get(
        "MEMORY_RUNTIME_AUTO_CANDIDATE_MODE", "shadow"
    ).strip().lower()
    mode = raw_mode if raw_mode in {"off", "shadow", "write"} else "shadow"
    enabled = os.environ.get(
        "MEMORY_RUNTIME_AUTO_CANDIDATE_ENABLED", ""
    ).strip()
    if mode == "off" or enabled.lower() in {"0", "false", "no", "off"}:
        return {"status": "DISABLED", "reason": reason}
    if mode == "write" and enabled != "1":
        mode = "shadow"

    prov = normalize_provenance("chat_auto_candidate", provenance)
    if mode == "shadow":
        key = deterministic_idempotency_key(
            candidate, source=prov["source"], provenance=prov
        )
        row, _ = _reserve(
            posts_db_path=posts_db_path,
            idempotency_key=key,
            source=prov["source"],
            provenance=prov,
            content_hash=content_sha256(candidate),
            status="SHADOW",
        )
        event_id = _event(
            posts_db_path,
            row["receipt_id"],
            "candidate_formed",
            "SHADOW",
            metadata={"rule": reason},
        )
        return {
            "status": "SHADOW",
            "reason": reason,
            "receipt_id": row["receipt_id"],
            "event_id": event_id,
            "content_sha256": content_sha256(candidate),
        }
    return persist_memory(
        posts_db_path,
        content=candidate,
        source=prov["source"],
        provenance=prov,
        type="MEMORY",
        author="fyodor",
        layer="long-term",
        tags="auto-candidate",
        importance=3,
        pinned=0,
        write_ombre=True,
    )
