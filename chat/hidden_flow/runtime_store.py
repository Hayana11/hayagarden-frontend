"""SQLite persistence for the opt-in Hidden Flow runtime.

This module is deliberately separate from the provider-neutral engine.  It
only stores normalized configuration, one runtime row per chat, and the
pending/committed assistant snapshot used by Daily's existing transactions.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
from typing import Any, Mapping, Optional

from .config import normalize_flow_config
from .types import AppliedGuide, FlowConfig, FlowState


DEFAULT_HIDDEN_FLOW_DB_PATH = os.environ.get("HAYA_DB_PATH", "/opt/frontend/memories.db")
SNAPSHOT_PENDING = "PENDING"
SNAPSHOT_COMMITTED = "COMMITTED"


class HiddenFlowStoreError(RuntimeError):
    pass


class HiddenFlowConflict(HiddenFlowStoreError):
    pass


def _connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = str(db_path or DEFAULT_HIDDEN_FLOW_DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return (
        datetime.datetime.utcnow()
        + datetime.timedelta(hours=8)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("hidden flow value is not JSON serializable") from exc


def ensure_hidden_flow_schema(
    db_path: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    owned = conn is None
    connection = conn or _connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS hidden_flow_configs (
                flow_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                config_json TEXT NOT NULL,
                version INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hidden_flow_runtime (
                chat_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                flow_id TEXT,
                state_json TEXT,
                pending_guide_json TEXT,
                config_version INTEGER,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hidden_flow_message_snapshots (
                assistant_message_id INTEGER PRIMARY KEY,
                user_message_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,
                flow_id TEXT,
                status TEXT NOT NULL,
                runtime_version_before INTEGER NOT NULL,
                config_version INTEGER,
                applied_guide_json TEXT,
                control_json TEXT,
                state_before_json TEXT NOT NULL,
                state_after_json TEXT NOT NULL,
                next_guide_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hidden_flow_snapshots_chat_status
                ON hidden_flow_message_snapshots(chat_id, status);
            """
        )
        if owned:
            connection.commit()
    finally:
        if owned:
            connection.close()


def _decode_config(row: sqlite3.Row) -> Optional[tuple[FlowConfig, int]]:
    try:
        raw = json.loads(str(row["config_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    config = normalize_flow_config(raw)
    if (
        config is None
        or not config.enabled
        or not bool(row["enabled"])
        or config.flow_id != str(row["flow_id"] or "")
        or int(row["schema_version"]) != int(config.schema_version)
    ):
        return None
    return config, int(row["version"])


def load_configs(
    db_path: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[tuple[FlowConfig, int], ...]:
    owned = conn is None
    connection = conn or _connect(db_path)
    try:
        ensure_hidden_flow_schema(conn=connection)
        rows = connection.execute(
            "SELECT * FROM hidden_flow_configs ORDER BY flow_id"
        ).fetchall()
        result: list[tuple[FlowConfig, int]] = []
        for row in rows:
            decoded = _decode_config(row)
            if decoded is not None:
                result.append(decoded)
        return tuple(result)
    finally:
        if owned:
            connection.close()


def upsert_flow_config(
    raw_config: Mapping[str, Any] | FlowConfig,
    *,
    enabled: Optional[bool] = None,
    db_path: Optional[str] = None,
    now: Optional[str] = None,
) -> int:
    config = normalize_flow_config(raw_config)
    if config is None:
        raise ValueError("invalid hidden flow config")
    enabled_value = bool(config.enabled if enabled is None else enabled)
    if not enabled_value:
        config = FlowConfig(
            flow_id=config.flow_id,
            enabled=False,
            initial_stage=config.initial_stage,
            stages=config.stages,
            cues=config.cues,
            pools=config.pools,
            schema_version=config.schema_version,
        )
    encoded = _json(config.to_dict())
    connection = _connect(db_path)
    try:
        ensure_hidden_flow_schema(conn=connection)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT version FROM hidden_flow_configs WHERE flow_id=?",
            (config.flow_id,),
        ).fetchone()
        version = int(existing["version"]) + 1 if existing is not None else 1
        stamp = str(now or _now())
        connection.execute(
            """
            INSERT INTO hidden_flow_configs
                (flow_id, schema_version, enabled, config_json, version,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(flow_id) DO UPDATE SET
                schema_version=excluded.schema_version,
                enabled=excluded.enabled,
                config_json=excluded.config_json,
                version=excluded.version,
                updated_at=excluded.updated_at
            """,
            (
                config.flow_id,
                int(config.schema_version),
                int(enabled_value),
                encoded,
                version,
                stamp,
                stamp,
            ),
        )
        connection.commit()
        return version
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _inactive_state() -> FlowState:
    return FlowState.inactive()


def _decode_state(value: Any) -> FlowState:
    if value is None:
        return _inactive_state()
    try:
        return FlowState.from_dict(json.loads(str(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HiddenFlowStoreError("invalid hidden flow runtime state")


def _decode_guide(value: Any) -> Optional[AppliedGuide]:
    if value is None:
        return None
    try:
        guide = AppliedGuide.from_dict(json.loads(str(value)))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HiddenFlowStoreError("invalid hidden flow pending guide")
    if guide.status != "pending":
        raise HiddenFlowStoreError("non-pending guide in runtime row")
    return guide


def load_runtime(
    chat_id: str,
    db_path: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    owned = conn is None
    connection = conn or _connect(db_path)
    try:
        ensure_hidden_flow_schema(conn=connection)
        row = connection.execute(
            "SELECT * FROM hidden_flow_runtime WHERE chat_id=?",
            (str(chat_id),),
        ).fetchone()
        if row is None:
            return {
                "chat_id": str(chat_id),
                "version": 0,
                "config_version": None,
                "state": _inactive_state(),
                "pending_guide": None,
                "flow_id": None,
            }
        try:
            state = _decode_state(row["state_json"])
            guide = _decode_guide(row["pending_guide_json"])
        except HiddenFlowStoreError:
            # Never trust a corrupt row as an active flow.
            state = _inactive_state()
            guide = None
        return {
            "chat_id": str(chat_id),
            "version": int(row["version"]),
            "config_version": (
                int(row["config_version"])
                if row["config_version"] is not None else None
            ),
            "state": state,
            "pending_guide": guide,
            "flow_id": str(row["flow_id"]) if row["flow_id"] else None,
        }
    finally:
        if owned:
            connection.close()



def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, FlowState):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError("invalid hidden flow state")

def _guide_dict(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, AppliedGuide):
        return value.to_dict()
    if isinstance(value, Mapping):
        return dict(value)
    raise ValueError("invalid hidden flow guide")


def stage_snapshot(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
) -> None:
    """Insert the PENDING half of a hidden-flow terminal projection."""
    required = (
        "assistant_message_id",
        "user_message_id",
        "chat_id",
        "runtime_version_before",
        "state_before",
        "state_after",
    )
    if any(key not in snapshot for key in required):
        raise ValueError("incomplete hidden flow snapshot")
    state_before_raw = _state_dict(snapshot["state_before"])
    state_after_raw = _state_dict(snapshot["state_after"])
    state_before = FlowState.from_dict(state_before_raw)
    state_after = FlowState.from_dict(state_after_raw)
    applied = _guide_dict(snapshot.get("applied_guide"))
    next_guide = _guide_dict(snapshot.get("next_guide"))
    if applied is not None:
        AppliedGuide.from_dict(applied)
    if next_guide is not None:
        AppliedGuide.from_dict(next_guide)
    control = snapshot.get("control")
    if control is not None:
        if not isinstance(control, Mapping):
            raise ValueError("invalid hidden flow control")
        if set(control) - {"flowId", "action", "keys"}:
            raise ValueError("invalid hidden flow control")
        if not isinstance(control.get("flowId"), str):
            raise ValueError("invalid hidden flow control")
        if control.get("action") is not None and control.get("action") not in {
            "start", "hold", "continue", "stop"
        }:
            raise ValueError("invalid hidden flow control")
        keys = control.get("keys", [])
        if not isinstance(keys, list) or len(keys) > 4 or any(
            not isinstance(item, str) for item in keys
        ):
            raise ValueError("invalid hidden flow control")
        control_json = _json(dict(control))
    else:
        control_json = None
    now = _now()
    conn.execute(
        """
        INSERT INTO hidden_flow_message_snapshots
            (assistant_message_id, user_message_id, chat_id, flow_id, status,
             runtime_version_before, config_version, applied_guide_json,
             control_json, state_before_json, state_after_json, next_guide_json,
             created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(snapshot["assistant_message_id"]),
            int(snapshot["user_message_id"]),
            str(snapshot["chat_id"]),
            (
                str(snapshot.get("flow_id") or "")
                or (state_after.flow_id if state_after.active else None)
            ),
            SNAPSHOT_PENDING,
            int(snapshot["runtime_version_before"]),
            (
                int(snapshot["config_version"])
                if snapshot.get("config_version") is not None else None
            ),
            _json(applied) if applied is not None else None,
            control_json,
            _json(state_before_raw),
            _json(state_after_raw),
            _json(next_guide) if next_guide is not None else None,
            now,
            now,
        ),
    )


def delete_pending_snapshot(
    conn: sqlite3.Connection,
    assistant_message_id: int,
) -> None:
    conn.execute(
        "DELETE FROM hidden_flow_message_snapshots "
        "WHERE assistant_message_id=? AND status=?",
        (int(assistant_message_id), SNAPSHOT_PENDING),
    )


def finalize_snapshot(
    conn: sqlite3.Connection,
    assistant_message_id: int,
) -> dict[str, Any]:
    """CAS the runtime and mark the matching snapshot COMMITTED."""
    row = conn.execute(
        "SELECT * FROM hidden_flow_message_snapshots "
        "WHERE assistant_message_id=?",
        (int(assistant_message_id),),
    ).fetchone()
    if row is None:
        raise HiddenFlowConflict("hidden flow snapshot missing")
    if str(row["status"]) == SNAPSHOT_COMMITTED:
        return dict(row)
    if str(row["status"]) != SNAPSHOT_PENDING:
        raise HiddenFlowConflict("hidden flow snapshot status invalid")
    runtime = conn.execute(
        "SELECT version FROM hidden_flow_runtime WHERE chat_id=?",
        (str(row["chat_id"]),),
    ).fetchone()
    current_version = int(runtime["version"]) if runtime is not None else 0
    expected = int(row["runtime_version_before"])
    if current_version != expected:
        raise HiddenFlowConflict("hidden flow runtime version CAS failed")
    state_after = FlowState.from_dict(json.loads(str(row["state_after_json"])))
    next_guide = (
        AppliedGuide.from_dict(json.loads(str(row["next_guide_json"])))
        if row["next_guide_json"] is not None else None
    )
    new_version = expected + 1
    state_json = _json(state_after.to_dict())
    guide_json = _json(next_guide.to_dict()) if next_guide is not None else None
    now = _now()
    if runtime is None:
        conn.execute(
            """
            INSERT INTO hidden_flow_runtime
                (chat_id, schema_version, flow_id, state_json,
                 pending_guide_json, config_version, version, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                str(row["chat_id"]),
                int(state_after.schema_version),
                state_after.flow_id if state_after.active else None,
                state_json,
                guide_json,
                row["config_version"],
                new_version,
                now,
            ),
        )
    else:
        updated = conn.execute(
            """
            UPDATE hidden_flow_runtime SET
                schema_version=?, flow_id=?, state_json=?,
                pending_guide_json=?, config_version=?, version=?, updated_at=?
            WHERE chat_id=? AND version=?
            """,
            (
                int(state_after.schema_version),
                state_after.flow_id if state_after.active else None,
                state_json,
                guide_json,
                row["config_version"],
                new_version,
                now,
                str(row["chat_id"]),
                expected,
            ),
        )
        if int(updated.rowcount or 0) != 1:
            raise HiddenFlowConflict("hidden flow runtime update CAS failed")
    updated = conn.execute(
        """
        UPDATE hidden_flow_message_snapshots
        SET status=?, updated_at=?
        WHERE assistant_message_id=? AND status=?
        """,
        (SNAPSHOT_COMMITTED, now, int(assistant_message_id), SNAPSHOT_PENDING),
    )
    if int(updated.rowcount or 0) != 1:
        raise HiddenFlowConflict("hidden flow snapshot commit CAS failed")
    return dict(
        conn.execute(
            "SELECT * FROM hidden_flow_message_snapshots WHERE assistant_message_id=?",
            (int(assistant_message_id),),
        ).fetchone()
    )


def load_runtime_bundle(
    chat_id: str,
    *,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    connection = _connect(db_path)
    try:
        ensure_hidden_flow_schema(conn=connection)
        return {
            "configs": load_configs(conn=connection),
            "runtime": load_runtime(chat_id, conn=connection),
        }
    finally:
        connection.close()
