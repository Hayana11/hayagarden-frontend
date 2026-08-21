"""Persistent provider-neutral runtime state for enabled capabilities.

The static capability manifest remains the eligibility source of truth. This
module stores only an optional ON/OFF overlay keyed by capability_id.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from typing import Any

from tools.capability_manifest import (
    CAPABILITY_MANIFEST,
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)

CAPABILITY_STATE_KEY = "CAPABILITY_RUNTIME_STATE_V1"
SCHEMA_VERSION = 1

RUNTIME_STATE_INHERIT = "INHERIT"
RUNTIME_STATE_ON = "ON"
RUNTIME_STATE_OFF = "OFF"
RUNTIME_STATE_DENY = "DENY"
_VALID_RUNTIME_STATES = frozenset(
    {RUNTIME_STATE_ON, RUNTIME_STATE_OFF}
)

# Keep the same DB as config_store without importing it at module import time:
# importing config_store would initialize the default production DB in tests.
DB_PATH = os.getenv("HAYAGARDEN_CONFIG_DB_PATH", "/opt/frontend/memories.db")


class CapabilityStateError(RuntimeError):
    """The runtime state could not be read or trusted."""


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=3)


def _empty_document() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "states": {}}


def _validate_document(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CapabilityStateError("runtime capability state must be an object")
    if set(raw) != {"schema_version", "states"}:
        raise CapabilityStateError("runtime capability state schema mismatch")
    version = raw.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise CapabilityStateError("runtime capability state version mismatch")
    states = raw.get("states")
    if not isinstance(states, Mapping):
        raise CapabilityStateError("runtime capability states must be an object")

    normalized: dict[str, str] = {}
    for capability_id, state in states.items():
        if (
            not isinstance(capability_id, str)
            or not capability_id.strip()
            or capability_id not in P1_ENABLED_CAPABILITY_IDS
            or capability_id in P1_RESERVED_CAPABILITY_IDS
            or state not in _VALID_RUNTIME_STATES
        ):
            raise CapabilityStateError("runtime capability state contains an invalid entry")
        normalized[capability_id] = state
    return {"schema_version": SCHEMA_VERSION, "states": normalized}


def _load_document(conn: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
    row = conn.execute(
        "SELECT value FROM runtime_config WHERE key=?",
        (CAPABILITY_STATE_KEY,),
    ).fetchone()
    if row is None:
        return _empty_document(), False
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapabilityStateError(
            "runtime capability state is malformed"
        ) from exc
    return _validate_document(raw), True


def _qualified_for_runtime_state(capability_id: str) -> bool:
    return (
        get_capability(capability_id) is not None
        and capability_id in P1_ENABLED_CAPABILITY_IDS
        and capability_id not in P1_RESERVED_CAPABILITY_IDS
    )


def read_capability_state(capability_id: str) -> str:
    """Read one overlay state, distinguishing missing from storage failure."""
    capability_id = str(capability_id or "")
    if not _qualified_for_runtime_state(capability_id):
        return RUNTIME_STATE_DENY

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        document, _present = _load_document(conn)
        return document["states"].get(capability_id, RUNTIME_STATE_INHERIT)
    except CapabilityStateError:
        raise
    except Exception as exc:
        raise CapabilityStateError(
            "runtime capability state storage is unavailable"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def effective_capability_state(capability_id: str) -> str:
    """Return DENY for static ineligibility, otherwise overlay state."""
    if not _qualified_for_runtime_state(str(capability_id or "")):
        return RUNTIME_STATE_DENY
    return read_capability_state(capability_id)


def capability_state_snapshot() -> list[dict[str, Any]]:
    """Return one consistent, read-only capability state snapshot."""
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        document, _present = _load_document(conn)
        states: list[dict[str, Any]] = []
        for capability in CAPABILITY_MANIFEST:
            capability_id = str(capability["capability_id"])
            static_enabled = capability_id in P1_ENABLED_CAPABILITY_IDS
            writable = _qualified_for_runtime_state(capability_id)
            if not writable:
                runtime_state = RUNTIME_STATE_DENY
                effective_enabled = False
            else:
                runtime_state = document["states"].get(
                    capability_id, RUNTIME_STATE_INHERIT
                )
                effective_enabled = runtime_state in {
                    RUNTIME_STATE_INHERIT,
                    RUNTIME_STATE_ON,
                }
            states.append(
                {
                    "capability_id": capability_id,
                    "static_enabled": static_enabled,
                    "runtime_state": runtime_state,
                    "effective_enabled": effective_enabled,
                    "writable": writable,
                }
            )
        return states
    except CapabilityStateError:
        raise
    except Exception as exc:
        raise CapabilityStateError(
            "runtime capability state storage is unavailable"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def _assert_writable_capability(capability_id: str) -> str:
    capability_id = str(capability_id or "")
    if get_capability(capability_id) is None:
        raise ValueError("unknown capability_id")
    if (
        capability_id in P1_RESERVED_CAPABILITY_IDS
        or capability_id not in P1_ENABLED_CAPABILITY_IDS
    ):
        raise ValueError("capability_id is not statically P1-enabled")
    return capability_id


def set_capability_state(capability_id: str, *, enabled: bool) -> None:
    """Atomically persist ON/OFF for one statically enabled capability."""
    capability_id = _assert_writable_capability(capability_id)
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be bool")

    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        document, _present = _load_document(conn)
        document["states"][capability_id] = (
            RUNTIME_STATE_ON if enabled else RUNTIME_STATE_OFF
        )
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO runtime_config (key, value, updated_at) "
            "VALUES (?, ?, datetime('now','+8 hours')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (CAPABILITY_STATE_KEY, encoded),
        )
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()
