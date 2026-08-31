"""Durable external MCP invocation with technical pre-call validation only."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Optional

from .external_mcp_auth_binding import ExternalMcpAuthBindingRegistry
from .external_server_registry import CONNECTED_STATE, ExternalServerRegistry
from .external_tool_registry import ExternalToolCandidateRegistry, PRESENT, canonical_json

PRE_CALL = "PRE_CALL"
STARTED = "STARTED"
SUCCEEDED = "SUCCEEDED"
TOOL_ERROR = "TOOL_ERROR"
FAILED_PRE_CALL = "FAILED_PRE_CALL"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
TERMINAL_STATUSES = frozenset({SUCCEEDED, TOOL_ERROR, FAILED_PRE_CALL, OUTCOME_UNKNOWN})
MAX_TOOL_INPUT_BYTES = 256 * 1024
_OUTCOME_STATUS = {"SUCCESS": SUCCEEDED, "TOOL_ERROR": TOOL_ERROR, "NOT_INVOKED": FAILED_PRE_CALL, "OUTCOME_UNKNOWN": OUTCOME_UNKNOWN}


class ExternalInvocationError(ValueError):
    code = "EXTERNAL_INVOCATION_ERROR"
    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None: self.code = code


class ExternalInvocationInitializationError(ExternalInvocationError): code = "SERVER_AUTHORITY_MISMATCH"

def _now() -> str: return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _snapshot_tool_input(value: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping): raise ExternalInvocationError("tool_input must be an object", code="INVALID_TOOL_INPUT")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        snapshot = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc: raise ExternalInvocationError("tool_input is not JSON-safe", code="INVALID_TOOL_INPUT") from exc
    if not isinstance(snapshot, dict): raise ExternalInvocationError("tool_input must freeze to an object", code="INVALID_TOOL_INPUT")
    if len(encoded) > MAX_TOOL_INPUT_BYTES: raise ExternalInvocationError("tool_input exceeds the byte limit", code="TOOL_INPUT_TOO_LARGE")
    return snapshot, encoded

def _freeze(value: Any) -> Any:
    if isinstance(value, dict): return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list): return tuple(_freeze(v) for v in value)
    return value

def build_external_action_id(control_id: str, fingerprint: str, source_registry_revision: int, tool_input: Mapping[str, Any]) -> str:
    payload = {"control_id": control_id, "fingerprint": fingerprint, "source_registry_revision": source_registry_revision, "tool_input": json.loads(canonical_json(tool_input))}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

def _safe_runner_reason_code(value: Any) -> str:
    error = value.get("error") if isinstance(value, Mapping) else None; code = error.get("code") if isinstance(error, Mapping) else None
    if type(code) is str and code and code == code.strip() and code.isascii() and len(code) <= 64 and all(c.isupper() or c.isdigit() or c == "_" for c in code): return code
    return "NOT_INVOKED"


class ExternalMcpInvocation:
    def __init__(self, connection: sqlite3.Connection, *, server_registry: ExternalServerRegistry, candidate_registry: ExternalToolCandidateRegistry, auth_binding_registry: ExternalMcpAuthBindingRegistry, id_factory: Optional[Callable[[], str]] = None) -> None:
        owners = (server_registry, candidate_registry, auth_binding_registry)
        if not isinstance(connection, sqlite3.Connection) or not isinstance(candidate_registry, ExternalToolCandidateRegistry) or not isinstance(server_registry, ExternalServerRegistry) or not isinstance(auth_binding_registry, ExternalMcpAuthBindingRegistry) or any(getattr(o, "_connection", None) is not connection for o in owners) or getattr(candidate_registry, "_server_registry", None) is not server_registry or getattr(auth_binding_registry, "_server_registry", None) is not server_registry:
            raise ExternalInvocationInitializationError("owners must share one connection")
        self._connection = connection; self._server_registry = server_registry; self._candidate_registry = candidate_registry; self._auth_binding_registry = auth_binding_registry; self._id_factory = id_factory or (lambda: secrets.token_hex(16)); self.initialize()

    def initialize(self) -> None:
        tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "external_tool_invocation_attempts" in tables:
            cols = {r[1] for r in self._connection.execute("PRAGMA table_info(external_tool_invocation_attempts)")}
            if "side_effect_" + "class" in cols: self._migrate_attempts()
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS external_tool_invocation_attempts (
                attempt_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, control_id TEXT NOT NULL,
                server_id TEXT NOT NULL, tool_name TEXT NOT NULL, external_action_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, source_registry_revision INTEGER NOT NULL,
                tool_input_sha256 TEXT NOT NULL, tool_input_byte_length INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PRE_CALL','STARTED','SUCCEEDED','TOOL_ERROR','FAILED_PRE_CALL','OUTCOME_UNKNOWN')),
                reason_code TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                UNIQUE (turn_id, external_action_id)
            );
            CREATE TABLE IF NOT EXISTS external_tool_invocation_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL REFERENCES external_tool_invocation_attempts(attempt_id),
                turn_id TEXT NOT NULL, control_id TEXT NOT NULL, server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, external_action_id TEXT NOT NULL, status TEXT NOT NULL,
                reason_code TEXT NOT NULL, event_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS external_tool_invocation_audit_no_update BEFORE UPDATE ON external_tool_invocation_audit BEGIN SELECT RAISE(ABORT, 'external invocation audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS external_tool_invocation_audit_no_delete BEFORE DELETE ON external_tool_invocation_audit BEGIN SELECT RAISE(ABORT, 'external invocation audit is append-only'); END;
        """)
        self._connection.commit()

    def _migrate_attempts(self) -> None:
        tables = {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "external_tool_invocation_audit" in tables: self._connection.execute("ALTER TABLE external_tool_invocation_audit RENAME TO external_tool_invocation_audit_legacy")
        self._connection.execute("ALTER TABLE external_tool_invocation_attempts RENAME TO external_tool_invocation_attempts_legacy")
        self._connection.executescript("""
            CREATE TABLE external_tool_invocation_attempts (
                attempt_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, control_id TEXT NOT NULL, server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, external_action_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                source_registry_revision INTEGER NOT NULL, tool_input_sha256 TEXT NOT NULL,
                tool_input_byte_length INTEGER NOT NULL, status TEXT NOT NULL,
                reason_code TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                UNIQUE (turn_id, external_action_id)
            );
            CREATE TABLE external_tool_invocation_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL REFERENCES external_tool_invocation_attempts(attempt_id), turn_id TEXT NOT NULL,
                control_id TEXT NOT NULL, server_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                external_action_id TEXT NOT NULL, status TEXT NOT NULL, reason_code TEXT NOT NULL, event_at TEXT NOT NULL
            );
        """)
        self._connection.execute("INSERT INTO external_tool_invocation_attempts (attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,fingerprint,source_registry_revision,tool_input_sha256,tool_input_byte_length,status,reason_code,created_at,started_at,completed_at) SELECT attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,fingerprint,source_registry_revision,tool_input_sha256,tool_input_byte_length,status,reason_code,created_at,started_at,completed_at FROM external_tool_invocation_attempts_legacy")
        if "external_tool_invocation_audit_legacy" in {r[0] for r in self._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            self._connection.execute("INSERT INTO external_tool_invocation_audit SELECT * FROM external_tool_invocation_audit_legacy")
            self._connection.execute("DROP TABLE external_tool_invocation_audit_legacy")
        self._connection.execute("DROP TABLE external_tool_invocation_attempts_legacy")

    def invoke(self, control_id: Any, tool_input: Any, turn_lease: Any, *, expected_turn_id: Any, runner: Callable[[Mapping[str, Any]], Any]) -> dict[str, Any]:
        del turn_lease
        try: frozen, encoded = _snapshot_tool_input(tool_input)
        except ExternalInvocationError as exc: return self._result(FAILED_PRE_CALL, reason_code=exc.code)
        if not callable(runner): return self._result(FAILED_PRE_CALL, reason_code="RUNNER_INVALID")
        try:
            decision = self._technical_decision(control_id, frozen, expected_turn_id)
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._existing(decision["turn_id"], decision["external_action_id"])
            if existing is not None: self._connection.commit(); return self._attempt_result(existing, duplicate=True)
            if decision["reason_code"] != "READY":
                attempt_id = self._record_failed_pre_call(decision, encoded); self._connection.commit(); return self._result(FAILED_PRE_CALL, attempt_id=attempt_id, reason_code=decision["reason_code"])
            attempt_id = self._new_id(); self._insert_attempt(attempt_id, decision, hashlib.sha256(encoded).hexdigest(), len(encoded), PRE_CALL, "PRE_CALL"); self._audit(attempt_id, decision, PRE_CALL, "PRE_CALL"); self._transition(attempt_id, decision, STARTED, "RUNNER_STARTED")
            envelope = MappingProxyType({"server_id": decision["server_id"], "tool_name": decision["tool_name"], "endpoint": decision["endpoint"], "transport": decision["transport"], "fingerprint": decision["fingerprint"], "source_registry_revision": decision["source_registry_revision"], "external_action_id": decision["external_action_id"], "auth_scheme": decision["auth_scheme"], "auth_binding_revision": decision["auth_binding_revision"], "secret_ref": decision["secret_ref"], "credential_slot": decision["credential_slot"], "tool_input": _freeze(frozen)})
            self._connection.commit()
        except sqlite3.Error:
            self._connection.rollback(); return self._result(FAILED_PRE_CALL, reason_code="AUTHORITY_WRITE_FAILED")
        try:
            result = runner(envelope); outcome = result.get("status") if isinstance(result, Mapping) else None; status = _OUTCOME_STATUS.get(outcome, OUTCOME_UNKNOWN); reason = _safe_runner_reason_code(result) if outcome == "NOT_INVOKED" else (str(outcome) if outcome in _OUTCOME_STATUS else "RUNNER_MALFORMED")
        except Exception: status, reason = OUTCOME_UNKNOWN, "RUNNER_EXCEPTION"
        try:
            self._connection.execute("BEGIN IMMEDIATE"); self._transition(attempt_id, decision, status, reason); self._connection.commit(); return self._result(status, attempt_id=attempt_id, reason_code=reason)
        except sqlite3.Error:
            self._connection.rollback(); return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="POST_CALL_PERSISTENCE_FAILED")

    def _technical_decision(self, control_id: Any, tool_input: dict[str, Any], expected_turn_id: Any) -> dict[str, Any]:
        decision: dict[str, Any] = {"turn_id": expected_turn_id if isinstance(expected_turn_id, str) and expected_turn_id else "unknown", "control_id": str(control_id)[:500], "server_id": "unknown", "tool_name": "unknown", "fingerprint": "unknown", "source_registry_revision": 0, "external_action_id": "failed:" + hashlib.sha256(canonical_json(tool_input).encode()).hexdigest(), "reason_code": "INVALID_CONTROL_ID"}
        if not isinstance(control_id, str): return decision
        candidate = self._candidate_registry.get_by_control_id(control_id)
        if candidate is None: return decision | {"reason_code": "CANDIDATE_NOT_FOUND"}
        decision.update({"server_id": candidate["server_id"], "tool_name": candidate["tool_name"], "fingerprint": candidate["current_fingerprint"], "source_registry_revision": candidate["current_source_registry_revision"]})
        decision["external_action_id"] = build_external_action_id(control_id, candidate["current_fingerprint"], candidate["current_source_registry_revision"], tool_input)
        try: server = self._server_registry.get(candidate["server_id"])
        except Exception: return decision | {"reason_code": "SERVER_NOT_FOUND"}
        decision.update({"endpoint": server.endpoint, "transport": server.transport})
        binding = self._auth_binding_registry.get_binding(server.server_id)
        decision.update({"auth_scheme": binding.auth_scheme if binding else None, "auth_binding_revision": binding.revision if binding else None, "secret_ref": binding.secret_ref if binding else None, "credential_slot": binding.credential_slot if binding else None})
        if candidate["presence_state"] != PRESENT: decision["reason_code"] = "CANDIDATE_MISSING"
        elif server.lifecycle_state != CONNECTED_STATE: decision["reason_code"] = "SERVER_DISCONNECTED"
        elif candidate["current_source_registry_revision"] != server.revision: decision["reason_code"] = "SOURCE_REVISION_STALE"
        elif not isinstance(candidate["current_fingerprint"], str) or len(candidate["current_fingerprint"]) != 64 or any(c not in "0123456789abcdef" for c in candidate["current_fingerprint"].lower()): decision["reason_code"] = "FINGERPRINT_INVALID"
        elif binding is None or binding.server_id != server.server_id: decision["reason_code"] = "AUTH_BINDING_MISSING"
        else: decision["reason_code"] = "READY"
        return decision

    def recover_unknown(self, attempt_id: Any) -> dict[str, Any]:
        if not isinstance(attempt_id, str) or not attempt_id: return self._result(FAILED_PRE_CALL, reason_code="UNKNOWN_ATTEMPT")
        try:
            self._connection.execute("BEGIN IMMEDIATE"); row = self._connection.execute("SELECT attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,status FROM external_tool_invocation_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if row is None: self._connection.rollback(); return self._result(FAILED_PRE_CALL, reason_code="UNKNOWN_ATTEMPT")
            if row[6] != STARTED: self._connection.rollback(); return self._result(row[6], attempt_id=attempt_id, reason_code="RECOVERY_NOT_ALLOWED")
            decision = dict(zip(("attempt_id","turn_id","control_id","server_id","tool_name","external_action_id","status"), row)); self._connection.execute("UPDATE external_tool_invocation_attempts SET status=?,reason_code=?,completed_at=? WHERE attempt_id=? AND status=?", (OUTCOME_UNKNOWN,"RECOVERED_UNKNOWN",_now(),attempt_id,STARTED)); self._audit(attempt_id, decision, OUTCOME_UNKNOWN, "RECOVERED_UNKNOWN"); self._connection.commit(); return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="RECOVERED_UNKNOWN")
        except sqlite3.Error: self._connection.rollback(); return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="RECOVERY_PERSISTENCE_FAILED")

    def get_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute("SELECT attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,fingerprint,source_registry_revision,tool_input_sha256,tool_input_byte_length,status,reason_code,created_at,started_at,completed_at FROM external_tool_invocation_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        return dict(zip(("attempt_id","turn_id","control_id","server_id","tool_name","external_action_id","fingerprint","source_registry_revision","tool_input_sha256","tool_input_byte_length","status","reason_code","created_at","started_at","completed_at"), row)) if row else None

    def list_audit(self, attempt_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute("SELECT audit_sequence,event_id,attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,status,reason_code,event_at FROM external_tool_invocation_audit WHERE attempt_id=? ORDER BY audit_sequence", (attempt_id,)).fetchall()
        return tuple(dict(zip(("audit_sequence","event_id","attempt_id","turn_id","control_id","server_id","tool_name","external_action_id","status","reason_code","event_at"), row)) for row in rows)

    def _record_failed_pre_call(self, decision: Mapping[str, Any], encoded: bytes) -> str:
        attempt_id = self._new_id(); self._insert_attempt(attempt_id, decision, hashlib.sha256(encoded).hexdigest(), len(encoded), PRE_CALL, "PRE_CALL"); self._audit(attempt_id, decision, PRE_CALL, "PRE_CALL"); self._transition(attempt_id, decision, FAILED_PRE_CALL, str(decision["reason_code"])); return attempt_id
    def _insert_attempt(self, attempt_id: str, d: Mapping[str, Any], input_hash: str, length: int, status: str, reason: str) -> None:
        self._connection.execute("INSERT INTO external_tool_invocation_attempts (attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,fingerprint,source_registry_revision,tool_input_sha256,tool_input_byte_length,status,reason_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (attempt_id,d["turn_id"],d["control_id"],d["server_id"],d["tool_name"],d["external_action_id"],d["fingerprint"],d["source_registry_revision"],input_hash,length,status,reason,_now()))
    def _transition(self, attempt_id: str, d: Mapping[str, Any], status: str, reason: str) -> None:
        now = _now(); if_started = status != STARTED
        if status == STARTED: result = self._connection.execute("UPDATE external_tool_invocation_attempts SET status=?,reason_code=?,started_at=? WHERE attempt_id=? AND status=?", (status,reason,now,attempt_id,PRE_CALL))
        else: result = self._connection.execute("UPDATE external_tool_invocation_attempts SET status=?,reason_code=?,completed_at=? WHERE attempt_id=? AND status=?", (status,reason,now,attempt_id,STARTED if if_started else PRE_CALL))
        if result.rowcount != 1: raise sqlite3.IntegrityError("invalid invocation transition")
        self._audit(attempt_id,d,status,reason)
    def _audit(self, attempt_id: str, d: Mapping[str, Any], status: str, reason: str) -> None:
        self._connection.execute("INSERT INTO external_tool_invocation_audit (event_id,attempt_id,turn_id,control_id,server_id,tool_name,external_action_id,status,reason_code,event_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (self._new_id(),attempt_id,d["turn_id"],d["control_id"],d["server_id"],d["tool_name"],d["external_action_id"],status,reason,_now()))
    def _existing(self, turn_id: str, action_id: str):
        row = self._connection.execute("SELECT attempt_id,status,reason_code FROM external_tool_invocation_attempts WHERE turn_id=? AND external_action_id=?", (turn_id,action_id)).fetchone()
        return dict(zip(("attempt_id","status","reason_code"),row)) if row else None
    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value or len(value) > 200: raise sqlite3.IntegrityError("unsafe generated id")
        return value
    @staticmethod
    def _result(status: str, *, attempt_id: Optional[str] = None, reason_code: str) -> dict[str, Any]:
        result = {"status": status, "reason_code": reason_code}
        if attempt_id is not None: result["attempt_id"] = attempt_id
        return result
    @staticmethod
    def _attempt_result(attempt: Mapping[str, Any], *, duplicate: bool) -> dict[str, Any]: return {"status": attempt["status"], "reason_code": "DUPLICATE_EXTERNAL_ACTION" if duplicate else attempt["reason_code"], "attempt_id": attempt["attempt_id"]}


__all__ = ["ExternalMcpInvocation", "ExternalInvocationError", "ExternalInvocationInitializationError", "PRE_CALL", "STARTED", "SUCCEEDED", "TOOL_ERROR", "FAILED_PRE_CALL", "OUTCOME_UNKNOWN", "TERMINAL_STATUSES", "MAX_TOOL_INPUT_BYTES", "build_external_action_id"]

