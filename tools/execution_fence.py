"""UH-A0 P4 execution fence: provider-neutral pre-execution decisions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tools.capability_manifest import (
    CAPABILITY_MANIFEST,
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)
from tools.lease_signer import ISSUED_FROM_VALUES, TURN_LEASE_FIELDS, TURN_MODES

LEASE_DECISIONS = frozenset(
    {"ALLOW", "DENIED_CAPABILITY", "CAPABILITY_ASK_REQUIRED", "LEASE_MISMATCH"}
)
DEFAULT_TURN_LEASE_FILENAME = ".uh-a0-current-turn-lease.json"
DEFAULT_REPO_ROOT = "/opt/frontend"


def _binding_values(binding: Any) -> tuple[str, ...]:
    if isinstance(binding, str):
        return (binding.strip(),) if binding.strip() else ()
    if isinstance(binding, (tuple, list)):
        return tuple(str(x).strip() for x in binding if str(x).strip())
    return ()


def tool_capability_index() -> dict[str, str]:
    """Compile the provider lookup from the frozen manifest."""
    out: dict[str, str] = {}
    for entry in CAPABILITY_MANIFEST:
        capability_id = str(entry.get("capability_id") or "")
        bindings = entry.get("provider_bindings") or {}
        for tool_name in _binding_values(bindings.get("claude_code")):
            out[tool_name] = capability_id
    return out


def capability_for_tool(tool_name: str) -> str | None:
    return tool_capability_index().get(str(tool_name or "").strip())


def build_approval_id(
    capability_id: str,
    tool_name: str,
    tool_input: Mapping[str, Any] | None,
) -> str:
    payload = {
        "capability_id": str(capability_id),
        "tool_name": str(tool_name),
        "tool_input": dict(tool_input or {}),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "action_sha256:" + hashlib.sha256(canonical).hexdigest()


def _decision(*, capability_id, turn_mode, lease_decision, **extra):
    if lease_decision not in LEASE_DECISIONS:
        raise ValueError(lease_decision)
    result = {
        "capability_id": capability_id,
        "turn_mode": turn_mode,
        "lease_decision": lease_decision,
    }
    result.update(extra)
    return result


def _validate_lease(lease, *, expected_turn_id=None):
    if not isinstance(lease, Mapping):
        return "turn_lease missing"
    if set(lease) != set(TURN_LEASE_FIELDS):
        return "turn_lease field set mismatch"
    if not str(lease.get("turn_id") or "").strip():
        return "turn_id missing"
    if expected_turn_id is not None and str(lease["turn_id"]) != str(expected_turn_id):
        return "turn_id mismatch"
    if lease.get("lease_version") != 1:
        return "lease_version mismatch"
    if str(lease.get("turn_mode") or "") not in TURN_MODES:
        return "turn_mode mismatch"
    if str(lease.get("issued_from") or "") not in ISSUED_FROM_VALUES:
        return "issued_from mismatch"
    for name in ("allowed_capabilities", "approval_ids"):
        value = lease.get(name)
        if (
            isinstance(value, (str, bytes))
            or not isinstance(value, Sequence)
            or any(not isinstance(x, str) or not x for x in value)
        ):
            return f"{name} malformed"
    if lease.get("task_contract_id") is not None and not isinstance(
        lease["task_contract_id"], str
    ):
        return "task_contract_id malformed"
    if not isinstance(lease.get("issued_at"), str) or not lease["issued_at"].strip():
        return "issued_at malformed"
    return None


def evaluate_tool_call(
    tool_name: str,
    tool_input: Mapping[str, Any] | None,
    turn_lease: Mapping[str, Any] | None,
    *,
    expected_turn_id: str | None = None,
) -> dict[str, Any]:
    """Return exactly one frozen lease_decision for one concrete action."""
    error = _validate_lease(turn_lease, expected_turn_id=expected_turn_id)
    if error:
        return _decision(
            capability_id=capability_for_tool(tool_name),
            turn_mode=(
                str(turn_lease.get("turn_mode") or "")
                if isinstance(turn_lease, Mapping) else None
            ),
            lease_decision="LEASE_MISMATCH",
            diagnostic=error,
        )

    capability_id = capability_for_tool(tool_name)
    turn_mode = str(turn_lease["turn_mode"])
    if capability_id is None:
        return _decision(
            capability_id=None, turn_mode=turn_mode,
            lease_decision="DENIED_CAPABILITY",
            diagnostic="unknown tool binding",
        )
    entry = get_capability(capability_id)
    if (
        entry is None
        or capability_id in P1_RESERVED_CAPABILITY_IDS
        or capability_id not in P1_ENABLED_CAPABILITY_IDS
    ):
        return _decision(
            capability_id=capability_id, turn_mode=turn_mode,
            lease_decision="DENIED_CAPABILITY",
            diagnostic="capability is RESERVED or not P1-enabled",
        )

    action_id = build_approval_id(capability_id, tool_name, tool_input)
    allowed = tuple(turn_lease["allowed_capabilities"])
    approvals = tuple(turn_lease["approval_ids"])
    is_write = entry.get("side_effect") == "external_state"

    if capability_id in allowed:
        if is_write and turn_lease["issued_from"] == "user_confirmation":
            if action_id not in approvals:
                return _decision(
                    capability_id=capability_id, turn_mode=turn_mode,
                    lease_decision="LEASE_MISMATCH", approval_id=action_id,
                    diagnostic="confirmation action mismatch",
                )
        return _decision(
            capability_id=capability_id, turn_mode=turn_mode,
            lease_decision="ALLOW", approval_id=action_id if is_write else None,
        )
    if entry.get("autonomy_mode") == "explicit_or_ask":
        return _decision(
            capability_id=capability_id, turn_mode=turn_mode,
            lease_decision="CAPABILITY_ASK_REQUIRED", approval_id=action_id,
            diagnostic="capability requires explicit user confirmation",
        )
    return _decision(
        capability_id=capability_id, turn_mode=turn_mode,
        lease_decision="DENIED_CAPABILITY",
        diagnostic="capability is not in current turn_lease",
    )


def default_turn_lease_path(*, cwd=None, env=None) -> str:
    environ = env or os.environ
    configured = str(environ.get("UH_A0_TURN_LEASE_PATH") or "").strip()
    if configured:
        return configured
    return str(Path(cwd or DEFAULT_REPO_ROOT) / DEFAULT_TURN_LEASE_FILENAME)


def write_current_turn_lease(path, turn_lease, *, session_id=None) -> str:
    error = _validate_lease(turn_lease)
    if error:
        raise ValueError(error)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"lease": dict(turn_lease), "turn_id": str(turn_lease["turn_id"])}
    if session_id is not None:
        record["session_id"] = str(session_id)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(target.parent),
        prefix=f".{target.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(record, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            temporary.chmod(0o600)
    os.replace(temporary, target)
    return str(target)


def read_current_turn_lease(path=None, *, env=None):
    target = Path(path or default_turn_lease_path(env=env))
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None, {}
    if not isinstance(raw, Mapping):
        return None, {}
    if isinstance(raw.get("lease"), Mapping):
        return dict(raw["lease"]), dict(raw)
    return dict(raw), dict(raw)


def clear_current_turn_lease(path=None):
    try:
        Path(path or default_turn_lease_path()).unlink()
    except FileNotFoundError:
        pass


class UH_A0TurnRuntime:
    """Trusted per-turn lease owner for the existing resident stream."""

    def __init__(self, path=None, *, session_id=None):
        self.path = str(path or default_turn_lease_path())
        self.session_id = None if session_id is None else str(session_id)
        self.active_turn_id = None

    def start_turn(self, turn_lease, *, session_id=None):
        """Atomically install exactly this turn's lease before tool use."""
        if session_id is not None:
            self.session_id = str(session_id)
        write_current_turn_lease(
            self.path,
            turn_lease,
            session_id=self.session_id,
        )
        self.active_turn_id = str(turn_lease["turn_id"])
        return self.active_turn_id

    def _record_matches_active_turn(self, record):
        return (
            self.active_turn_id is not None
            and str(record.get("turn_id") or "") == str(self.active_turn_id)
        )

    def end_turn(self, *, turn_id=None):
        """Clear only the lease installed by this runtime turn."""
        lease, record = read_current_turn_lease(self.path)
        expected = self.active_turn_id if turn_id is None else str(turn_id)
        cleared = bool(
            expected
            and str(record.get("turn_id") or "") == str(expected)
            and (
                self.active_turn_id is None
                or str(self.active_turn_id) == str(expected)
            )
        )
        if cleared:
            clear_current_turn_lease(self.path)
        self.active_turn_id = None
        return cleared

    def abort_turn(self, *, turn_id=None):
        return self.end_turn(turn_id=turn_id)

    def evaluate(self, tool_name, tool_input):
        lease, record = read_current_turn_lease(self.path)
        if (
            self.session_id is not None
            and record.get("session_id") is not None
            and str(record.get("session_id")) != str(self.session_id)
        ):
            result = _decision(
                capability_id=capability_for_tool(tool_name),
                turn_mode=str(lease.get("turn_mode") or "") if lease else None,
                lease_decision="LEASE_MISMATCH",
                diagnostic="session_id mismatch",
            )
            return result
        return evaluate_tool_call(
            tool_name,
            tool_input,
            lease,
            expected_turn_id=record.get("turn_id"),
        )

    def deferred_tool_use(self, tool_name, tool_input):
        """Return the existing stream payload for a concrete deferred action."""
        result = self.evaluate(tool_name, tool_input)
        payload = dict(result)
        payload.update({
            "event": "deferred_tool_use",
            "tool_name": str(tool_name),
            "tool_input": dict(tool_input or {}),
        })
        return payload


def pretooluse_payload(result):
    decision = str(result.get("lease_decision") or "LEASE_MISMATCH")
    provider_decision = (
        "allow" if decision == "ALLOW"
        else "defer" if decision == "CAPABILITY_ASK_REQUIRED"
        else "deny"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": provider_decision,
            "permissionDecisionReason": (
                f"UH-A0 {decision}: {result.get('capability_id') or 'unknown'}"
            ),
        }
    }


def _hook_result(payload, env):
    lease, record = read_current_turn_lease(env=env)
    tool_name = str(payload.get("tool_name") or "")
    if (
        payload.get("session_id")
        and record.get("session_id")
        and str(payload["session_id"]) != str(record["session_id"])
    ):
        result = _decision(
            capability_id=capability_for_tool(tool_name),
            turn_mode=str(lease.get("turn_mode") or "") if lease else None,
            lease_decision="LEASE_MISMATCH",
            diagnostic="session_id mismatch",
        )
    else:
        result = evaluate_tool_call(
            tool_name,
            payload.get("tool_input") if isinstance(payload.get("tool_input"), Mapping) else {},
            lease,
            expected_turn_id=record.get("turn_id"),
        )
    return pretooluse_payload(result)


def _verify_json(payload, env):
    lease = payload.get("turn_lease")
    record = {}
    if not isinstance(lease, Mapping):
        lease, record = read_current_turn_lease(env=env)
    return evaluate_tool_call(
        str(payload.get("tool_name") or ""),
        payload.get("tool_input") if isinstance(payload.get("tool_input"), Mapping) else {},
        lease,
        expected_turn_id=record.get("turn_id"),
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("pretooluse", "verify-json"))
    args = parser.parse_args(argv)
    try:
        payload = json.loads(os.sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    output = (
        _hook_result(payload, os.environ)
        if args.mode == "pretooluse"
        else _verify_json(payload, os.environ)
    )
    os.sys.stdout.write(json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
