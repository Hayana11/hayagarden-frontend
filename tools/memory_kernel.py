"""Memory Semantic Kernel CORE-0: Evidence, State, Delta, and an explicit store.

No provider, retrieval, HTTP, MCP, runtime config, or application imports.
All persisted states are accepted semantic versions (not necessarily facts).
Candidate review and head selection belong outside this module.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

from tools.memory_kernel_schema import ensure_memory_kernel_schema


@dataclass(frozen=True, kw_only=True)
class Evidence:
    evidence_id: str
    source_type: str
    source_ref: str
    observed_at: str
    provenance: dict[str, Any]
    # Required, never inferred from source_type or from the text's plausibility.
    origin_kind: Literal["source", "derived"]
    occurred_at: str | None = None
    content: str | None = None
    content_ref: str | None = None
    participants: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    integrity: str | None = None
    visibility: str | None = None


@dataclass(frozen=True, kw_only=True)
class State:
    state_id: str
    scope: str
    subject_ref: str
    representation: str
    evidence_refs: tuple[str, ...]
    confidence: float
    epistemic_status: str
    status: str
    created_at: str
    lineage_id: str | None = None
    content: str | None = None
    structured_value: Any = None


@dataclass(frozen=True, kw_only=True)
class Delta:
    """A recorded transition; triggers are supporting inputs, never causal facts."""

    delta_id: str
    target_scope: str
    before_ref: str | None
    after_ref: str
    trigger_evidence_refs: tuple[str, ...]
    derived_by: dict[str, Any]
    confidence: float
    occurred_at: str
    review_status: str
    rationale: str | None = None
    rationale_ref: str | None = None
    rationale_kind: Literal["derived"] = "derived"


_JSON_FIELDS = {
    Evidence: {"provenance", "participants", "entities"},
    State: {"structured_value", "evidence_refs"},
    Delta: {"trigger_evidence_refs", "derived_by"},
}
_SEQUENCE_FIELDS = {"participants", "entities", "evidence_refs", "trigger_evidence_refs"}
_TABLES = {
    Evidence: ("memory_kernel_evidence", "evidence_id"),
    State: ("memory_kernel_state", "state_id"),
    Delta: ("memory_kernel_delta", "delta_id"),
}


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _timestamp(value: Any, name: str) -> None:
    _text(value, name)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} requires a timezone")


def _json(value: Any) -> str:
    # Reject lossy tuple/object/key coercion and NaN before persistence.
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if json.loads(encoded) != value:
        raise ValueError("value must round-trip as JSON without coercion")
    return encoded


def _validate(record: Evidence | State | Delta) -> None:
    if type(record) not in _TABLES:
        raise TypeError("expected Evidence, State or Delta")
    identity = _TABLES[type(record)][1]
    _text(getattr(record, identity), identity)
    for field in fields(record):
        value = getattr(record, field.name)
        if field.name in _SEQUENCE_FIELDS:
            if not isinstance(value, tuple):
                raise ValueError(f"{field.name} must be a tuple")
            for ref in value:
                _text(ref, field.name)
            if len(set(value)) != len(value):
                raise ValueError(f"{field.name} contains duplicate values")
        elif field.name in ("provenance", "derived_by"):
            if not isinstance(value, dict) or not value:
                raise ValueError(f"{field.name} must be a nonempty JSON object")
            _json(value)
        elif field.name in ("observed_at", "created_at", "occurred_at"):
            if value is not None or field.name != "occurred_at" or isinstance(record, Delta):
                _timestamp(value, field.name)
        elif field.name == "confidence":
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError("confidence must be finite and between zero and one")
        elif field.name == "structured_value":
            if value is not None:
                _json(value)
        elif value is not None:
            # Content may intentionally be an empty string; never strip source text.
            if field.name in ("content", "rationale"):
                if not isinstance(value, str):
                    raise ValueError(f"{field.name} must be text")
            else:
                _text(value, field.name)
    required = {
        Evidence: ("source_type", "source_ref", "origin_kind"),
        State: ("scope", "subject_ref", "representation", "epistemic_status", "status"),
        Delta: ("target_scope", "after_ref", "review_status", "rationale_kind"),
    }
    for name in required[type(record)]:
        _text(getattr(record, name), name)
    if isinstance(record, Evidence):
        if record.origin_kind not in ("source", "derived"):
            raise ValueError("origin_kind must explicitly be source or derived")
        if record.content is None and record.content_ref is None:
            raise ValueError("Evidence requires content or content_ref")
    elif isinstance(record, State):
        if record.content is None and record.structured_value is None:
            raise ValueError("State requires content or structured_value")
    else:
        if record.before_ref == record.after_ref:
            raise ValueError("Delta cannot be a self-loop")
        if record.rationale_kind != "derived":
            raise ValueError("Delta rationale must remain derived")


def _values(record: Evidence | State | Delta) -> dict[str, Any]:
    _validate(record)
    values = asdict(record)
    for name in _JSON_FIELDS[type(record)]:
        value = values[name]
        if value is not None:
            values[name] = _json(list(value) if name in _SEQUENCE_FIELDS else value)
    return values


def _record(record_type, row: sqlite3.Row):
    values = dict(row)
    for name in _JSON_FIELDS[record_type]:
        if values[name] is not None:
            values[name] = json.loads(values[name])
            if name in _SEQUENCE_FIELDS:
                values[name] = tuple(values[name])
    return record_type(**values)


class MemoryKernel:
    """Explicit file-backed SQLite repository/service; no implicit production path.

    Call initialize() explicitly before use. Each write owns BEGIN IMMEDIATE,
    rolls back on failure, and closes its connection. Returned records are detached
    snapshots: even mutation of a nested JSON value cannot write through to storage.
    """

    def __init__(self, db_path: str | Path):
        if not isinstance(db_path, (str, Path)) or not str(db_path).strip():
            raise ValueError("an explicit database path is required")
        if str(db_path) == ":memory:" or str(db_path).startswith("file:"):
            raise ValueError("use an explicit filesystem database path")
        self.db_path = str(Path(db_path).resolve())

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def initialize(self) -> None:
        with self._connection() as conn:
            ensure_memory_kernel_schema(conn)

    @staticmethod
    def _insert(conn, record) -> None:
        values = _values(record)
        table = _TABLES[type(record)][0]
        names = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            tuple(values.values()),
        )

    @staticmethod
    def _get(conn, record_type, identity):
        _text(identity, "identity")
        table, key = _TABLES[record_type]
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (identity,)).fetchone()
        if row is None:
            raise KeyError(f"{record_type.__name__} not found: {identity}")
        return _record(record_type, row)

    def create_evidence(self, evidence: Evidence) -> Evidence:
        if type(evidence) is not Evidence:
            raise TypeError("expected Evidence")
        with self._write() as conn:
            self._insert(conn, evidence)
            return self._get(conn, Evidence, evidence.evidence_id)

    def create_state(self, state: State, formation: Delta) -> tuple[State, Delta]:
        """First formation always records a null-before Delta in the same commit."""
        return self._transition(state, formation, initial=True)

    def revise_state(self, state: State, transition: Delta) -> tuple[State, Delta]:
        """Append a new semantic version and Delta, preserving the predecessor."""
        return self._transition(state, transition, initial=False)

    def _transition(self, state: State, delta: Delta, *, initial: bool):
        if type(state) is not State or type(delta) is not Delta:
            raise TypeError("expected State and Delta")
        _validate(state)
        _validate(delta)
        if (delta.before_ref is None) != initial:
            raise ValueError("formation requires null before_ref; revision requires a predecessor")
        if delta.after_ref != state.state_id or delta.target_scope != state.scope:
            raise ValueError("Delta must target the new State and its scope")
        with self._write() as conn:
            if initial:
                if state.lineage_id is not None and conn.execute(
                    "SELECT 1 FROM memory_kernel_state WHERE lineage_id = ?",
                    (state.lineage_id,),
                ).fetchone():
                    raise ValueError("existing lineage requires revise_state")
            else:
                before = self._get(conn, State, delta.before_ref)
                semantic_fields = (
                    "scope", "subject_ref", "representation", "content",
                    "structured_value", "evidence_refs", "confidence",
                    "epistemic_status", "status",
                )
                if all(getattr(before, name) == getattr(state, name) for name in semantic_fields):
                    raise ValueError("revision requires a semantic change, not a comparison")
                if before.lineage_id != state.lineage_id:
                    raise ValueError("revision must preserve lineage, including a null lineage")
            self._insert(conn, state)
            self._insert(conn, delta)
            return (
                self._get(conn, State, state.state_id),
                self._get(conn, Delta, delta.delta_id),
            )

    def get_evidence(self, evidence_id: str) -> Evidence:
        with self._connection() as conn:
            return self._get(conn, Evidence, evidence_id)

    def get_state(self, state_id: str) -> State:
        with self._connection() as conn:
            return self._get(conn, State, state_id)

    def get_delta(self, delta_id: str) -> Delta:
        with self._connection() as conn:
            return self._get(conn, Delta, delta_id)

    def lineage_history(self, lineage_id: str) -> list[State]:
        """Return persisted versions in insertion order, not a unique path.

        Branching revisions are allowed; this list does not select a current head
        or imply linear history. Use transitions_for_state and Delta edges to
        reconstruct branching paths.
        """
        _text(lineage_id, "lineage_id")
        with self._connection() as conn:
            return [
                _record(State, row) for row in conn.execute(
                    "SELECT * FROM memory_kernel_state WHERE lineage_id = ? ORDER BY rowid",
                    (lineage_id,),
                )
            ]

    def transitions_for_state(self, state_id: str) -> list[Delta]:
        with self._connection() as conn:
            self._get(conn, State, state_id)
            return [
                _record(Delta, row) for row in conn.execute(
                    "SELECT * FROM memory_kernel_delta "
                    "WHERE before_ref = ? OR after_ref = ? ORDER BY rowid",
                    (state_id, state_id),
                )
            ]

    def evidence_for_state(self, state_id: str) -> list[Evidence]:
        with self._connection() as conn:
            state = self._get(conn, State, state_id)
            return [self._get(conn, Evidence, ref) for ref in state.evidence_refs]

    def evidence_for_delta(self, delta_id: str) -> list[Evidence]:
        with self._connection() as conn:
            delta = self._get(conn, Delta, delta_id)
            return [self._get(conn, Evidence, ref) for ref in delta.trigger_evidence_refs]
