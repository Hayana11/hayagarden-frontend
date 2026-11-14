"""Durable intake candidates and read-only provenance back to canonical sources."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

@dataclass(frozen=True)
class CandidateSourceTrace:
    source_refs: tuple[str, ...]
    derived_refs: tuple[str, ...]
    unresolved_refs: tuple[str, ...]
    raw_messages: tuple[dict[str, Any], ...]
    evidence_text: str

    @property
    def provenance_status(self) -> str:
        return "unresolved" if self.unresolved_refs else "resolved"

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_candidates (
            candidate_id TEXT PRIMARY KEY,
            candidate_kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_refs_json TEXT NOT NULL,
            derived_refs_json TEXT NOT NULL,
            producer TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            explicit_intent INTEGER NOT NULL DEFAULT 0,
            provenance_status TEXT NOT NULL DEFAULT 'resolved',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)

def create_candidate(
    db_path: str,
    *,
    candidate_kind: str,
    payload: Any,
    source_type: str,
    source_refs: Iterable[str],
    derived_refs: Iterable[str] = (),
    producer: str,
    priority: int = 0,
    explicit_intent: bool = False,
    provenance_status: str = "resolved",
    candidate_id: str | None = None,
) -> str:
    if provenance_status not in {"resolved", "unresolved"}:
        raise ValueError("provenance_status must be resolved or unresolved")
    fields = (candidate_kind, source_type, producer)
    if any(not isinstance(value, str) or not value.strip() for value in fields):
        raise ValueError("candidate_kind, source_type, and producer are required")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    source_refs_json = json.dumps(list(source_refs), ensure_ascii=False, separators=(",", ":"))
    derived_refs_json = json.dumps(list(derived_refs), ensure_ascii=False, separators=(",", ":"))
    candidate_id = candidate_id or "memcand:" + uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        ensure_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO memory_candidates (
                candidate_id,candidate_kind,payload_json,source_type,source_refs_json,
                derived_refs_json,producer,priority,explicit_intent,provenance_status,
                status,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
            (candidate_id, candidate_kind, payload_json, source_type, source_refs_json,
             derived_refs_json, producer, int(priority), int(bool(explicit_intent)),
             provenance_status, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return candidate_id

def load_candidate(db_path: str, candidate_id: str) -> dict[str, Any] | None:
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM memory_candidates WHERE candidate_id=?", (candidate_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    item = dict(row)
    for column, key in (("payload_json", "payload"), ("source_refs_json", "source_refs"),
                        ("derived_refs_json", "derived_refs")):
        item[key] = json.loads(item.pop(column))
    item["explicit_intent"] = bool(item["explicit_intent"])
    return item

def _unresolved_turn_source(db_path: str, turn_id: str) -> tuple[str, list[str], str]:
    ref = [f"turn_id:{turn_id}"] if turn_id else []
    return "chat_turn", ref, "unresolved"

def _chat_turn_source(db_path: str, turn_id: str | None) -> tuple[str, list[str], str]:
    turn = str(turn_id or "").strip()
    if not turn:
        return _unresolved_turn_source(db_path, turn)
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT user_message_id FROM moments_active_turn WHERE turn_key=?", (turn,),
            ).fetchall()
        except sqlite3.OperationalError:
            return _unresolved_turn_source(db_path, turn)
        if len(rows) != 1 or rows[0][0] is None:
            return _unresolved_turn_source(db_path, turn)
        message_id = int(rows[0][0])
        row = conn.execute(
            "SELECT author FROM chat_messages WHERE id=?", (message_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None or str(row[0] or "").strip().lower() not in {"hayana", "haya", "user"}:
        return _unresolved_turn_source(db_path, turn)
    return "chat_messages", [f"chat_messages:{message_id}"], "resolved"


def capture_memory_write_candidate(
    db_path: str,
    *,
    content: str,
    surface: str | None,
    turn_id: str | None,
) -> str:
    source_type, source_refs, provenance_status = _chat_turn_source(db_path, turn_id)
    stable_id = None
    if source_refs:
        identity = json.dumps(
            ["memory.write", source_type, source_refs, content],
            ensure_ascii=False, separators=(",", ":"),
        )
        stable_id = "memcand:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return create_candidate(
        db_path, candidate_kind="memory",
        payload={"content": content, "surface": str(surface or "")},
        source_type=source_type, source_refs=source_refs, producer="memory.write",
        priority=100, explicit_intent=True, provenance_status=provenance_status,
        candidate_id=stable_id,
    )


def create_profile_patch_candidate(
    db_path: str, *, target: str, source_day: str, changes: Iterable[dict[str, Any]],
    source_refs: Iterable[str], derived_refs: Iterable[str], prompt_version: str,
    provenance_status: str = "resolved",
) -> str:
    target = str(target or "").strip().upper()
    changes, derived_refs = list(changes), list(derived_refs)
    if target not in {"SELF", "HAYA"} or not changes or not source_day or not prompt_version:
        raise ValueError("profile patch requires target, day, prompt version, and changes")
    identity = json.dumps(
        ["daily_profile", target, source_day, derived_refs, prompt_version],
        ensure_ascii=False, separators=(",", ":"),
    )
    candidate_id = "memcand:profile:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return create_candidate(
        db_path, candidate_kind="profile_patch",
        payload={"target": target, "source_day": source_day, "prompt_version": prompt_version,
                 "changes": changes},
        source_type="chat_messages", source_refs=source_refs, derived_refs=derived_refs,
        producer="daily_profile", priority=50, provenance_status=provenance_status,
        candidate_id=candidate_id,
    )


def _message_ids_for_member(member: Any) -> tuple[int, ...]:
    ref = str(member.source_ref)
    parts = ref.split(":")
    if member.source_kind == "completed_turn":
        from continuity.materialization import _turn_ids
        return _turn_ids(ref)
    if member.source_kind == "autonomous_event":
        from continuity.materialization import _wake_id
        return (_wake_id(ref),)
    if member.source_kind == "attachment_span" and len(parts) == 3 and parts[0] == "attachment":
        message_id, ordinal = int(parts[1]), int(parts[2])
        return (message_id,) if message_id > 0 and ordinal >= 0 else ()
    if member.source_kind == "incomplete_user_turn":
        from dayblock_shadow import _dayblock_visible_turn_ids, _is_dayblock_incomplete_user_member
        visible = _dayblock_visible_turn_ids(ref)
        if visible:
            return visible
        if _is_dayblock_incomplete_user_member(member) and len(parts) == 2:
            message_id = int(parts[1])
            return (message_id,) if message_id > 0 else ()
    return ()


def _read_raw_rows(db_path: str, message_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = tuple(dict.fromkeys(int(value) for value in message_ids))
    if not ids:
        return {}
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"file:{quote(path.as_posix(), safe='/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM chat_messages WHERE id IN ({placeholders}) ORDER BY id", ids,
        ).fetchall()
    finally:
        conn.close()
    return {int(row["id"]): dict(row) for row in rows}


def _trace_members(
    db_path: str,
    members: tuple[Any, ...],
    derived_ref: str,
    materialize: Callable[[tuple[Any, ...], tuple[dict[str, Any], ...]], Any],
) -> CandidateSourceTrace:
    ids_by_member: dict[str, tuple[int, ...]] = {}
    unresolved: list[str] = []
    all_ids: list[int] = []
    for member in members:
        try:
            ids = _message_ids_for_member(member)
        except (ValueError, RuntimeError):
            ids = ()
        if not ids:
            unresolved.append(str(member.source_ref))
        ids_by_member[str(member.source_ref)] = ids
        all_ids.extend(ids)
    rows_by_id = _read_raw_rows(db_path, all_ids)
    for member in members:
        ids = ids_by_member[str(member.source_ref)]
        if any(message_id not in rows_by_id for message_id in ids):
            unresolved.append(str(member.source_ref))
    message_refs = tuple(f"chat_messages:{mid}" for mid in sorted(rows_by_id))
    evidence_text = ""
    if not unresolved:
        try:
            evidence_text = str(materialize(members, tuple(rows_by_id[mid] for mid in sorted(rows_by_id))).body)
        except RuntimeError:
            unresolved.extend(str(member.source_ref) for member in members)
    public_rows = tuple({
        key: row.get(key) for key in (
            "id", "author", "content", "created_at", "tool_calls", "attachments",
            "file_url", "file_name", "image_url", "source_kind",
        )
    } for row in (rows_by_id[mid] for mid in sorted(rows_by_id)))
    return CandidateSourceTrace(
        source_refs=message_refs, derived_refs=(derived_ref,),
        unresolved_refs=tuple(dict.fromkeys(unresolved)), raw_messages=public_rows,
        evidence_text=evidence_text,
    )


def trace_ready_chunk(db_path: str, chunk_id: str) -> CandidateSourceTrace:
    from continuity.materialization import materialize_source_members
    from continuity.store import load_chunk, load_snapshot, open_continuity_read_only

    conn = open_continuity_read_only(db_path)
    try:
        chunk = load_chunk(conn, chunk_id)
        if chunk is None or chunk.status != "ready":
            raise ValueError("READY Continuity Chunk not found")
        snapshot = load_snapshot(conn, chunk.snapshot_id)
    finally:
        conn.close()
    if snapshot is None:
        raise ValueError("Continuity source snapshot not found")
    return _trace_members(
        db_path, snapshot.members, f"continuity_chunk:{chunk_id}",
        materialize_source_members,
    )


def trace_ready_dayblock(
    db_path: str,
    dayblock_db_path: str,
    dayblock_id: str,
    *,
    static_dir: str = "/opt/frontend/static",
) -> CandidateSourceTrace:
    from continuity.dayblock_context import load_ready_dayblocks
    from dayblock_shadow import materialize_raw_evidence

    dayblocks, _ = load_ready_dayblocks(dayblock_db_path)
    block = next((item for item in dayblocks if item.chunk_id == dayblock_id), None)
    if block is None:
        raise ValueError("READY DayBlock not found")
    return _trace_members(
        db_path, block.members, f"dayblock:{block.source_day}:{block.generation_id}",
        lambda members, rows: materialize_raw_evidence(members, rows, static_dir=static_dir),
    )
