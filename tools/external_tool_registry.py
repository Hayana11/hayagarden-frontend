"""SQLite owner for external MCP tool presence and immutable raw snapshots."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, Optional

from .external_server_registry import CONNECTED_STATE, DISCONNECTED_STATE, ExternalServerRegistry, REVOKED_STATE, UnknownServerError

PRESENT = "PRESENT"
MISSING = "MISSING"
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
        if code is not None: self.code = code


class ToolCatalogValidationError(ToolRegistryError): code = "INVALID_TOOL_CATALOG"
class ToolRegistryRejectedError(ToolRegistryError): code = "TOOL_INGEST_REJECTED"


def _timestamp() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def canonical_json(value: Any) -> str:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc: raise ToolCatalogValidationError("tool record is not safely JSON serializable", code="INVALID_TOOL_RECORD_JSON") from exc

def fingerprint_raw_tool(tool: Mapping[str, Any]) -> str:
    if not isinstance(tool, Mapping): raise ToolCatalogValidationError("tool record must be an object")
    return hashlib.sha256(canonical_json(tool).encode("utf-8")).hexdigest()

def _server_id(value: Any) -> str:
    if not isinstance(value, str) or not value or ":" in value: raise ToolRegistryRejectedError("invalid server identity", code="INVALID_SERVER_ID")
    return value

def _tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value.encode("utf-8")) > MAX_TOOL_NAME_BYTES or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ToolCatalogValidationError("tool name is invalid", code="INVALID_TOOL_NAME")
    return value

def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1: raise ToolRegistryRejectedError("source registry revision is invalid", code="INVALID_SOURCE_REGISTRY_REVISION")
    return value


class ExternalToolCandidateRegistry:
    def __init__(self, connection: sqlite3.Connection, *, server_registry: ExternalServerRegistry) -> None:
        if not isinstance(connection, sqlite3.Connection) or not isinstance(server_registry, ExternalServerRegistry): raise TypeError("invalid candidate registry owners")
        if getattr(server_registry, "_connection", None) is not connection: raise ToolRegistryRejectedError("candidate and server registries must share the exact connection", code="SERVER_AUTHORITY_MISMATCH")
        self._connection = connection; self._server_registry = server_registry; self.initialize()

    def initialize(self) -> None:
        tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "external_tool_candidate_registry" in tables:
            columns = {r[1] for r in self._connection.execute("PRAGMA table_info(external_tool_candidate_registry)")}
            if "review" + "_state" in columns or "model_" + "visible" in columns or "execution_" + "allowed" in columns: self._migrate_legacy_schema()
        else: self._create_schema()
        for object_name in (
            "external_tool_" + "review" + "_audit_no_update",
            "external_tool_" + "review" + "_audit_no_delete",
        ):
            self._connection.execute("DROP TRIGGER IF EXISTS " + object_name)
        for table_name in (
            "external_tool_" + "approval" + "_baselines",
            "external_tool_" + "review" + "_audit",
            "external_tool_side_effect_" + "baselines",
            "external_tool_side_effect_" + "audit",
        ):
            self._connection.execute("DROP TABLE IF EXISTS " + table_name)
        self._connection.commit()

    def _create_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS external_tool_candidate_registry (
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                control_id TEXT NOT NULL UNIQUE,
                presence_state TEXT NOT NULL CHECK (presence_state IN ('PRESENT','MISSING')),
                current_fingerprint TEXT NOT NULL,
                current_source_registry_revision INTEGER CHECK (current_source_registry_revision >= 1),
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
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
        """)

    def _migrate_legacy_schema(self) -> None:
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(external_tool_candidate_registry)")}
        source_column = "current_source_registry_revision" if "current_source_registry_revision" in columns else "NULL"
        self._connection.execute("ALTER TABLE external_tool_candidate_registry RENAME TO external_tool_candidate_registry_legacy")
        self._create_schema()
        rows = self._connection.execute("SELECT server_id, tool_name, control_id, presence_state, current_fingerprint, " + source_column + ", first_seen, last_seen, updated_at, revision FROM external_tool_candidate_registry_legacy").fetchall()
        self._connection.executemany("INSERT INTO external_tool_candidate_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self._connection.execute("DROP TABLE external_tool_candidate_registry_legacy")

    def ingest(self, discovery_result: Mapping[str, Any]) -> dict[str, Any]:
        server_id, source_revision, tools = self._validate_discovery(discovery_result)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._gate_current_server(server_id, source_revision)
            now = _timestamp()
            existing = {row[0]: row for row in self._connection.execute("SELECT tool_name, control_id, presence_state, current_fingerprint, current_source_registry_revision, first_seen, last_seen, updated_at, revision FROM external_tool_candidate_registry WHERE server_id=?", (server_id,))}
            present = set()
            for name, raw_json, fingerprint in tools:
                present.add(name); old = existing.get(name)
                if old is None:
                    self._connection.execute("INSERT INTO external_tool_candidate_registry (server_id, tool_name, control_id, presence_state, current_fingerprint, current_source_registry_revision, first_seen, last_seen, updated_at, revision) VALUES (?, ?, ?, 'PRESENT', ?, ?, ?, ?, ?, 1)", (server_id, name, f"ext:{server_id}:{name}", fingerprint, source_revision, now, now, now))
                else:
                    self._verify_raw_snapshot(server_id, name, old[3])
                    self._connection.execute("UPDATE external_tool_candidate_registry SET presence_state='PRESENT', current_fingerprint=?, current_source_registry_revision=?, last_seen=?, updated_at=?, revision=revision+1 WHERE server_id=? AND tool_name=?", (fingerprint, source_revision, now, now, server_id, name))
                self._connection.execute("INSERT INTO external_tool_raw_snapshots (server_id, tool_name, fingerprint, raw_snapshot_json, source_registry_revision, first_observed_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(server_id, tool_name, fingerprint) DO NOTHING", (server_id, name, fingerprint, raw_json, source_revision, now))
                self._verify_raw_snapshot(server_id, name, fingerprint)
            for name, old in existing.items():
                if name not in present:
                    self._connection.execute("UPDATE external_tool_candidate_registry SET presence_state='MISSING', current_source_registry_revision=?, updated_at=?, revision=revision+1 WHERE server_id=? AND tool_name=?", (source_revision, now, server_id, name))
            self._connection.commit()
        except Exception: self._connection.rollback(); raise
        rows = self._connection.execute("SELECT presence_state FROM external_tool_candidate_registry WHERE server_id=?", (server_id,)).fetchall()
        return {"server_id": server_id, "registry_revision": source_revision, "present_tool_count": sum(r[0] == PRESENT for r in rows), "missing_tool_count": sum(r[0] == MISSING for r in rows)}

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
        server_id = _server_id(result.get("server_id"))
        source_revision = result.get("registry_revision")
        if isinstance(source_revision, bool) or not isinstance(source_revision, int) or source_revision < 1:
            raise ToolRegistryRejectedError("registry revision is invalid", code="INVALID_REGISTRY_REVISION")
        if "control_id" in result:
            raise ToolRegistryRejectedError("caller-controlled tool identity is forbidden", code="UNSUPPORTED_DISCOVERY_FIELD")
        raw_tools = result.get("tools")
        if not isinstance(raw_tools, list) or len(raw_tools) > MAX_CATALOG_TOOLS:
            raise ToolCatalogValidationError("tool catalog exceeds its count bound", code="CATALOG_TOO_LARGE")
        tools: list[tuple[str, str, str]] = []
        names: set[str] = set()
        total_bytes = 0
        for tool in raw_tools:
            if not isinstance(tool, Mapping):
                raise ToolCatalogValidationError("every tool must be an object", code="INVALID_TOOL_RECORD")
            name = _tool_name(tool.get("name"))
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

    def _verify_raw_snapshot(self, server_id: str, tool_name: str, fingerprint: str) -> None:
        row = self._connection.execute(
            "SELECT raw_snapshot_json FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=? AND fingerprint=?",
            (server_id, tool_name, fingerprint),
        ).fetchone()
        if row is None:
            raise ToolRegistryRejectedError("raw snapshot does not exist", code="SNAPSHOT_MISSING")
        if not isinstance(row[0], str) or hashlib.sha256(row[0].encode("utf-8")).hexdigest() != fingerprint:
            raise ToolRegistryRejectedError("raw snapshot hash does not match candidate", code="SNAPSHOT_MISMATCH")

    def _gate_current_server(self, server_id: str, source_revision: int) -> None:
        try:
            record = self._server_registry.get(server_id)
        except UnknownServerError as exc:
            raise ToolRegistryRejectedError(
                "server identity is unknown", code="UNKNOWN_SERVER"
            ) from exc
        if record.lifecycle_state == REVOKED_STATE:
            raise ToolRegistryRejectedError("revoked server cannot ingest tools", code="REVOKED_SERVER")
        if record.lifecycle_state not in (CONNECTED_STATE, DISCONNECTED_STATE):
            raise ToolRegistryRejectedError("server lifecycle is not ingestible", code="INVALID_SERVER_STATE")
        if record.revision != source_revision:
            raise ToolRegistryRejectedError("server revision changed before ingest", code="REVISION_MISMATCH")

    def get_candidate(self, server_id: str, tool_name: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute("SELECT server_id, tool_name, control_id, presence_state, current_fingerprint, current_source_registry_revision, first_seen, last_seen, updated_at, revision FROM external_tool_candidate_registry WHERE server_id=? AND tool_name=?", (server_id, tool_name)).fetchone()
        return dict(zip(("server_id", "tool_name", "control_id", "presence_state", "current_fingerprint", "current_source_registry_revision", "first_seen", "last_seen", "updated_at", "revision"), row)) if row else None

    def get_by_control_id(self, control_id: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute("SELECT server_id, tool_name FROM external_tool_candidate_registry WHERE control_id=?", (control_id,)).fetchone()
        return self.get_candidate(*row) if row else None

    def list_candidates(self, server_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(self.get_candidate(server_id, row[0]) for row in self._connection.execute("SELECT tool_name FROM external_tool_candidate_registry WHERE server_id=? ORDER BY tool_name", (server_id,)))

    def list_snapshots(self, server_id: str, tool_name: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute("SELECT fingerprint, raw_snapshot_json, source_registry_revision, first_observed_at FROM external_tool_raw_snapshots WHERE server_id=? AND tool_name=? ORDER BY snapshot_id", (server_id, tool_name)).fetchall()
        return tuple(dict(zip(("fingerprint", "raw_snapshot_json", "source_registry_revision", "first_observed_at"), row)) for row in rows)


__all__ = ["DISCOVERY_SUCCESS", "ExternalToolCandidateRegistry", "MISSING", "PRESENT", "RAW_BOUNDARY", "SECURITY_FINGERPRINT_SCOPE", "ToolCatalogValidationError", "ToolRegistryError", "ToolRegistryRejectedError", "canonical_json", "fingerprint_raw_tool"]
