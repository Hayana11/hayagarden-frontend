"""UH-A0 v1.0 turn lease signer (P2).

Issues a provider-neutral turn_lease for one turn.  This module does *not*
execute tools, expose ToolSearch/PreToolUse, enforce MCP fences, merge
Chat/Wake residents, or persist leases.  Those are later UH-A0 slices.

Frozen contract (2026-08-12):
- turn_lease uses exactly eight frozen field names;
- issued_from is one of four allowed sources (model is never an issuer);
- Chat / Wake / Task default_policy sets come from §5.3;
- every turn is signed independently — no lease inheritance;
- capability IDs come only from capability_manifest;
- P1 RESERVED capabilities fail closed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from tools.capability_manifest import (
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)

LEASE_VERSION = 1
EXTERNAL_AUTONOMOUS_POLICY = "external_autonomous_policy"

TURN_LEASE_FIELDS = (
    "lease_version",
    "turn_id",
    "turn_mode",
    "issued_from",
    "allowed_capabilities",
    "approval_ids",
    "task_contract_id",
    "issued_at",
)

ISSUED_FROM_VALUES = frozenset(
    {
        "default_policy",
        "explicit_user_intent",
        "user_confirmation",
        EXTERNAL_AUTONOMOUS_POLICY,
        "task_contract",
    }
)

TURN_MODES = frozenset({"chat", "wake", "task"})

# Frozen §5.3 P1 default lease policy (exact capability sets).
DEFAULT_ALLOWED_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "chat": (
        "memory.search",
        "memory.write",
        "diary.write",
        "home.light.status",
        "todo.read",
        "todo.write",
        "countdown.read",
        "task.timer.start",
        "ledger.read",
        "ledger.budget.read",
        "ledger.write",
    ),
    "wake": (
        "memory.search",
        "home.light.status",
        "todo.read",
        "countdown.read",
        "ledger.read",
        "ledger.budget.read",
    ),
    "task": (
        "files.read",
        "files.find",
        "code.search",
    ),
}


class LeaseSignError(ValueError):
    """Fail-closed signing error with a frozen diagnostic code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)


def default_allowed_capabilities(turn_mode: str) -> tuple[str, ...]:
    """Return the frozen §5.3 default capability tuple for a turn_mode."""
    mode = str(turn_mode or "")
    if mode not in DEFAULT_ALLOWED_CAPABILITIES:
        raise LeaseSignError("DENIED_CAPABILITY", f"unknown turn_mode: {mode!r}")
    return DEFAULT_ALLOWED_CAPABILITIES[mode]


def issue_turn_lease(
    *,
    turn_id: str,
    turn_mode: str,
    issued_from: str,
    requested_capabilities: Sequence[str] | None = None,
    approval_ids: Sequence[str] | None = None,
    task_contract_id: str | None = None,
    issued_at: str | None = None,
    previous_lease: Any = None,
) -> dict[str, Any]:
    """Sign one independent turn_lease.

    ``previous_lease`` is accepted only so callers cannot accidentally smuggle
    inheritance through an undocumented kwarg path: any non-None value is
    rejected.  There is no copy/union from a prior lease.
    """
    if previous_lease is not None:
        raise LeaseSignError(
            "LEASE_MISMATCH",
            "lease inheritance is forbidden; previous_lease must not be supplied",
        )

    tid = str(turn_id or "").strip()
    if not tid:
        raise LeaseSignError("LEASE_MISMATCH", "turn_id is required")

    mode = str(turn_mode or "").strip()
    if mode not in TURN_MODES:
        raise LeaseSignError("DENIED_CAPABILITY", f"unknown turn_mode: {mode!r}")

    source = str(issued_from or "").strip()
    if source not in ISSUED_FROM_VALUES:
        raise LeaseSignError(
            "DENIED_CAPABILITY",
            f"issued_from not allowed: {source!r}",
        )

    approvals = _normalize_ids(approval_ids)
    contract_id = _normalize_optional_id(task_contract_id)
    extras = _normalize_ids(requested_capabilities)

    if source in {"user_confirmation", EXTERNAL_AUTONOMOUS_POLICY} and not approvals:
        raise LeaseSignError(
            "LEASE_MISMATCH",
            f"{source} requires approval_ids",
        )
    if source not in {"user_confirmation", EXTERNAL_AUTONOMOUS_POLICY} and approvals:
        raise LeaseSignError(
            "LEASE_MISMATCH",
            "approval_ids require a confirmation or autonomous external lease",
        )

    if source == "task_contract":
        if not contract_id:
            raise LeaseSignError(
                "LEASE_MISMATCH",
                "task_contract requires task_contract_id",
            )
        if mode != "task":
            raise LeaseSignError(
                "LEASE_MISMATCH",
                "task_contract leases require turn_mode=task",
            )
    elif contract_id is not None:
        raise LeaseSignError(
            "LEASE_MISMATCH",
            "task_contract_id is only valid with issued_from=task_contract",
        )

    if source == "default_policy":
        if extras:
            raise LeaseSignError(
                "DENIED_CAPABILITY",
                "default_policy cannot add requested_capabilities",
            )
        allowed = list(default_allowed_capabilities(mode))
    else:
        allowed = list(default_allowed_capabilities(mode))
        for capability_id in extras:
            _assert_grantable(capability_id, issued_from=source)
            if capability_id not in allowed:
                allowed.append(capability_id)

    stamp = str(issued_at or "").strip() or _utc_now_iso()

    lease = {
        "lease_version": LEASE_VERSION,
        "turn_id": tid,
        "turn_mode": mode,
        "issued_from": source,
        "allowed_capabilities": tuple(allowed),
        "approval_ids": approvals,
        "task_contract_id": contract_id,
        "issued_at": stamp,
    }
    if set(lease) != set(TURN_LEASE_FIELDS):
        raise LeaseSignError("LEASE_MISMATCH", "turn_lease field set drifted")
    return lease


def issue_external_autonomous_lease(
    *,
    turn_id: str,
    external_action_id: str,
    issued_at: str | None = None,
) -> dict[str, Any]:
    """Issue the only supported lease for an approved autonomous external action."""
    if (
        not isinstance(external_action_id, str)
        or not external_action_id
        or external_action_id != external_action_id.strip()
    ):
        raise LeaseSignError("LEASE_MISMATCH", "external_action_id is required")
    return issue_turn_lease(
        turn_id=turn_id,
        turn_mode="chat",
        issued_from=EXTERNAL_AUTONOMOUS_POLICY,
        requested_capabilities=(),
        approval_ids=(external_action_id,),
        issued_at=issued_at,
    )


def _assert_grantable(capability_id: str, *, issued_from: str) -> None:
    entry = get_capability(capability_id)
    if entry is None:
        raise LeaseSignError(
            "DENIED_CAPABILITY",
            f"unknown capability_id: {capability_id!r}",
        )
    if capability_id in P1_RESERVED_CAPABILITY_IDS:
        raise LeaseSignError(
            "DENIED_CAPABILITY",
            f"P1 RESERVED capability cannot be signed: {capability_id!r}",
        )
    if capability_id not in P1_ENABLED_CAPABILITY_IDS:
        raise LeaseSignError(
            "DENIED_CAPABILITY",
            f"capability is not P1-enabled: {capability_id!r}",
        )

    autonomy = entry["autonomy_mode"]
    if autonomy == "never_auto":
        raise LeaseSignError(
            "DENIED_CAPABILITY",
            f"never_auto capability cannot be signed: {capability_id!r}",
        )
    if issued_from in {"explicit_user_intent", "user_confirmation"}:
        if autonomy == "task_only":
            raise LeaseSignError(
                "DENIED_CAPABILITY",
                f"task_only capability requires task_contract: {capability_id!r}",
            )
        if autonomy not in {"self_write_auto", "read_auto"}:
            raise LeaseSignError(
                "DENIED_CAPABILITY",
                f"capability autonomy incompatible with {issued_from}: {capability_id!r}",
            )
    elif issued_from == "task_contract":
        # task_only / read_auto / self_write_auto may be appended by contract;
        # RESERVED (including code.write / workspace.execute) already failed above.
        if autonomy not in {"task_only", "read_auto", "self_write_auto"}:
            raise LeaseSignError(
                "DENIED_CAPABILITY",
                f"capability autonomy incompatible with task_contract: {capability_id!r}",
            )


def _normalize_ids(values: Sequence[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    out: list[str] = []
    for raw in values:
        item = str(raw or "").strip()
        if not item:
            raise LeaseSignError("LEASE_MISMATCH", "empty id is not allowed")
        if item not in out:
            out.append(item)
    return tuple(out)


def _normalize_optional_id(value: str | None) -> str | None:
    if value is None:
        return None
    item = str(value).strip()
    return item or None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _iter_module_export_names() -> Iterable[str]:
    return (
        "LEASE_VERSION",
        "TURN_LEASE_FIELDS",
        "ISSUED_FROM_VALUES",
        "EXTERNAL_AUTONOMOUS_POLICY",
        "TURN_MODES",
        "DEFAULT_ALLOWED_CAPABILITIES",
        "LeaseSignError",
        "default_allowed_capabilities",
        "issue_turn_lease",
        "issue_external_autonomous_lease",
    )
