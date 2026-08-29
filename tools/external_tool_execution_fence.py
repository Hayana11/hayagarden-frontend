"""Canonical pre-execution decisions for reviewed external tools.

This owner is deliberately read-only.  It consumes the existing server,
candidate, approval, and side-effect classification owners and returns a
decision for one concrete action in one chat turn.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any, Optional

from .external_server_registry import ExternalServerRegistry
from .external_tool_registry import ExternalToolCandidateRegistry
from .lease_signer import (
    ISSUED_FROM_VALUES,
    LEASE_VERSION,
    TURN_LEASE_FIELDS,
    TURN_MODES,
)
from .external_tool_side_effect_policy import (
    AUTONOMOUS,
    CODE_OR_PROCESS,
    EXTERNAL_STATE,
    NONE,
    OWNER_CONFIRMED,
    SIDE_EFFECT_CLASSES,
    UNKNOWN,
    ExternalToolSideEffectPolicy,
)

ALLOW = "ALLOW"
ASK = "ASK"
DENY = "DENY"
DECISIONS = frozenset({ALLOW, ASK, DENY})

UNKNOWN_CONTROL_ID = "UNKNOWN_CONTROL_ID"
CANDIDATE_NOT_PRESENT = "CANDIDATE_NOT_PRESENT"
APPROVAL_NOT_EFFECTIVE = "APPROVAL_NOT_EFFECTIVE"
CLASSIFICATION_NOT_EFFECTIVE = "CLASSIFICATION_NOT_EFFECTIVE"
UNKNOWN_SIDE_EFFECT = "UNKNOWN_SIDE_EFFECT"
CODE_OR_PROCESS_DENIED = "CODE_OR_PROCESS_DENIED"
TURN_LEASE_INVALID = "TURN_LEASE_INVALID"
TURN_ID_MISMATCH = "TURN_ID_MISMATCH"
TURN_MODE_NOT_ALLOWED = "TURN_MODE_NOT_ALLOWED"
ACTION_NOT_CONFIRMED = "ACTION_NOT_CONFIRMED"
AUTONOMOUS_CONFIRMATION_ARTIFACT = "AUTONOMOUS_CONFIRMATION_ARTIFACT"
EXECUTION_MODE_INVALID = "EXECUTION_MODE_INVALID"
OWNER_CONFIRMATION_REQUIRED = "OWNER_CONFIRMATION_REQUIRED"
ALLOW_CURRENT_ACTION = "ALLOW_CURRENT_ACTION"
AUTHORITY_READ_FAILED = "AUTHORITY_READ_FAILED"

ACTION_ID_PREFIX = "external_action_sha256:"


class ExternalExecutionFenceError(ValueError):
    code = "EXTERNAL_EXECUTION_FENCE_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        if code is not None:
            self.code = code


class ExternalExecutionFenceInitializationError(ExternalExecutionFenceError):
    code = "EXTERNAL_EXECUTION_FENCE_INIT_FAILED"


class ExternalExecutionFenceValidationError(ExternalExecutionFenceError):
    code = "TURN_LEASE_INVALID"


def _deny(reason_code: str, **extra: Any) -> dict[str, Any]:
    result = {"decision": DENY, "reason_code": reason_code}
    result.update(extra)
    return result


def _parse_control_id(control_id: Any) -> tuple[str, str]:
    if not isinstance(control_id, str) or not control_id or control_id != control_id.strip():
        raise ExternalExecutionFenceValidationError(
            "control_id must be an exact non-empty string", code=UNKNOWN_CONTROL_ID
        )
    if not control_id.startswith("ext:"):
        raise ExternalExecutionFenceValidationError(
            "control_id is not an external control id", code=UNKNOWN_CONTROL_ID
        )
    remainder = control_id[4:]
    server_id, separator, tool_name = remainder.partition(":")
    if not separator or not server_id or not tool_name:
        raise ExternalExecutionFenceValidationError(
            "control_id has an invalid external format", code=UNKNOWN_CONTROL_ID
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in control_id):
        raise ExternalExecutionFenceValidationError(
            "control_id contains a control character", code=UNKNOWN_CONTROL_ID
        )
    return server_id, tool_name


def _canonical_tool_input(tool_input: Any) -> tuple[dict[str, Any], bytes]:
    if not isinstance(tool_input, Mapping):
        raise ExternalExecutionFenceValidationError(
            "tool_input must be an object", code="INVALID_TOOL_INPUT"
        )
    value = dict(tool_input)
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExternalExecutionFenceValidationError(
            "tool_input is not safely canonicalizable", code="INVALID_TOOL_INPUT"
        ) from exc
    return value, canonical


def _valid_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def build_external_action_id(
    control_id: Any,
    fingerprint: Any,
    source_registry_revision: Any,
    side_effect_class: Any,
    tool_input: Any,
) -> str:
    """Build the canonical id for one exact external action."""
    _parse_control_id(control_id)
    if not _valid_fingerprint(fingerprint):
        raise ExternalExecutionFenceValidationError(
            "fingerprint is invalid", code=CLASSIFICATION_NOT_EFFECTIVE
        )
    if (
        isinstance(source_registry_revision, bool)
        or not isinstance(source_registry_revision, int)
        or source_registry_revision < 1
    ):
        raise ExternalExecutionFenceValidationError(
            "source registry revision is invalid", code=CLASSIFICATION_NOT_EFFECTIVE
        )
    if side_effect_class not in SIDE_EFFECT_CLASSES:
        raise ExternalExecutionFenceValidationError(
            "side-effect class is invalid", code=CLASSIFICATION_NOT_EFFECTIVE
        )
    normalized_input, _ = _canonical_tool_input(tool_input)
    payload = {
        "control_id": control_id,
        "fingerprint": fingerprint,
        "source_registry_revision": source_registry_revision,
        "side_effect_class": side_effect_class,
        "tool_input": normalized_input,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return ACTION_ID_PREFIX + hashlib.sha256(canonical).hexdigest()


def _lease_error(turn_lease: Any) -> Optional[str]:
    if not isinstance(turn_lease, Mapping):
        return "turn_lease is not an object"
    if set(turn_lease) != set(TURN_LEASE_FIELDS):
        return "turn_lease field set mismatch"
    if turn_lease.get("lease_version") != LEASE_VERSION:
        return "lease_version mismatch"
    turn_id = turn_lease.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id or turn_id != turn_id.strip():
        return "turn_id malformed"
    mode = turn_lease.get("turn_mode")
    if not isinstance(mode, str) or mode not in TURN_MODES:
        return "turn_mode malformed"
    issued_from = turn_lease.get("issued_from")
    if not isinstance(issued_from, str) or issued_from not in ISSUED_FROM_VALUES:
        return "issued_from malformed"
    for field in ("allowed_capabilities", "approval_ids"):
        value = turn_lease.get(field)
        if (
            isinstance(value, (str, bytes))
            or not isinstance(value, (tuple, list))
            or any(
                not isinstance(item, str)
                or not item
                or item != item.strip()
                for item in value
            )
        ):
            return f"{field} malformed"
    task_contract_id = turn_lease.get("task_contract_id")
    if task_contract_id is not None and (
        not isinstance(task_contract_id, str)
        or not task_contract_id
        or task_contract_id != task_contract_id.strip()
    ):
        return "task_contract_id malformed"
    issued_at = turn_lease.get("issued_at")
    if not isinstance(issued_at, str) or not issued_at or issued_at != issued_at.strip():
        return "issued_at malformed"
    if issued_from == "user_confirmation" and not turn_lease["approval_ids"]:
        return "user_confirmation has no approval_ids"
    if issued_from != "user_confirmation" and turn_lease["approval_ids"]:
        return "approval_ids require user_confirmation"
    if issued_from == "task_contract":
        if task_contract_id is None:
            return "task_contract requires task_contract_id"
        if mode != "task":
            return "task_contract leases require turn_mode=task"
    elif task_contract_id is not None:
        return "task_contract_id is only valid with issued_from=task_contract"
    return None


class ExternalToolExecutionFence:
    """Read-only external-tool execution decision owner."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        server_registry: ExternalServerRegistry,
        candidate_registry: ExternalToolCandidateRegistry,
        side_effect_policy: ExternalToolSideEffectPolicy,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ExternalExecutionFenceInitializationError(
                "connection must be sqlite3.Connection", code="SERVER_AUTHORITY_MISMATCH"
            )
        if not isinstance(server_registry, ExternalServerRegistry):
            raise ExternalExecutionFenceInitializationError(
                "server registry has the wrong owner type", code="SERVER_AUTHORITY_MISMATCH"
            )
        if not isinstance(candidate_registry, ExternalToolCandidateRegistry):
            raise ExternalExecutionFenceInitializationError(
                "candidate registry has the wrong owner type", code="SERVER_AUTHORITY_MISMATCH"
            )
        if not isinstance(side_effect_policy, ExternalToolSideEffectPolicy):
            raise ExternalExecutionFenceInitializationError(
                "side-effect policy has the wrong owner type", code="SERVER_AUTHORITY_MISMATCH"
            )
        owners = (server_registry, candidate_registry, side_effect_policy)
        if any(getattr(owner, "_connection", None) is not connection for owner in owners):
            raise ExternalExecutionFenceInitializationError(
                "all external fence owners must share one authoritative connection",
                code="SERVER_AUTHORITY_MISMATCH",
            )
        self._connection = connection
        self._server_registry = server_registry
        self._candidate_registry = candidate_registry
        self._side_effect_policy = side_effect_policy

    def evaluate(
        self,
        control_id: Any,
        tool_input: Any,
        turn_lease: Any,
        *,
        expected_turn_id: Any,
    ) -> dict[str, Any]:
        if (
            not isinstance(expected_turn_id, str)
            or not expected_turn_id
            or expected_turn_id != expected_turn_id.strip()
        ):
            return _deny(TURN_ID_MISMATCH)
        lease_error = _lease_error(turn_lease) if turn_lease is not None else None
        if (
            isinstance(turn_lease, Mapping)
            and turn_lease.get("turn_id") != expected_turn_id
        ):
            return _deny(TURN_ID_MISMATCH)
        if (
            isinstance(turn_lease, Mapping)
            and turn_lease.get("turn_mode") != "chat"
        ):
            return _deny(TURN_MODE_NOT_ALLOWED)
        try:
            server_id, tool_name = _parse_control_id(control_id)
            normalized_input, _ = _canonical_tool_input(tool_input)
        except ExternalExecutionFenceValidationError as exc:
            return _deny(exc.code)

        savepoint = "external_execution_fence_snapshot"
        try:
            self._connection.execute(f"SAVEPOINT {savepoint}")
        except sqlite3.Error:
            return _deny(AUTHORITY_READ_FAILED)

        try:
            candidate = self._candidate_registry.get_candidate(server_id, tool_name)
            if candidate is None or candidate.get("control_id") != control_id:
                return _deny(UNKNOWN_CONTROL_ID, control_id=control_id)
            if candidate.get("presence_state") != "PRESENT":
                return _deny(CANDIDATE_NOT_PRESENT, control_id=control_id)

            approval = self._candidate_registry.get_effective_approval(server_id, tool_name)
            if approval.get("effective_approved") is not True:
                return _deny(APPROVAL_NOT_EFFECTIVE, control_id=control_id)

            classification = self._side_effect_policy.get_effective_classification(
                server_id, tool_name
            )
            if classification.get("effective_classified") is not True:
                return _deny(CLASSIFICATION_NOT_EFFECTIVE, control_id=control_id)

            side_effect_class = classification.get("effective_side_effect_class")
            execution_mode = classification.get("effective_execution_mode")
            fingerprint = classification.get("candidate_fingerprint")
            source_revision = classification.get("candidate_source_registry_revision")
            if (
                side_effect_class not in SIDE_EFFECT_CLASSES
                or execution_mode not in {AUTONOMOUS, OWNER_CONFIRMED}
                or not _valid_fingerprint(fingerprint)
                or isinstance(source_revision, bool)
                or not isinstance(source_revision, int)
                or source_revision < 1
            ):
                return _deny(CLASSIFICATION_NOT_EFFECTIVE, control_id=control_id)

            external_action_id = build_external_action_id(
                control_id,
                fingerprint,
                source_revision,
                side_effect_class,
                normalized_input,
            )
            common = {
                "control_id": control_id,
                "server_id": server_id,
                "tool_name": tool_name,
                "external_action_id": external_action_id,
            }
            if side_effect_class == UNKNOWN:
                return _deny(UNKNOWN_SIDE_EFFECT, **common)
            if side_effect_class == CODE_OR_PROCESS:
                return _deny(CODE_OR_PROCESS_DENIED, **common)
            if side_effect_class not in (NONE, EXTERNAL_STATE):
                return _deny(UNKNOWN_SIDE_EFFECT, **common)

            if execution_mode == AUTONOMOUS:
                if turn_lease is not None:
                    return _deny(
                        TURN_LEASE_INVALID
                        if lease_error
                        else AUTONOMOUS_CONFIRMATION_ARTIFACT,
                        **common,
                    )
                allow_turn_id = expected_turn_id
            elif execution_mode == OWNER_CONFIRMED:
                if lease_error:
                    return _deny(TURN_LEASE_INVALID, **common)
                if (
                    turn_lease is None
                    or turn_lease["issued_from"] != "user_confirmation"
                    or external_action_id not in tuple(turn_lease["approval_ids"])
                ):
                    return _deny(OWNER_CONFIRMATION_REQUIRED, **common)
                allow_turn_id = turn_lease["turn_id"]
            else:
                return _deny(EXECUTION_MODE_INVALID, **common)

            return {
                "decision": ALLOW,
                "reason_code": ALLOW_CURRENT_ACTION,
                "control_id": control_id,
                "server_id": server_id,
                "tool_name": tool_name,
                "fingerprint": fingerprint,
                "source_registry_revision": source_revision,
                "side_effect_class": side_effect_class,
                "external_action_id": external_action_id,
                "turn_id": allow_turn_id,
            }
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            return _deny(AUTHORITY_READ_FAILED, control_id=control_id)
        finally:
            try:
                self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except sqlite3.Error:
                pass


__all__ = [
    "ACTION_ID_PREFIX",
    "ACTION_NOT_CONFIRMED",
    "ALLOW",
    "ALLOW_CURRENT_ACTION",
    "APPROVAL_NOT_EFFECTIVE",
    "ASK",
    "AUTHORITY_READ_FAILED",
    "CANDIDATE_NOT_PRESENT",
    "CLASSIFICATION_NOT_EFFECTIVE",
    "CODE_OR_PROCESS_DENIED",
    "DENY",
    "DECISIONS",
    "ExternalExecutionFenceError",
    "ExternalExecutionFenceInitializationError",
    "ExternalExecutionFenceValidationError",
    "ExternalToolExecutionFence",
    "TURN_ID_MISMATCH",
    "TURN_LEASE_FIELDS",
    "TURN_LEASE_INVALID",
    "TURN_MODE_NOT_ALLOWED",
    "UNKNOWN_CONTROL_ID",
    "UNKNOWN_SIDE_EFFECT",
    "build_external_action_id",
]
