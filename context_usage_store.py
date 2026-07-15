"""Whitelisted SQLite storage for Claude Code and Codex quota snapshots."""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3
from typing import Any


VALID_AGENT_IDS = frozenset({"claude", "codex"})


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _iso(value: Any, fallback: str = "") -> str:
    if isinstance(value, (int, float)) and math.isfinite(value):
        try:
            return dt.datetime.fromtimestamp(float(value), dt.timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return fallback
    text = str(value or "").strip()
    return text[:80] if _timestamp(text) else fallback


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _number(value: Any, *, minimum: float = 0.0, maximum: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return round(result, 4)


def _integer(value: Any, *, maximum: int = 10**12) -> int | None:
    number = _number(value, minimum=0, maximum=float(maximum))
    return int(round(number)) if number is not None else None


def _window(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    used = _number(raw.get("used_percentage", raw.get("used_percent")), maximum=100)
    remaining = _number(raw.get("remaining_percentage"), maximum=100)
    if used is None and remaining is not None:
        used = round(100 - remaining, 4)
    if remaining is None and used is not None:
        remaining = round(100 - used, 4)
    result: dict[str, Any] = {}
    if used is not None:
        result["used_percentage"] = used
    if remaining is not None:
        result["remaining_percentage"] = remaining
    remaining_minutes = _integer(raw.get("remaining_minutes"), maximum=60 * 24 * 31)
    if remaining_minutes is not None:
        result["remaining_minutes"] = remaining_minutes
    resets_at = _iso(raw.get("resets_at"))
    if resets_at:
        result["resets_at"] = resets_at
    for key in ("total_tokens", "projected_total_tokens"):
        value = _integer(raw.get(key))
        if value is not None:
            result[key] = value
    basis = _text(raw.get("remaining_basis"), 40)
    if basis:
        result["remaining_basis"] = basis
    models = raw.get("models")
    if isinstance(models, list):
        result["models"] = [_text(model, 80) for model in models[:8] if _text(model, 80)]
    return result


def _session(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, Any] = {}
    for source, target in (
        ("latest_context_tokens", "latest_context_tokens"),
        ("context_window_tokens", "context_window_tokens"),
    ):
        value = _integer(raw.get(source))
        if value is not None:
            result[target] = value
    model = _text(raw.get("model"), 100)
    if model:
        result["model"] = model
    updated_at = _iso(raw.get("updated_at"))
    if updated_at:
        result["updated_at"] = updated_at
    return result or None


def normalize_agent(raw: Any, generated_at: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("agent must be an object")
    agent_id = _text(raw.get("id"), 16).lower()
    if agent_id not in VALID_AGENT_IDS:
        raise ValueError("agent id must be claude or codex")
    quota_raw = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
    updated_at = _iso(quota_raw.get("updated_at") or raw.get("observed_at"), generated_at)
    quota: dict[str, Any] = {
        "five_hour": _window(quota_raw.get("five_hour")),
        "seven_day": _window(quota_raw.get("seven_day")),
        "updated_at": updated_at,
    }
    for key in ("latest_tokens", "total_tokens", "context_window_tokens"):
        value = _integer(quota_raw.get(key))
        if value is not None:
            quota[key] = value
    effective = quota_raw.get("effective_limit")
    if isinstance(effective, dict):
        quota["effective_limit"] = {
            key: value for key, value in {
                "kind": _text(effective.get("kind"), 40),
                "reset_text": _text(effective.get("reset_text"), 240),
                "observed_at": _iso(effective.get("observed_at"), updated_at),
                "exhausted": bool(effective.get("exhausted")),
            }.items() if value not in ("", None)
        }
    sessions = []
    for item in raw.get("active_sessions") if isinstance(raw.get("active_sessions"), list) else []:
        clean = _session(item)
        if clean:
            sessions.append(clean)
        if len(sessions) >= 6:
            break
    return {
        "id": agent_id,
        "name": _text(raw.get("name"), 60) or ("Claude Code" if agent_id == "claude" else "Codex"),
        "quota_source": _text(raw.get("quota_source"), 80) or "unavailable",
        "quota": quota,
        "active_sessions": sessions,
    }


def ensure_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_usage_snapshots (
                agent_id TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_report(payload: Any, db_path: str) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("report must be an object")
    generated_at = _iso(payload.get("generated_at"), _now_iso())
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise ValueError("agents must be a non-empty array")
    normalized: dict[str, dict[str, Any]] = {}
    for raw in raw_agents[:4]:
        agent = normalize_agent(raw, generated_at)
        normalized[agent["id"]] = agent
    if not normalized:
        raise ValueError("no supported agents")

    ensure_schema(db_path)
    received_at = _now_iso()
    accepted: list[str] = []
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for agent_id, agent in normalized.items():
            observed_at = agent["quota"]["updated_at"] or generated_at
            current = conn.execute(
                "SELECT observed_at FROM context_usage_snapshots WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
            if current and _timestamp(current[0]) > _timestamp(observed_at):
                continue
            conn.execute(
                """
                INSERT INTO context_usage_snapshots (agent_id,snapshot_json,observed_at,received_at)
                VALUES (?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    snapshot_json=excluded.snapshot_json,
                    observed_at=excluded.observed_at,
                    received_at=excluded.received_at
                """,
                (agent_id, json.dumps(agent, ensure_ascii=False, separators=(",", ":")), observed_at, received_at),
            )
            accepted.append(agent_id)
        conn.commit()
    finally:
        conn.close()
    return accepted, get_snapshot(db_path)


def get_snapshot(db_path: str) -> dict[str, Any]:
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rows = conn.execute(
            "SELECT snapshot_json,observed_at FROM context_usage_snapshots "
            "ORDER BY CASE agent_id WHEN 'claude' THEN 0 ELSE 1 END"
        ).fetchall()
    finally:
        conn.close()
    agents: list[dict[str, Any]] = []
    newest = ""
    for payload, observed_at in rows:
        try:
            agents.append(json.loads(payload))
        except (TypeError, json.JSONDecodeError):
            continue
        if _timestamp(observed_at) >= _timestamp(newest):
            newest = observed_at
    return {"generated_at": newest, "agents": agents}
