"""Administrative side-effect classification for reviewed external tools.

The owner stores only explicit management classifications.  It has no default
database, network, runtime, model, or execution integration.
"""

from __future__ import annotations

import hashlib
import secrets
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
from .external_tool_registry import ExternalToolCandidateRegistry

NONE = "none"
EXTERNAL_STATE = "external_state"
CODE_OR_PROCESS = "code_or_process"
UNKNOWN = "unknown"
SIDE_EFFECT_CLASSES = frozenset({NONE, EXTERNAL_STATE, CODE_OR_PROCESS, UNKNOWN})
MAX_CLASSIFICATION_TEXT_BYTES = 200


class SideEffectPolicyError(Exception):
    code = "SIDE_EFFECT_POLICY_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class SideEffectValidationError(SideEffectPolicyError):
    code = "INVALID_SIDE_EFFECT_CLASSIFICATION"


class SideEffectRejectedError(SideEffectPolicyError):
    code = "SIDE_EFFECT_CLASSIFICATION_REJECTED"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _require_server_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or ":" in value:
        raise SideEffectValidationError(
            "server_id is invalid", code="INVALID_SERVER_ID"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SideEffectValidationError(
            "server_id contains a control character", code="INVALID_SERVER_ID"
        )
    return value


def _require_tool_name(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SideEffectValidationError(
            "tool_name is invalid", code="INVALID_TOOL_NAME"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SideEffectValidationError(
            "tool_name contains a control character", code="INVALID_TOOL_NAME"
        )
    return value


def _review_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SideEffectValidationError(
            f"{field} must be a non-empty exact string", code=f"INVALID_{field.upper()}"
        )
    if len(value.encode("utf-8")) > MAX_CLASSIFICATION_TEXT_BYTES:
        raise SideEffectValidationError(
            f"{field} is too long", code=f"INVALID_{field.upper()}"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SideEffectValidationError(
            f"{field} contains a control character", code=f"INVALID_{field.upper()}"
        )
    if value.lower() == "anonymous":
        raise SideEffectValidationError(
            f"{field} cannot be anonymous", code=f"INVALID_{field.upper()}"
        )
    return value


def _fingerprint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != hashlib.sha256().digest_size * 2
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise SideEffectValidationError(
            "expected fingerprint is invalid", code="INVALID_EXPECTED_FINGERPRINT"
        )
    return value


def _revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SideEffectValidationError(
            "expected source registry revision is invalid",
            code="INVALID_EXPECTED_SOURCE_REVISION",
        )
    return value


def _classification(value: Any) -> str:
    if not isinstance(value, str) or value not in SIDE_EFFECT_CLASSES:
        raise SideEffectValidationError(
            "side-effect class is not one of the frozen values",
            code="INVALID_SIDE_EFFECT_CLASS",
        )
    return value


class ExternalToolSideEffectPolicy:
    """SQLite owner for explicit side-effect classification facts."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        candidate_registry: ExternalToolCandidateRegistry,
        server_registry: ExternalServerRegistry,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be an sqlite3.Connection")
        if not isinstance(candidate_registry, ExternalToolCandidateRegistry):
            raise TypeError("candidate_registry must be an ExternalToolCandidateRegistry")
        if not isinstance(server_registry, ExternalServerRegistry):
            raise TypeError("server_registry must be an ExternalServerRegistry")
        if (
            getattr(candidate_registry, "_connection", None) is not connection
            or getattr(server_registry, "_connection", None) is not connection
        ):
            raise SideEffectRejectedError(
                "classification owner and registries must share the authoritative connection",
                code="SERVER_AUTHORITY_MISMATCH",
            )
        self._connection = connection
        self._candidate_registry = candidate_registry
        self._server_registry = server_registry
        self.initialize()

    def initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS external_tool_side_effect_baselines (
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                classified_fingerprint TEXT NOT NULL,
                classified_source_registry_revision INTEGER NOT NULL CHECK (
                    classified_source_registry_revision >= 1
                ),
                side_effect_class TEXT NOT NULL CHECK (
                    side_effect_class IN ('none', 'external_state', 'code_or_process', 'unknown')
                ),
                classified_at TEXT NOT NULL,
                classified_actor TEXT NOT NULL,
                classified_provenance TEXT NOT NULL,
                PRIMARY KEY (server_id, tool_name)
            );
            CREATE TABLE IF NOT EXISTS external_tool_side_effect_audit (
                audit_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                classification_event_id TEXT NOT NULL UNIQUE,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                source_registry_revision INTEGER NOT NULL CHECK (
                    source_registry_revision >= 1
                ),
                side_effect_class TEXT NOT NULL CHECK (
                    side_effect_class IN ('none', 'external_state', 'code_or_process', 'unknown')
                ),
                actor TEXT NOT NULL,
                provenance TEXT NOT NULL,
                classified_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS external_tool_side_effect_audit_no_update
            BEFORE UPDATE ON external_tool_side_effect_audit
            BEGIN
                SELECT RAISE(ABORT, 'side-effect audit is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS external_tool_side_effect_audit_no_delete
            BEFORE DELETE ON external_tool_side_effect_audit
            BEGIN
                SELECT RAISE(ABORT, 'side-effect audit is append-only');
            END;
            """
        )
        self._connection.commit()

    def classify(
        self,
        *,
        server_id: Any,
        tool_name: Any,
        expected_fingerprint: Any,
        expected_source_registry_revision: Any,
        side_effect_class: Any,
        actor: Any,
        provenance: Any,
    ) -> dict[str, Any]:
        server_id = _require_server_id(server_id)
        tool_name = _require_tool_name(tool_name)
        fingerprint = _fingerprint(expected_fingerprint)
        source_revision = _revision(expected_source_registry_revision)
        side_effect_class = _classification(side_effect_class)
        actor = _review_text(actor, "actor")
        provenance = _review_text(provenance, "provenance")

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            candidate = self._candidate_for_classification(
                server_id, tool_name, fingerprint, source_revision
            )
            self._gate_server(server_id, source_revision)
            approval = self._candidate_registry.get_effective_approval(server_id, tool_name)
            if approval.get("effective_approved") is not True:
                raise SideEffectRejectedError(
                    "candidate does not have an effective approval",
                    code="APPROVAL_NOT_EFFECTIVE",
                )
            self._verify_snapshot(server_id, tool_name, fingerprint, candidate)
            baseline = self._baseline_row(server_id, tool_name)
            if (
                baseline is not None
                and baseline[0] == fingerprint
                and baseline[1] == source_revision
                and baseline[2] == side_effect_class
                and baseline[4] == actor
                and baseline[5] == provenance
            ):
                self._connection.rollback()
                return self._result(
                    server_id, tool_name, fingerprint, source_revision,
                    side_effect_class, changed=False, baseline=baseline,
                )

            now = _timestamp()
            self._connection.execute(
                "INSERT INTO external_tool_side_effect_baselines ("
                "server_id, tool_name, classified_fingerprint, "
                "classified_source_registry_revision, side_effect_class, classified_at, "
                "classified_actor, classified_provenance) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(server_id, tool_name) DO UPDATE SET "
                "classified_fingerprint=excluded.classified_fingerprint, "
                "classified_source_registry_revision=excluded.classified_source_registry_revision, "
                "side_effect_class=excluded.side_effect_class, "
                "classified_at=excluded.classified_at, "
                "classified_actor=excluded.classified_actor, "
                "classified_provenance=excluded.classified_provenance",
                (
                    server_id, tool_name, fingerprint, source_revision, side_effect_class,
                    now, actor, provenance,
                ),
            )
            self._connection.execute(
                "INSERT INTO external_tool_side_effect_audit ("
                "classification_event_id, server_id, tool_name, fingerprint, "
                "source_registry_revision, side_effect_class, actor, provenance, classified_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(16), server_id, tool_name, fingerprint, source_revision,
                    side_effect_class, actor, provenance, now,
                ),
            )
            baseline = self._baseline_row(server_id, tool_name)
            self._connection.commit()
            return self._result(
                server_id, tool_name, fingerprint, source_revision,
                side_effect_class, changed=True, baseline=baseline,
            )
        except Exception:
            self._connection.rollback()
            raise

    def get_baseline(self, server_id: Any, tool_name: Any) -> Optional[dict[str, Any]]:
        server_id = _require_server_id(server_id)
        tool_name = _require_tool_name(tool_name)
        row = self._baseline_row(server_id, tool_name)
        return self._baseline_dict(server_id, tool_name, row) if row else None

    def list_audit(self, server_id: Any, tool_name: Any) -> tuple[dict[str, Any], ...]:
        server_id = _require_server_id(server_id)
        tool_name = _require_tool_name(tool_name)
        rows = self._connection.execute(
            "SELECT classification_event_id, server_id, tool_name, fingerprint, "
            "source_registry_revision, side_effect_class, actor, provenance, classified_at "
            "FROM external_tool_side_effect_audit WHERE server_id=? AND tool_name=? "
            "ORDER BY audit_sequence",
            (server_id, tool_name),
        ).fetchall()
        keys = (
            "classification_event_id", "server_id", "tool_name", "fingerprint",
            "source_registry_revision", "side_effect_class", "actor", "provenance",
            "classified_at",
        )
        return tuple(dict(zip(keys, row)) for row in rows)

    def get_effective_classification(
        self, server_id: Any, tool_name: Any
    ) -> dict[str, Any]:
        server_id = _require_server_id(server_id)
        tool_name = _require_tool_name(tool_name)
        candidate = self._candidate_registry.get_candidate(server_id, tool_name)
        baseline = self.get_baseline(server_id, tool_name)
        effective = False
        effective_class = UNKNOWN
        if candidate is not None:
            try:
                server = self._server_registry.get(server_id)
            except UnknownServerError:
                server = None
            approval = self._candidate_registry.get_effective_approval(server_id, tool_name)
            effective = bool(
                candidate["presence_state"] == "PRESENT"
                and candidate["current_fingerprint"]
                and candidate["current_source_registry_revision"] is not None
                and approval.get("effective_approved") is True
                and baseline is not None
                and baseline["classified_fingerprint"] == candidate["current_fingerprint"]
                and baseline["classified_source_registry_revision"]
                == candidate["current_source_registry_revision"]
                and baseline["side_effect_class"] in SIDE_EFFECT_CLASSES
                and server is not None
                and server.lifecycle_state in (REGISTRATION_STATE, SERVER_REVIEW_REQUIRED)
                and server.revision == candidate["current_source_registry_revision"]
            )
            if effective:
                effective_class = baseline["side_effect_class"]
        return {
            "effective_classified": effective,
            "effective_side_effect_class": effective_class,
            "server_id": server_id,
            "tool_name": tool_name,
            "candidate_fingerprint": (
                candidate["current_fingerprint"] if candidate else None
            ),
            "candidate_source_registry_revision": (
                candidate["current_source_registry_revision"] if candidate else None
            ),
            "baseline": baseline,
        }

    def _candidate_for_classification(
        self, server_id: str, tool_name: str, fingerprint: str, source_revision: int
    ) -> Mapping[str, Any]:
        candidate = self._candidate_registry.get_candidate(server_id, tool_name)
        if candidate is None:
            raise SideEffectRejectedError(
                "candidate does not exist", code="UNKNOWN_CANDIDATE"
            )
        if candidate["presence_state"] != "PRESENT":
            raise SideEffectRejectedError(
                "candidate is not present", code="CANDIDATE_NOT_PRESENT"
            )
        if candidate["current_fingerprint"] != fingerprint:
            raise SideEffectRejectedError(
                "candidate fingerprint changed", code="FINGERPRINT_MISMATCH"
            )
        if candidate["current_source_registry_revision"] is None:
            raise SideEffectRejectedError(
                "candidate freshness is unknown", code="FRESHNESS_UNKNOWN"
            )
        if candidate["current_source_registry_revision"] != source_revision:
            raise SideEffectRejectedError(
                "candidate source revision changed", code="SOURCE_REVISION_MISMATCH"
            )
        return candidate

    def _gate_server(self, server_id: str, source_revision: int) -> None:
        try:
            server = self._server_registry.get(server_id)
        except UnknownServerError as exc:
            raise SideEffectRejectedError(
                "server identity is unknown", code="UNKNOWN_SERVER"
            ) from exc
        if server.lifecycle_state == REVOKED_STATE:
            raise SideEffectRejectedError(
                "revoked server cannot be classified", code="REVOKED_SERVER"
            )
        if server.lifecycle_state not in (REGISTRATION_STATE, SERVER_REVIEW_REQUIRED):
            raise SideEffectRejectedError(
                "server lifecycle is not classifiable", code="INVALID_SERVER_STATE"
            )
        if server.revision != source_revision:
            raise SideEffectRejectedError(
                "server revision changed before classification", code="REVISION_MISMATCH"
            )

    def _verify_snapshot(
        self, server_id: str, tool_name: str, fingerprint: str, candidate: Mapping[str, Any]
    ) -> None:
        snapshots = self._candidate_registry.list_snapshots(server_id, tool_name)
        snapshot = next(
            (row for row in snapshots if row["fingerprint"] == fingerprint), None
        )
        if snapshot is None:
            raise SideEffectRejectedError(
                "raw snapshot does not exist", code="SNAPSHOT_MISSING"
            )
        actual = hashlib.sha256(snapshot["raw_snapshot_json"].encode("utf-8")).hexdigest()
        if actual != candidate["current_fingerprint"]:
            raise SideEffectRejectedError(
                "raw snapshot hash does not match candidate", code="SNAPSHOT_MISMATCH"
            )

    def _baseline_row(self, server_id: str, tool_name: str):
        return self._connection.execute(
            "SELECT classified_fingerprint, classified_source_registry_revision, "
            "side_effect_class, classified_at, classified_actor, classified_provenance "
            "FROM external_tool_side_effect_baselines WHERE server_id=? AND tool_name=?",
            (server_id, tool_name),
        ).fetchone()

    @staticmethod
    def _baseline_dict(server_id: str, tool_name: str, row) -> dict[str, Any]:
        keys = (
            "classified_fingerprint", "classified_source_registry_revision",
            "side_effect_class", "classified_at", "classified_actor", "classified_provenance",
        )
        return {"server_id": server_id, "tool_name": tool_name, **dict(zip(keys, row))}

    @classmethod
    def _result(
        cls, server_id: str, tool_name: str, fingerprint: str, source_revision: int,
        side_effect_class: str, *, changed: bool, baseline,
    ) -> dict[str, Any]:
        return {
            "changed": changed,
            "server_id": server_id,
            "tool_name": tool_name,
            "fingerprint": fingerprint,
            "source_registry_revision": source_revision,
            "side_effect_class": side_effect_class,
            "baseline": (
                cls._baseline_dict(server_id, tool_name, baseline)
                if baseline else None
            ),
        }


__all__ = [
    "CODE_OR_PROCESS",
    "EXTERNAL_STATE",
    "ExternalToolSideEffectPolicy",
    "NONE",
    "SIDE_EFFECT_CLASSES",
    "SideEffectPolicyError",
    "SideEffectRejectedError",
    "SideEffectValidationError",
    "UNKNOWN",
]
