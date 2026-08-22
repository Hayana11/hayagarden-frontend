"""Provider-neutral durable confirmation state for M4-01B1.

This module owns only the confirmation interval between a canonical ASK and
future side-effect execution.  It deliberately contains no Todo handler, HTTP
client, or product-specific result fields.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping


PENDING_ACTION_TTL = timedelta(minutes=10)
PENDING_STATES = frozenset({"pending", "approved", "rejected", "expired", "completed"})
TERMINAL_STATES = frozenset({"rejected", "expired", "completed"})
CONFIRMATION_DECISIONS = frozenset({"approve", "reject"})


class ConfirmationError(ValueError):
    """Fail-closed confirmation error with a stable diagnostic code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


class PendingActionStoreError(ConfirmationError):
    pass


@dataclass(frozen=True)
class PendingAction:
    pending_action_id: str
    approval_id: str
    capability_id: str
    tool_name: str
    tool_input: dict[str, Any]
    owner_id: str
    turn_id: str
    created_at: str
    expires_at: str
    state: str
    confirmed_at: str | None = None
    rejected_at: str | None = None
    completed_at: str | None = None
    tool_use_id: str | None = None


@dataclass(frozen=True)
class ConfirmedActionContext:
    action: PendingAction
    turn_lease: Mapping[str, Any]
    evaluation: Mapping[str, Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        current = value
    else:
        current = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _stamp(value: datetime | str | None = None) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _canonical_input(tool_input: Mapping[str, Any] | None) -> str:
    if not isinstance(tool_input, Mapping):
        raise ConfirmationError("MALFORMED_ACTION", "tool_input must be an object")
    try:
        return json.dumps(
            dict(tool_input),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ConfirmationError("MALFORMED_ACTION", "tool_input is not JSON-safe") from exc


def _nonempty(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ConfirmationError("MALFORMED_REQUEST", f"{name} is required")
    return result


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create only the generic pending-action schema, idempotently."""
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS confirmation_pending_actions (
                pending_action_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tool_input_json TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN (
                    'pending', 'approved', 'rejected', 'expired', 'completed'
                )),
                confirmed_at TEXT,
                rejected_at TEXT,
                completed_at TEXT,
                tool_use_id TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_confirmation_pending_owner_state
            ON confirmation_pending_actions (owner_id, state)
            """
        )
        conn.commit()
    except sqlite3.Error as exc:
        raise PendingActionStoreError("STORE_UNAVAILABLE", "pending schema unavailable") from exc


class PendingActionStore:
    """SQLite-backed generic pending action store.

    ``owner_id`` must be supplied by trusted backend request context.  It is
    never read from a user-provided tool input or used as an authorization
    substitute for the pending row's immutable action snapshot.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ttl: timedelta = PENDING_ACTION_TTL,
        id_factory: Callable[[], str] | None = None,
        approval_builder: Callable[[str, str, Mapping[str, Any]], str] | None = None,
        lease_issuer: Callable[..., Mapping[str, Any]] | None = None,
        runtime_evaluator: Callable[[str, Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] | None = None,
    ):
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.conn = conn
        self.ttl = ttl
        self.id_factory = id_factory or (lambda: secrets.token_urlsafe(32))
        self._approval_builder = approval_builder
        self._lease_issuer = lease_issuer
        self._runtime_evaluator = runtime_evaluator
        ensure_schema(conn)

    def _build_approval_id(self, capability_id: str, tool_name: str, tool_input: Mapping[str, Any]) -> str:
        if self._approval_builder is not None:
            return str(self._approval_builder(capability_id, tool_name, tool_input))
        from tools.execution_fence import build_approval_id
        return build_approval_id(capability_id, tool_name, tool_input)

    def _new_id(self) -> str:
        for _ in range(4):
            candidate = _nonempty("pending_action_id", self.id_factory())
            try:
                self.conn.execute(
                    "SELECT 1 FROM confirmation_pending_actions WHERE pending_action_id=?",
                    (candidate,),
                )
            except sqlite3.Error as exc:
                raise PendingActionStoreError("STORE_UNAVAILABLE", "pending store read failed") from exc
            row = self.conn.execute(
                "SELECT 1 FROM confirmation_pending_actions WHERE pending_action_id=?",
                (candidate,),
            ).fetchone()
            if row is None:
                return candidate
        raise PendingActionStoreError("STORE_UNAVAILABLE", "could not allocate action instance id")

    def create_pending_action(
        self,
        *,
        capability_id: str,
        tool_name: str,
        tool_input: Mapping[str, Any],
        owner_id: str,
        turn_id: str,
        now: datetime | str | None = None,
        tool_use_id: str | None = None,
    ) -> PendingAction:
        capability = _nonempty("capability_id", capability_id)
        tool = _nonempty("tool_name", tool_name)
        owner = _nonempty("owner_id", owner_id)
        turn = _nonempty("turn_id", turn_id)
        canonical = _canonical_input(tool_input)
        parsed_input = json.loads(canonical)
        approval_id = self._build_approval_id(capability, tool, parsed_input)
        created = _as_utc(now)
        expires = created + self.ttl
        try:
            pending_id = self._new_id()
            self.conn.execute(
                """
                INSERT INTO confirmation_pending_actions (
                    pending_action_id, approval_id, capability_id, tool_name,
                    tool_input_json, owner_id, turn_id, created_at, expires_at,
                    state, tool_use_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    pending_id, approval_id, capability, tool, canonical, owner,
                    turn, _stamp(created), _stamp(expires),
                    None if tool_use_id is None else str(tool_use_id),
                ),
            )
            self.conn.commit()
            return PendingAction(
                pending_action_id=pending_id,
                approval_id=approval_id,
                capability_id=capability,
                tool_name=tool,
                tool_input=parsed_input,
                owner_id=owner,
                turn_id=turn,
                created_at=_stamp(created),
                expires_at=_stamp(expires),
                state="pending",
                tool_use_id=None if tool_use_id is None else str(tool_use_id),
            )
        except ConfirmationError:
            _rollback(self.conn)
            raise
        except sqlite3.Error as exc:
            _rollback(self.conn)
            raise PendingActionStoreError("STORE_UNAVAILABLE", "pending action write failed") from exc

    def get(self, pending_action_id: str, *, now: datetime | str | None = None) -> PendingAction:
        pending_id = _nonempty("pending_action_id", pending_action_id)
        try:
            row = self.conn.execute(
                "SELECT * FROM confirmation_pending_actions WHERE pending_action_id=?",
                (pending_id,),
            ).fetchone()
            if row is None:
                raise ConfirmationError("UNKNOWN_PENDING_ACTION", "unknown pending_action_id")
            action = self._row_to_action(row)
            if action.state == "pending" and _as_utc(now) >= _as_utc(action.expires_at):
                self.conn.execute(
                    "UPDATE confirmation_pending_actions SET state='expired' WHERE pending_action_id=? AND state='pending'",
                    (pending_id,),
                )
                self.conn.commit()
                action = self._row_to_action(
                    self.conn.execute(
                        "SELECT * FROM confirmation_pending_actions WHERE pending_action_id=?",
                        (pending_id,),
                    ).fetchone()
                )
            return action
        except ConfirmationError:
            raise
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PendingActionStoreError("STORE_UNAVAILABLE", "pending store read failed") from exc

    def _row_to_action(self, row: sqlite3.Row | tuple[Any, ...]) -> PendingAction:
        try:
            values = dict(row) if isinstance(row, sqlite3.Row) else {
                "pending_action_id": row[0], "approval_id": row[1], "capability_id": row[2],
                "tool_name": row[3], "tool_input_json": row[4], "owner_id": row[5],
                "turn_id": row[6], "created_at": row[7], "expires_at": row[8],
                "state": row[9], "confirmed_at": row[10], "rejected_at": row[11],
                "completed_at": row[12], "tool_use_id": row[13],
            }
            tool_input = json.loads(values["tool_input_json"])
            if not isinstance(tool_input, dict) or values["state"] not in PENDING_STATES:
                raise ValueError("malformed pending row")
            action = PendingAction(
                pending_action_id=_nonempty("pending_action_id", values["pending_action_id"]),
                approval_id=_nonempty("approval_id", values["approval_id"]),
                capability_id=_nonempty("capability_id", values["capability_id"]),
                tool_name=_nonempty("tool_name", values["tool_name"]),
                tool_input=tool_input,
                owner_id=_nonempty("owner_id", values["owner_id"]),
                turn_id=_nonempty("turn_id", values["turn_id"]),
                created_at=_nonempty("created_at", values["created_at"]),
                expires_at=_nonempty("expires_at", values["expires_at"]),
                state=str(values["state"]),
                confirmed_at=values["confirmed_at"],
                rejected_at=values["rejected_at"],
                completed_at=values["completed_at"],
                tool_use_id=values["tool_use_id"],
            )
            expected = self._build_approval_id(action.capability_id, action.tool_name, action.tool_input)
            if expected != action.approval_id:
                raise ValueError("approval identity mismatch")
            return action
        except ConfirmationError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PendingActionStoreError("MALFORMED_ROW", "pending action row is invalid") from exc

    def _validate_request(self, request: Mapping[str, Any]) -> tuple[str, str, str]:
        if not isinstance(request, Mapping):
            raise ConfirmationError("MALFORMED_REQUEST", "confirmation request must be an object")
        pending_id = _nonempty("pending_action_id", request.get("pending_action_id"))
        approval_id = _nonempty("approval_id", request.get("approval_id"))
        decision = _nonempty("confirmation_decision", request.get("confirmation_decision"))
        if decision not in CONFIRMATION_DECISIONS:
            raise ConfirmationError("MALFORMED_REQUEST", "confirmation_decision is invalid")
        return pending_id, approval_id, decision

    def _load_owned(self, pending_id: str, approval_id: str, owner_id: str, now: datetime | str | None) -> PendingAction:
        action = self.get(pending_id, now=now)
        if action.owner_id != _nonempty("owner_id", owner_id):
            raise ConfirmationError("OWNERSHIP_MISMATCH", "pending action owner mismatch")
        if action.approval_id != approval_id:
            raise ConfirmationError("APPROVAL_MISMATCH", "approval_id does not match pending action")
        if action.state == "expired":
            raise ConfirmationError("EXPIRED", "pending action expired")
        return action

    def _issue_confirmation_lease(self, action: PendingAction) -> Mapping[str, Any]:
        issuer = self._lease_issuer
        if issuer is None:
            from tools.lease_signer import issue_turn_lease
            issuer = issue_turn_lease
        try:
            lease = issuer(
                turn_id=f"confirmation-{secrets.token_hex(16)}",
                turn_mode="chat",
                issued_from="user_confirmation",
                requested_capabilities=(action.capability_id,),
                approval_ids=(action.approval_id,),
            )
        except Exception as exc:
            raise ConfirmationError("LEASE_MISMATCH", "could not issue confirmation lease") from exc
        if not isinstance(lease, Mapping):
            raise ConfirmationError("LEASE_MISMATCH", "confirmation lease is malformed")
        if action.capability_id not in tuple(lease.get("allowed_capabilities") or ()):
            raise ConfirmationError("DENIED_CAPABILITY", "confirmed capability missing from lease")
        if tuple(lease.get("approval_ids") or ()) != (action.approval_id,):
            raise ConfirmationError("LEASE_MISMATCH", "confirmation approval identity drifted")
        if lease.get("issued_from") != "user_confirmation":
            raise ConfirmationError("LEASE_MISMATCH", "lease issuer is not user_confirmation")
        return dict(lease)

    def _recheck_runtime(self, action: PendingAction, lease: Mapping[str, Any]) -> Mapping[str, Any]:
        evaluator = self._runtime_evaluator
        if evaluator is None:
            from tools.execution_fence import evaluate_tool_call
            evaluator = evaluate_tool_call
        try:
            result = evaluator(action.tool_name, action.tool_input, lease)
        except Exception as exc:
            raise ConfirmationError("RUNTIME_UNAVAILABLE", "capability runtime state unavailable") from exc
        if not isinstance(result, Mapping) or result.get("lease_decision") != "ALLOW":
            raise ConfirmationError("DENIED_CAPABILITY", "capability is not currently executable")
        return dict(result)

    def confirm(self, request: Mapping[str, Any], *, owner_id: str, now: datetime | str | None = None) -> ConfirmedActionContext:
        pending_id, approval_id, decision = self._validate_request(request)
        if decision != "approve":
            raise ConfirmationError("MALFORMED_REQUEST", "confirm requires approve")
        try:
            action = self._load_owned(pending_id, approval_id, owner_id, now)
            if action.state != "pending":
                raise ConfirmationError("STATE_CONFLICT", "pending action is not confirmable")
            lease = self._issue_confirmation_lease(action)
            evaluation = self._recheck_runtime(action, lease)
            stamp = _stamp(now)
            updated = self.conn.execute(
                """
                UPDATE confirmation_pending_actions
                SET state='approved', confirmed_at=?
                WHERE pending_action_id=? AND state='pending'
                """,
                (stamp, pending_id),
            )
            if updated.rowcount != 1:
                _rollback(self.conn)
                raise ConfirmationError("STATE_CONFLICT", "pending action changed concurrently")
            self.conn.commit()
            return ConfirmedActionContext(
                action=self.get(pending_id, now=now),
                turn_lease=lease,
                evaluation=evaluation,
            )
        except ConfirmationError:
            _rollback(self.conn)
            raise
        except sqlite3.Error as exc:
            _rollback(self.conn)
            raise PendingActionStoreError("STORE_UNAVAILABLE", "confirmation state write failed") from exc

    def resume_approved(
        self,
        request: Mapping[str, Any],
        *,
        owner_id: str,
        now: datetime | str | None = None,
    ) -> ConfirmedActionContext:
        """Revalidate an approved action after a gateway crash or lost response."""
        pending_id, approval_id, decision = self._validate_request(request)
        if decision != "approve":
            raise ConfirmationError("MALFORMED_REQUEST", "resume requires approve")
        try:
            action = self._load_owned(pending_id, approval_id, owner_id, now)
            if action.state != "approved":
                raise ConfirmationError("STATE_CONFLICT", "pending action is not approved")
            lease = self._issue_confirmation_lease(action)
            evaluation = self._recheck_runtime(action, lease)
            return ConfirmedActionContext(
                action=self.get(pending_id, now=now),
                turn_lease=lease,
                evaluation=evaluation,
            )
        except ConfirmationError:
            _rollback(self.conn)
            raise
        except sqlite3.Error as exc:
            _rollback(self.conn)
            raise PendingActionStoreError("STORE_UNAVAILABLE", "approved action resume failed") from exc

    def reject(self, request: Mapping[str, Any], *, owner_id: str, now: datetime | str | None = None) -> PendingAction:
        pending_id, approval_id, decision = self._validate_request(request)
        if decision != "reject":
            raise ConfirmationError("MALFORMED_REQUEST", "reject requires reject")
        try:
            action = self._load_owned(pending_id, approval_id, owner_id, now)
            if action.state == "rejected":
                return action
            if action.state != "pending":
                raise ConfirmationError("STATE_CONFLICT", "pending action is not rejectable")
            stamp = _stamp(now)
            updated = self.conn.execute(
                """
                UPDATE confirmation_pending_actions
                SET state='rejected', rejected_at=?
                WHERE pending_action_id=? AND state='pending'
                """,
                (stamp, pending_id),
            )
            if updated.rowcount != 1:
                _rollback(self.conn)
                raise ConfirmationError("STATE_CONFLICT", "pending action changed concurrently")
            self.conn.commit()
            return self.get(pending_id, now=now)
        except ConfirmationError:
            _rollback(self.conn)
            raise
        except sqlite3.Error as exc:
            _rollback(self.conn)
            raise PendingActionStoreError("STORE_UNAVAILABLE", "rejection state write failed") from exc

    def mark_completed(self, pending_action_id: str, *, owner_id: str, now: datetime | str | None = None) -> PendingAction:
        """Future B2 terminal primitive; it never performs a product side effect."""
        try:
            action = self.get(pending_action_id, now=now)
            if action.owner_id != _nonempty("owner_id", owner_id):
                raise ConfirmationError("OWNERSHIP_MISMATCH", "pending action owner mismatch")
            if action.state != "approved":
                raise ConfirmationError("STATE_CONFLICT", "only approved actions can complete")
            self.conn.execute(
                "UPDATE confirmation_pending_actions SET state='completed', completed_at=? WHERE pending_action_id=? AND state='approved'",
                (_stamp(now), action.pending_action_id),
            )
            self.conn.commit()
            return self.get(action.pending_action_id, now=now)
        except ConfirmationError:
            _rollback(self.conn)
            raise
        except sqlite3.Error as exc:
            _rollback(self.conn)
            raise PendingActionStoreError("STORE_UNAVAILABLE", "completion state write failed") from exc


def deferred_payload(action: PendingAction, *, approval_prompt: str | None = None) -> dict[str, Any]:
    """Build the provider-neutral deferred payload without executing anything."""
    payload: dict[str, Any] = {
        "name": action.tool_name,
        "args": dict(action.tool_input),
        "tool_input": dict(action.tool_input),
        "approval_id": action.approval_id,
        "pending_action_id": action.pending_action_id,
        "deferred_tool_use": True,
        "status": "waiting_for_confirmation",
    }
    if approval_prompt is not None:
        payload["approval_prompt"] = str(approval_prompt)
    if action.tool_use_id is not None:
        payload["id"] = action.tool_use_id
    return payload
