"""Administrative candidate records for complete external-tool discoveries.

This module deliberately owns only candidate metadata and immutable raw
snapshots.  It has no default database, network, MCP client, approval API, or
runtime/model integration.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from .external_server_registry import (
    ExternalServerRegistry,
    REGISTRATION_STATE,
    REVIEW_REQUIRED_STATE as SERVER_REVIEW_REQUIRED,
    REVOKED_STATE,
    UnknownServerError,
)

PRESENT = "PRESENT"
MISSING = "MISSING"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
APPROVED = "APPROVED"
DISCOVERY_SUCCESS = "SUCCESS"
RAW_BOUNDARY = "SDK_VISIBLE_RAW"
SECURITY_FINGERPRINT_SCOPE = "ENTIRE_SDK_VISIBLE_RAW_TOOL_RECORD"
MAX_TOOL_NAME_BYTES = 256
MAX_TOOL_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_CATALOG_TOOLS = 1000


class ToolRegistryError(Exception):
    code = "TOOL_REGISTRY_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ToolCatalogValidationError(ToolRegistryError):
    code = "INVALID_TOOL_CATALOG"


class ToolRegistryRejectedError(ToolRegistryError):
    code = "TOOL_INGEST_REJECTED"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used for raw snapshots and hashes."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ToolCatalogValidationError(
            "tool record is not safely JSON serializable",
            code="INVALID_TOOL_RECORD_JSON",
        ) from exc


def fingerprint_raw_tool(tool: Mapping[str, Any]) -> str:
    if not isinstance(tool, Mapping):
        raise ToolCatalogValidationError("tool record must be an object")
    payload = canonical_json(tool).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_server_id(value: Any) -> str:
    if not isinstance(value, str) or not value or ":" in value:
        raise ToolRegistryRejectedError(
            "discovery result has an invalid server identity", code="INVALID_SERVER_ID"
        )
    return value


def _validate_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ToolCatalogValidationError(
            "tool name must be a non-empty exact string", code="INVALID_TOOL_NAME"
        )
    if len(value.encode("utf-8")) > MAX_TOOL_NAME_BYTES or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ToolCatalogValidationError("tool name exceeds safe limits", code="INVALID_TOOL_NAME")
    return value


class ExternalToolCandidateRegistry:
    """SQLite owner for stable external-tool candidate identity and snapshots."""

    def __init__(
        self, connection: sqlite3.Connection, *, server_registry: ExternalServerRegistry
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be an sqlite3.Connection")
        if not isinstance(server_registry, ExternalServerRegistry):
            raise TypeError("server_registry must be an ExternalServerRegistry")
        if getattr(server_registry, "_connection", None) is not connection:
            raise ToolRegistryRejectedError(
                "candidate and server registries must share the exact authoritative connection",
                code="SERVER_AUTHORITY_MISMATCH",
            )
        self._connection = connection
        self._server_registry = server_registry
        self.initialize()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_tool_candidate_registry (
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                control_id TEXT NOT NULL UNIQUE,
                presence_state TEXT NOT NULL CHECK (
                    presence_state IN ('PRESENT', 'MISSING')
                ),
                review_state TEXT NOT NULL CHECK (
                    review_state IN ('REVIEW_REQUIRED', 'APPROVED')
                ),
                current_fingerprint TEXT NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                model_visible INTEGER NOT NULL DEFAULT 0 CHECK (model_visible = 0),
                execution_allowed INTEGER NOT NULL DEFAULT 0 CHECK (execution_allowed = 0),
                PRIMARY KEY (server_id, tool_name)
            );
            CREATE TABLE IF NOT EXISTS external_tool_raw_snapshots (
                snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                raw_snapshot_json TEXT NOT NULL,
                source_registry_revision INTEGER NOT NULL CHECK (source_registry_revision >= 1),
                first_observed_at TEXT NOT NULL,
                UNIQUE (server_id, tool_name, fingerprint)
            );
            """
        )
        self._connection.commit()

    def ingest(self, discovery_result: Mapping[str, Any]) -> dict[str, Any]:
        """Atomically reconcile one complete, current discovery result."""
        validated = self._validate_discovery(discovery_result)
        server_id, source_revision, tools = validated
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._gate_current_server(server_id, source_revision)
            now = _timestamp()
            existing = {
                row[0]: row
                for row in self._connection.execute(
                    "SELECT tool_name, control_id, presence_state, review_state, "
                    "current_fingerprint, first_seen, last_seen, revision "
                    "FROM external_tool_candidate_registry WHERE server_id=?",
                    (server_id,),
                )
            }
            present_names = set()
            for name, raw_json, fingerprint in tools:
                present_names.add(name)
                old = existing.get(name)
                if old is None:
                    self._connection.execute(
                        "INSERT INTO external_tool_candidate_registry ("
                        "server_id, tool_name, control_id, presence_state, review_state, "
                        "current_fingerprint, first_seen, last_seen, updated_at, revision, "
                        "model_visible, execution_allowed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, 0)",
                        (
                            server_id, name, f"ext:{server_id}:{name}", PRESENT,
                            REVIEW_REQUIRED, fingerprint, now, now, now,
                        ),
                    )
                else:
                    _, _, old_presence, old_review, old_fingerprint, first_seen, _, old_revision = old
                    review = (
                        REVIEW_REQUIRED
                        if old_presence == MISSING or old_fingerprint != fingerprint
                        else old_review
                    )
                    self._connection.execute(
                        "UPDATE external_tool_candidate_registry SET presence_state=?, "
                        "review_state=?, current_fingerprint=?, last_seen=?, updated_at=?, "
                        "revision=?, model_visible=0, execution_allowed=0 "
                        "WHERE server_id=? AND tool_name=?",
                        (
                            PRESENT, review, fingerprint, now, now, old_revision + 1,
                            server_id, name,
                        ),
                    )
                self._connection.execute(
                    "INSERT INTO external_tool_raw_snapshots ("
                    "server_id, tool_name, fingerprint, raw_snapshot_json, "
                    "source_registry_revision, first_observed_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(server_id, tool_name, fingerprint) DO NOTHING",
                    (server_id, name, fingerprint, raw_json, source_revision, now),
                )

            for name, row in existing.items():
                if name not in present_names:
                    old_revision = row[7]
                    self._connection.execute(
                        "UPDATE external_tool_candidate_registry SET presence_state=?, "
                        "review_state=?, updated_at=?, revision=?, model_visible=0, "
                        "execution_allowed=0 WHERE server_id=? AND tool_name=?",
                        (MISSING, REVIEW_REQUIRED, now, old_revision + 1, server_id, name),
                    )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

        rows = self._connection.execute(
            "SELECT presence_state FROM external_tool_candidate_registry WHERE server_id=?",
            (server_id,),
        ).fetchall()
        return {
            "server_id": server_id,
            "registry_revision": source_revision,
            "present_tool_count": sum(row[0] == PRESENT for row in rows),
            "missing_tool_count": sum(row[0] == MISSING for row in rows),
        }

    def get_candidate(self, server_id: str, tool_name: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute(
            "SELECT server_id, tool_name, control_id, presence_state, review_state, "
            "current_fingerprint, first_seen, last_seen, updated_at, revision, "
            "model_visible, execution_allowed FROM external_tool_candidate_registry "
            "WHERE server_id=? AND tool_name=?",
            (server_id, tool_name),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "server_id", "tool_name", "control_id", "presence_state", "review_state",
            "current_fingerprint", "first_seen", "last_seen", "updated_at", "revision",
            "model_visible", "execution_allowed",
        )
        return dict(zip(keys, row))

    def list_candidates(self, server_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            self.get_candidate(server_id, row[0])
            for row in self._connection.execute(
                "SELECT tool_name FROM external_tool_candidate_registry "
                "WHERE server_id=? ORDER BY tool_name",
                (server_id,),
            ).fetchall()
        )

    def list_snapshots(self, server_id: str, tool_name: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            "SELECT fingerprint, raw_snapshot_json, source_registry_revision, first_observed_at "
            "FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=? "
            "ORDER BY snapshot_id",
            (server_id, tool_name),
        ).fetchall()
        return tuple(
            {
                "fingerprint": row[0],
                "raw_snapshot_json": row[1],
                "source_registry_revision": row[2],
                "first_observed_at": row[3],
            }
            for row in rows
        )

    def _validate_discovery(
        self, result: Mapping[str, Any]
    ) -> tuple[str, int, list[tuple[str, str, str]]]:
        if not isinstance(result, Mapping):
            raise ToolRegistryRejectedError("discovery result must be an object", code="INVALID_DISCOVERY_RESULT")
        if result.get("status") != DISCOVERY_SUCCESS or result.get("catalog_complete") is not True:
            raise ToolRegistryRejectedError("only complete successful discovery is ingestible", code="INCOMPLETE_DISCOVERY")
        diagnostics = result.get("diagnostics")
        if not isinstance(diagnostics, Mapping) or diagnostics.get("registry_changed_during_attempt") is not False:
            raise ToolRegistryRejectedError("discovery attempt is stale or lacks a change fence", code="STALE_DISCOVERY")
        if result.get("tool_record_boundary") != RAW_BOUNDARY:
            raise ToolRegistryRejectedError("tool record boundary is unsupported", code="UNSUPPORTED_RECORD_BOUNDARY")
        server_id = _require_server_id(result.get("server_id"))
        source_revision = result.get("registry_revision")
        if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
            raise ToolRegistryRejectedError("registry revision is invalid", code="INVALID_REGISTRY_REVISION")
        if result.get("model_visible", False) is not False or result.get("execution_allowed", False) is not False:
            raise ToolRegistryRejectedError("discovery result cannot enable a candidate", code="UNSAFE_DISCOVERY_RESULT")
        for field in ("control_id", "review_state", "approved", "enabled"):
            if field in result:
                raise ToolRegistryRejectedError("caller-controlled approval fields are forbidden", code="UNSUPPORTED_DISCOVERY_FIELD")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list) or len(raw_tools) > MAX_CATALOG_TOOLS:
            raise ToolCatalogValidationError("tool catalog exceeds its count bound", code="CATALOG_TOO_LARGE")
        tools: list[tuple[str, str, str]] = []
        names: set[str] = set()
        total_bytes = 0
        for tool in raw_tools:
            if not isinstance(tool, Mapping):
                raise ToolCatalogValidationError("every tool must be an object", code="INVALID_TOOL_RECORD")
            name = _validate_tool_name(tool.get("name"))
            if name in names:
                raise ToolCatalogValidationError("duplicate exact tool name", code="DUPLICATE_TOOL_NAME")
            names.add(name)
            raw_json = canonical_json(tool)
            raw_bytes = len(raw_json.encode("utf-8"))
            if raw_bytes > MAX_TOOL_SNAPSHOT_BYTES:
                raise ToolCatalogValidationError("tool snapshot exceeds its byte bound", code="SNAPSHOT_TOO_LARGE")
            total_bytes += raw_bytes
            if total_bytes > MAX_CATALOG_BYTES:
                raise ToolCatalogValidationError("catalog exceeds its byte bound", code="CATALOG_TOO_LARGE")
            tools.append((name, raw_json, hashlib.sha256(raw_json.encode("utf-8")).hexdigest()))
        return server_id, source_revision, tools

    def _gate_current_server(self, server_id: str, source_revision: int) -> None:
        try:
            record = self._server_registry.get(server_id)
        except UnknownServerError as exc:
            raise ToolRegistryRejectedError(
                "server identity is unknown", code="UNKNOWN_SERVER"
            ) from exc
        if record.lifecycle_state == REVOKED_STATE:
            raise ToolRegistryRejectedError("revoked server cannot ingest tools", code="REVOKED_SERVER")
        if record.lifecycle_state not in (REGISTRATION_STATE, SERVER_REVIEW_REQUIRED):
            raise ToolRegistryRejectedError("server lifecycle is not ingestible", code="INVALID_SERVER_STATE")
        if record.revision != source_revision:
            raise ToolRegistryRejectedError("server revision changed before ingest", code="REVISION_MISMATCH")


__all__ = [
    "APPROVED",
    "DISCOVERY_SUCCESS",
    "ExternalToolCandidateRegistry",
    "MAX_CATALOG_BYTES",
    "MAX_CATALOG_TOOLS",
    "MAX_TOOL_NAME_BYTES",
    "MAX_TOOL_SNAPSHOT_BYTES",
    "MISSING",
    "PRESENT",
    "RAW_BOUNDARY",
    "REVIEW_REQUIRED",
    "SECURITY_FINGERPRINT_SCOPE",
    "ToolCatalogValidationError",
    "ToolRegistryError",
    "ToolRegistryRejectedError",
    "canonical_json",
    "fingerprint_raw_tool",
]
