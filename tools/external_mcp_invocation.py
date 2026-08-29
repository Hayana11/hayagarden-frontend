"""Durable, fail-closed ownership of one external MCP invocation attempt.

This module deliberately stops at an injected runner seam. It never reads
secret values and knows nothing about a production Node transport.
"""

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
from .external_server_registry import ExternalServerRegistry, REVOKED_STATE
from .external_tool_execution_fence import ALLOW, ExternalToolExecutionFence
from .external_tool_registry import ExternalToolCandidateRegistry
from .external_tool_side_effect_policy import ExternalToolSideEffectPolicy

PRE_CALL = "PRE_CALL"
STARTED = "STARTED"
SUCCEEDED = "SUCCEEDED"
TOOL_ERROR = "TOOL_ERROR"
FAILED_PRE_CALL = "FAILED_PRE_CALL"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

TERMINAL_STATUSES = frozenset({SUCCEEDED, TOOL_ERROR, FAILED_PRE_CALL, OUTCOME_UNKNOWN})
_RUNNER_OUTCOMES = frozenset({"SUCCESS", "TOOL_ERROR", "NOT_INVOKED", "OUTCOME_UNKNOWN"})
_OUTCOME_STATUS = {
    "SUCCESS": SUCCEEDED,
    "TOOL_ERROR": TOOL_ERROR,
    "NOT_INVOKED": FAILED_PRE_CALL,
    "OUTCOME_UNKNOWN": OUTCOME_UNKNOWN,
}
MAX_TOOL_INPUT_BYTES = 256 * 1024


class ExternalInvocationError(ValueError):
    code = "EXTERNAL_INVOCATION_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ExternalInvocationInitializationError(ExternalInvocationError):
    code = "SERVER_AUTHORITY_MISMATCH"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_tool_input(value: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(value, Mapping):
        raise ExternalInvocationError("tool_input must be an object", code="INVALID_TOOL_INPUT")
    # JSON round-tripping both rejects unsafe values and severs caller mutation.
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        snapshot = json.loads(encoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExternalInvocationError("tool_input is not JSON-safe", code="INVALID_TOOL_INPUT") from exc
    if not isinstance(snapshot, dict):
        raise ExternalInvocationError("tool_input must freeze to an object", code="INVALID_TOOL_INPUT")
    if len(encoded) > MAX_TOOL_INPUT_BYTES:
        raise ExternalInvocationError("tool_input exceeds the byte limit", code="TOOL_INPUT_TOO_LARGE")
    return snapshot, encoded


def _freeze(value: Any) -> Any:
    """Recursively freeze an already JSON-safe snapshot for the runner seam."""
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


class ExternalMcpInvocation:
    """The sole durable owner of a final fence check and one runner handoff."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        server_registry: ExternalServerRegistry,
        candidate_registry: ExternalToolCandidateRegistry,
        side_effect_policy: ExternalToolSideEffectPolicy,
        execution_fence: ExternalToolExecutionFence,
        auth_binding_registry: ExternalMcpAuthBindingRegistry,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        owners = (server_registry, candidate_registry, side_effect_policy, execution_fence, auth_binding_registry)
        if (
            not isinstance(connection, sqlite3.Connection)
            or not isinstance(server_registry, ExternalServerRegistry)
            or not isinstance(candidate_registry, ExternalToolCandidateRegistry)
            or not isinstance(side_effect_policy, ExternalToolSideEffectPolicy)
            or not isinstance(execution_fence, ExternalToolExecutionFence)
            or not isinstance(auth_binding_registry, ExternalMcpAuthBindingRegistry)
            or any(getattr(owner, "_connection", None) is not connection for owner in owners)
            or getattr(execution_fence, "_server_registry", None) is not server_registry
            or getattr(execution_fence, "_candidate_registry", None) is not candidate_registry
            or getattr(execution_fence, "_side_effect_policy", None) is not side_effect_policy
            or getattr(auth_binding_registry, "_server_registry", None) is not server_registry
        ):
            raise ExternalInvocationInitializationError("owners must share one connection")
        self._connection = connection
        self._server_registry = server_registry
        self._auth_binding_registry = auth_binding_registry
        self._execution_fence = execution_fence
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))
        self.initialize()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_tool_invocation_attempts (
                attempt_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                control_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                external_action_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                source_registry_revision INTEGER NOT NULL,
                side_effect_class TEXT NOT NULL,
                tool_input_sha256 TEXT NOT NULL,
                tool_input_byte_length INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN (
                    'PRE_CALL', 'STARTED', 'SUCCEEDED', 'TOOL_ERROR',
                    'FAILED_PRE_CALL', 'OUTCOME_UNKNOWN'
                )),
                reason_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE (turn_id, external_action_id)
            );
            CREATE TABLE IF NOT EXISTS external_tool_invocation_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                attempt_id TEXT NOT NULL REFERENCES external_tool_invocation_attempts(attempt_id),
                turn_id TEXT NOT NULL,
                control_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                external_action_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                event_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS external_tool_invocation_audit_no_update
            BEFORE UPDATE ON external_tool_invocation_audit BEGIN
                SELECT RAISE(ABORT, 'external invocation audit is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS external_tool_invocation_audit_no_delete
            BEFORE DELETE ON external_tool_invocation_audit BEGIN
                SELECT RAISE(ABORT, 'external invocation audit is append-only');
            END;
            """
        )
        self._connection.commit()

    def invoke(
        self,
        control_id: Any,
        tool_input: Any,
        turn_lease: Any,
        *,
        expected_turn_id: Any,
        runner: Callable[[Mapping[str, Any]], Any],
    ) -> dict[str, Any]:
        try:
            frozen_input, encoded_input = _snapshot_tool_input(tool_input)
        except ExternalInvocationError as exc:
            return self._result(FAILED_PRE_CALL, reason_code=exc.code)
        if not callable(runner):
            return self._result(FAILED_PRE_CALL, reason_code="RUNNER_INVALID")

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            decision = self._execution_fence.evaluate(
                control_id, frozen_input, turn_lease, expected_turn_id=expected_turn_id
            )
            if decision.get("decision") != ALLOW:
                attempt_id = self._record_failed_pre_call(
                    decision, control_id, expected_turn_id, encoded_input
                )
                self._connection.commit()
                return self._result(
                    FAILED_PRE_CALL,
                    attempt_id=attempt_id,
                    reason_code=str(decision.get("reason_code", "FENCE_DENIED")),
                )
            server = self._server_snapshot(decision)
            if server is None:
                self._connection.rollback()
                return self._result(FAILED_PRE_CALL, reason_code="SERVER_AUTHORITY_CHANGED")
            binding = self._auth_binding_registry.get_binding(decision["server_id"])
            if binding is None:
                attempt_id = self._record_failed_pre_call(
                    decision, control_id, expected_turn_id, encoded_input,
                    reason_override="AUTH_BINDING_MISSING",
                )
                self._connection.commit()
                return self._result(
                    FAILED_PRE_CALL, attempt_id=attempt_id, reason_code="AUTH_BINDING_MISSING"
                )
            existing = self._existing(decision["turn_id"], decision["external_action_id"])
            if existing is not None:
                self._connection.commit()
                return self._attempt_result(existing, duplicate=True)
            attempt_id = self._new_id()
            input_hash = hashlib.sha256(encoded_input).hexdigest()
            self._insert_attempt(attempt_id, decision, input_hash, len(encoded_input), PRE_CALL, "PRE_CALL")
            self._audit(attempt_id, decision, PRE_CALL, "PRE_CALL")
            self._transition(attempt_id, decision, STARTED, "RUNNER_STARTED")
            call_envelope = MappingProxyType(
                {
                    "server_id": decision["server_id"],
                    "tool_name": decision["tool_name"],
                    "endpoint": server.endpoint,
                    "transport": server.transport,
                    "fingerprint": decision["fingerprint"],
                    "source_registry_revision": decision["source_registry_revision"],
                    "side_effect_class": decision["side_effect_class"],
                    "external_action_id": decision["external_action_id"],
                    "auth_scheme": binding.auth_scheme,
                    "auth_binding_revision": binding.revision,
                    "secret_ref": binding.secret_ref,
                    "credential_slot": binding.credential_slot,
                    "tool_input": _freeze(frozen_input),
                }
            )
            self._connection.commit()
        except sqlite3.IntegrityError:
            self._connection.rollback()
            existing = self._existing_for_decision_safely(control_id, frozen_input, turn_lease, expected_turn_id)
            return self._attempt_result(existing, duplicate=True) if existing else self._result(FAILED_PRE_CALL, reason_code="AUTHORITY_WRITE_FAILED")
        except sqlite3.Error:
            self._connection.rollback()
            return self._result(FAILED_PRE_CALL, reason_code="AUTHORITY_WRITE_FAILED")

        try:
            runner_result = runner(call_envelope)
            outcome = runner_result.get("status") if isinstance(runner_result, Mapping) else None
            terminal_status = _OUTCOME_STATUS.get(outcome) if outcome in _RUNNER_OUTCOMES else OUTCOME_UNKNOWN
            reason = str(outcome) if outcome in _RUNNER_OUTCOMES else "RUNNER_MALFORMED"
        except Exception:
            terminal_status, reason = OUTCOME_UNKNOWN, "RUNNER_EXCEPTION"

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._transition(attempt_id, decision, terminal_status, reason)
            self._connection.commit()
            return self._result(terminal_status, attempt_id=attempt_id, reason_code=reason)
        except sqlite3.Error:
            self._connection.rollback()
            return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="POST_CALL_PERSISTENCE_FAILED")

    def recover_unknown(self, attempt_id: Any) -> dict[str, Any]:
        if not isinstance(attempt_id, str) or not attempt_id:
            return self._result(FAILED_PRE_CALL, reason_code="UNKNOWN_ATTEMPT")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT attempt_id, turn_id, control_id, server_id, tool_name, external_action_id, status "
                "FROM external_tool_invocation_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                self._connection.rollback()
                return self._result(FAILED_PRE_CALL, reason_code="UNKNOWN_ATTEMPT")
            if row[6] != STARTED:
                self._connection.rollback()
                return self._result(row[6], attempt_id=attempt_id, reason_code="RECOVERY_NOT_ALLOWED")
            decision = dict(zip(("attempt_id", "turn_id", "control_id", "server_id", "tool_name", "external_action_id", "status"), row))
            self._connection.execute(
                "UPDATE external_tool_invocation_attempts SET status=?, reason_code=?, completed_at=? WHERE attempt_id=? AND status=?",
                (OUTCOME_UNKNOWN, "RECOVERED_UNKNOWN", _now(), attempt_id, STARTED),
            )
            self._audit(attempt_id, decision, OUTCOME_UNKNOWN, "RECOVERED_UNKNOWN")
            self._connection.commit()
            return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="RECOVERED_UNKNOWN")
        except sqlite3.Error:
            self._connection.rollback()
            return self._result(OUTCOME_UNKNOWN, attempt_id=attempt_id, reason_code="RECOVERY_PERSISTENCE_FAILED")

    def get_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute(
            "SELECT attempt_id, turn_id, control_id, server_id, tool_name, external_action_id, fingerprint, "
            "source_registry_revision, side_effect_class, tool_input_sha256, tool_input_byte_length, status, "
            "reason_code, created_at, started_at, completed_at FROM external_tool_invocation_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            return None
        keys = ("attempt_id", "turn_id", "control_id", "server_id", "tool_name", "external_action_id", "fingerprint", "source_registry_revision", "side_effect_class", "tool_input_sha256", "tool_input_byte_length", "status", "reason_code", "created_at", "started_at", "completed_at")
        return dict(zip(keys, row))

    def list_audit(self, attempt_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            "SELECT audit_sequence, event_id, attempt_id, turn_id, control_id, server_id, tool_name, "
            "external_action_id, status, reason_code, event_at FROM external_tool_invocation_audit "
            "WHERE attempt_id=? ORDER BY audit_sequence", (attempt_id,)
        ).fetchall()
        keys = ("audit_sequence", "event_id", "attempt_id", "turn_id", "control_id", "server_id", "tool_name", "external_action_id", "status", "reason_code", "event_at")
        return tuple(dict(zip(keys, row)) for row in rows)

    def _server_snapshot(self, decision: Mapping[str, Any]):
        try:
            server = self._server_registry.get(decision["server_id"])
            if (
                server.revision == decision["source_registry_revision"]
                and server.lifecycle_state != REVOKED_STATE
                and bool(server.endpoint)
                and server.server_id == decision["server_id"]
            ):
                return server
            return None
        except Exception:
            return None

    def _insert_attempt(self, attempt_id: str, decision: Mapping[str, Any], input_hash: str, byte_length: int, status: str, reason: str) -> None:
        self._connection.execute(
            "INSERT INTO external_tool_invocation_attempts (attempt_id, turn_id, control_id, server_id, tool_name, external_action_id, fingerprint, source_registry_revision, side_effect_class, tool_input_sha256, tool_input_byte_length, status, reason_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, decision["turn_id"], decision["control_id"], decision["server_id"], decision["tool_name"], decision["external_action_id"], decision["fingerprint"], decision["source_registry_revision"], decision["side_effect_class"], input_hash, byte_length, status, reason, _now()),
        )

    def _record_failed_pre_call(
        self, decision: Mapping[str, Any], control_id: Any, expected_turn_id: Any, encoded_input: bytes,
        *, reason_override: Optional[str] = None,
    ) -> str:
        """Persist a bounded denial without retaining any caller payload."""
        parts = control_id.split(":", 2) if isinstance(control_id, str) else ()
        server_id = decision.get("server_id") or (parts[1] if len(parts) == 3 else "unknown")
        tool_name = decision.get("tool_name") or (parts[2] if len(parts) == 3 else "unknown")
        turn_id = decision.get("turn_id") or (expected_turn_id if isinstance(expected_turn_id, str) and expected_turn_id else "unknown")
        attempt_id = self._new_id()
        reason = str(reason_override or decision.get("reason_code", "FENCE_DENIED"))
        denial_identity = hashlib.sha256(
            json.dumps(
                {
                    "control_id": str(control_id),
                    "tool_input_sha256": hashlib.sha256(encoded_input).hexdigest(),
                    "reason_code": reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        safe_decision = {
            "turn_id": turn_id,
            "control_id": decision.get("control_id") or str(control_id)[:500],
            "server_id": server_id,
            "tool_name": tool_name,
            "external_action_id": decision.get("external_action_id") or "failed:" + denial_identity,
            "fingerprint": decision.get("fingerprint") or "unknown",
            "source_registry_revision": decision.get("source_registry_revision") or 0,
            "side_effect_class": decision.get("side_effect_class") or "unknown",
        }
        self._insert_attempt(
            attempt_id, safe_decision, hashlib.sha256(encoded_input).hexdigest(), len(encoded_input), PRE_CALL, "PRE_CALL"
        )
        self._audit(attempt_id, safe_decision, PRE_CALL, "PRE_CALL")
        # PRE_CALL is terminal only through this single, transaction-local transition.
        now = _now()
        self._connection.execute(
            "UPDATE external_tool_invocation_attempts SET status=?, reason_code=?, completed_at=? WHERE attempt_id=? AND status=?",
            (FAILED_PRE_CALL, reason, now, attempt_id, PRE_CALL),
        )
        self._audit(attempt_id, safe_decision, FAILED_PRE_CALL, reason)
        return attempt_id

    def _transition(self, attempt_id: str, decision: Mapping[str, Any], status: str, reason: str) -> None:
        now = _now()
        if status == STARTED:
            result = self._connection.execute("UPDATE external_tool_invocation_attempts SET status=?, reason_code=?, started_at=? WHERE attempt_id=? AND status=?", (status, reason, now, attempt_id, PRE_CALL))
        else:
            result = self._connection.execute("UPDATE external_tool_invocation_attempts SET status=?, reason_code=?, completed_at=? WHERE attempt_id=? AND status=?", (status, reason, now, attempt_id, STARTED))
        if result.rowcount != 1:
            raise sqlite3.IntegrityError("invalid invocation transition")
        self._audit(attempt_id, decision, status, reason)

    def _audit(self, attempt_id: str, decision: Mapping[str, Any], status: str, reason: str) -> None:
        self._connection.execute(
            "INSERT INTO external_tool_invocation_audit (event_id, attempt_id, turn_id, control_id, server_id, tool_name, external_action_id, status, reason_code, event_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self._new_id(), attempt_id, decision["turn_id"], decision["control_id"], decision["server_id"], decision["tool_name"], decision["external_action_id"], status, reason, _now()),
        )

    def _existing(self, turn_id: str, action_id: str) -> Optional[dict[str, Any]]:
        row = self._connection.execute("SELECT attempt_id, status, reason_code FROM external_tool_invocation_attempts WHERE turn_id=? AND external_action_id=?", (turn_id, action_id)).fetchone()
        return dict(zip(("attempt_id", "status", "reason_code"), row)) if row else None

    def _existing_for_decision_safely(self, control_id: Any, tool_input: dict[str, Any], turn_lease: Any, expected_turn_id: Any) -> Optional[dict[str, Any]]:
        try:
            decision = self._execution_fence.evaluate(control_id, tool_input, turn_lease, expected_turn_id=expected_turn_id)
            return self._existing(decision["turn_id"], decision["external_action_id"]) if decision.get("decision") == ALLOW else None
        except Exception:
            return None

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value or len(value) > 200:
            raise sqlite3.IntegrityError("unsafe generated id")
        return value

    @staticmethod
    def _result(status: str, *, attempt_id: Optional[str] = None, reason_code: str) -> dict[str, Any]:
        result: dict[str, Any] = {"status": status, "reason_code": reason_code}
        if attempt_id is not None:
            result["attempt_id"] = attempt_id
        return result

    @staticmethod
    def _attempt_result(attempt: Optional[Mapping[str, Any]], *, duplicate: bool) -> dict[str, Any]:
        if attempt is None:
            return ExternalMcpInvocation._result(FAILED_PRE_CALL, reason_code="DUPLICATE_EXTERNAL_ACTION")
        return {"status": attempt["status"], "reason_code": "DUPLICATE_EXTERNAL_ACTION" if duplicate else attempt["reason_code"], "attempt_id": attempt["attempt_id"]}


__all__ = [
    "ExternalMcpInvocation", "ExternalInvocationError", "ExternalInvocationInitializationError",
    "PRE_CALL", "STARTED", "SUCCEEDED", "TOOL_ERROR", "FAILED_PRE_CALL", "OUTCOME_UNKNOWN",
    "TERMINAL_STATUSES", "MAX_TOOL_INPUT_BYTES",
]
