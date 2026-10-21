"""B-2: Kernel -> Ombre projection through Ombre's official runtime.

The Kernel is the authority; Ombre's vault is a projection it can rebuild.
This module never reads or writes Ombre files: every Ombre access goes through
tools/ombre_projection_worker.py, which runs in Ombre's own interpreter and
uses only BucketManager.create / update / get (plus list_all to see buckets
Ombre created itself). ``hold`` is never used, so its similarity merge stays
Ombre's own behaviour.

Bookkeeping lives in a separate projection-state database next to the Kernel
(never inside Ombre's buckets): lineage -> bucket, the target State, the State
last projected, what Ombre actually stored after the write (the observed
baseline and its hash), status, error and attempts.

- After every create/update the bucket is read back with get(); Ombre's own
  normalisation (e.g. a pinned bucket's importance becomes 10) is part of that
  baseline, so it is a success, never drift.
- Retries are idempotent: get() comes first, so a bucket Ombre already wrote
  before the bookkeeping committed is updated, not created twice.
- If Ombre is unavailable or incompatible the lineage is left ``pending``;
  the Kernel write already succeeded and nothing falls back to files or posts.
- A bucket whose semantics differ from the observed baseline was edited
  inside Ombre: it is reported ``diverged`` (ingested as a Kernel revision by
  memory_kernel_store), never overwritten.

Head selection is a policy outside the Kernel: a lineage's head is the State
with no outgoing Delta. A lineage with several heads is reported, not guessed.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tools.memory_kernel import MemoryKernel, State

WORKER = Path(__file__).with_name("ombre_projection_worker.py")
# What the projection owns in a bucket; lifecycle (resolved, activation, decay,
# archive placement, dont_surface) stays Ombre's.
SEMANTIC_FIELDS = ("content", "name", "domain", "tags", "importance", "valence", "arousal", "pinned")
# Fields Ombre may rewrite on a write (pinned -> importance 10, default domain,
# generated names). Only used to recognise our own landed write after a crash.
OMBRE_REWRITTEN = frozenset({"importance", "domain", "name"})
# Ombre's handover bucket is its own runtime state, never Kernel-managed.
SYSTEM_DOMAINS = {"呼吸间"}
DEFAULT_TIMEOUT_SECONDS = 60.0
# Lineages per worker run: starting the worker (Ombre import) costs ~1.7s on the VPS,
# a request ~10ms, so a first baseline of 360 lineages is 3 runs.
BATCH_SIZE = 120


class RuntimeUnavailable(RuntimeError):
    """Ombre's runtime could not be reached, or is not the one we were built for."""


class RequestFailed(str):
    """One request inside a worker run failed; the others still have results."""


@dataclass(frozen=True)
class Head:
    lineage_id: str
    state: State

    @property
    def bucket_id(self) -> str:
        return bucket_id_for(self.lineage_id)


def bucket_id_for(lineage_id: str) -> str:
    # Imported Ombre lineages keep their bucket; everything else gets a stable id.
    if lineage_id.startswith("lin:ombre:"):
        return lineage_id[len("lin:ombre:"):]
    return hashlib.sha256(lineage_id.encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return re.sub(r"\[\[|\]\]", "", text or "").strip()


def _as_list(value: Any) -> list[str]:
    # Legacy posts keep tags as one comma-separated string.
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v) for v in (value or [])]


def _importance(value: Any) -> int:
    # Legacy posts default to 0 meaning "unset"; Ombre would clamp it to 1 and decay it first.
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 5
    return 5 if number <= 0 else min(number, 10)


# ---------------------------------------------------------------- Kernel side

def select_heads(kernel_db: Path) -> tuple[list[Head], list[dict[str, Any]]]:
    """Read-only: one head per lineage, plus lineages that cannot be projected."""
    conn = sqlite3.connect(f"file:{kernel_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT s.state_id, s.lineage_id FROM memory_kernel_state s "
            "WHERE NOT EXISTS (SELECT 1 FROM memory_kernel_delta d WHERE d.before_ref = s.state_id) "
            "ORDER BY s.rowid"
        ).fetchall()
    finally:
        conn.close()
    kernel = MemoryKernel(kernel_db)
    by_lineage: dict[str, list[str]] = {}
    problems = []
    for state_id, lineage_id in rows:
        if not lineage_id:
            problems.append({"state_id": state_id, "reason": "no_lineage"})
            continue
        by_lineage.setdefault(lineage_id, []).append(state_id)
    heads = []
    for lineage_id, state_ids in by_lineage.items():
        if len(state_ids) > 1:
            problems.append({"lineage_id": lineage_id, "reason": "branched_heads", "heads": state_ids})
            continue
        state = kernel.get_state(state_ids[0])
        if state.content is None:
            problems.append({"lineage_id": lineage_id, "reason": "no_content"})
            continue
        heads.append(Head(lineage_id, state))
    return heads, problems


def current_heads(kernel_db: Path, lineage_ids: list[str]) -> dict[str, Head | None]:
    """The Kernel's head of each lineage now; None when it has none or several."""
    wanted = sorted(set(lineage_ids))
    found: dict[str, list[str]] = {lineage_id: [] for lineage_id in wanted}
    conn = sqlite3.connect(f"file:{kernel_db}?mode=ro", uri=True)
    try:
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for state_id, lineage_id in conn.execute(
                f"SELECT s.state_id, s.lineage_id FROM memory_kernel_state s WHERE s.lineage_id IN ({marks}) "
                "AND NOT EXISTS (SELECT 1 FROM memory_kernel_delta d WHERE d.before_ref = s.state_id)", chunk,
            ):
                found[lineage_id].append(state_id)
    finally:
        conn.close()
    kernel = MemoryKernel(kernel_db)
    heads: dict[str, Head | None] = {}
    for lineage_id, state_ids in found.items():
        state = kernel.get_state(state_ids[0]) if len(state_ids) == 1 else None
        heads[lineage_id] = Head(lineage_id, state) if state is not None and state.content is not None else None
    return heads


def head_contents(kernel_db: Path) -> list[tuple[str, str, str]]:
    """Cheap read-only (lineage_id, state_id, content) of every head, for dedupe."""
    conn = sqlite3.connect(f"file:{kernel_db}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in conn.execute(
            "SELECT s.lineage_id, s.state_id, s.content FROM memory_kernel_state s "
            "WHERE s.lineage_id IS NOT NULL AND s.content IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM memory_kernel_delta d WHERE d.before_ref = s.state_id)"
        )]
    finally:
        conn.close()


def bucket_type_for(head: Head) -> str:
    """FACT, core and pinned memories never become decaying dynamic buckets."""
    value = head.state.structured_value if isinstance(head.state.structured_value, dict) else {}
    legacy = value.get("legacy") or {}
    memory = value.get("memory") or {}
    if (
        head.state.representation == "fact"
        or memory.get("type") == "FACT"
        or memory.get("layer") == "core"
        or memory.get("pinned")
        or legacy.get("pinned")
        or legacy.get("ombre_type") == "permanent"
    ):
        return "permanent"
    return "dynamic"


def target_fields(head: Head) -> dict[str, Any]:
    state = head.state
    value = state.structured_value if isinstance(state.structured_value, dict) else {}
    memory = value.get("memory") or {}
    legacy = value.get("legacy") or {}
    fields = {
        "content": state.content,
        "domain": _as_list(value.get("domain")) or ["未分类"],
        "tags": _as_list(value.get("tags")),
        "importance": _importance(value.get("importance")),
        "valence": value.get("valence") if value.get("valence") is not None else 0.5,
        "arousal": value.get("arousal") if value.get("arousal") is not None else 0.3,
        "pinned": bool(memory.get("pinned", legacy.get("pinned", False))),
    }
    # Without an explicit title Ombre names the bucket itself; never fight it over that.
    title = str(value.get("title") or "").strip()
    if title:
        fields["name"] = title
    return fields


# ----------------------------------------------------------- Ombre semantics

def _norm_field(name: str, value: Any) -> Any:
    if name == "content":
        return _clean(str(value or ""))
    if name in ("domain", "tags"):
        return sorted(str(v) for v in _as_list(value))
    if name == "pinned":
        return bool(value)
    if name == "importance":
        return int(value) if value is not None else None
    if name in ("valence", "arousal"):
        return round(float(value), 6) if value is not None else None
    return value


def observed_semantics(bucket: dict[str, Any]) -> dict[str, Any]:
    """What Ombre actually holds for the projected fields, normalised for comparison."""
    meta = bucket.get("metadata") or {}
    raw = {"content": bucket.get("content"), **{k: meta.get(k) for k in SEMANTIC_FIELDS if k != "content"}}
    return {k: _norm_field(k, raw.get(k)) for k in SEMANTIC_FIELDS}


def observed_sha(bucket: dict[str, Any]) -> str:
    return _sha(json.dumps(observed_semantics(bucket), ensure_ascii=False, sort_keys=True))


def _norm_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: _norm_field(k, v) for k, v in fields.items() if k in SEMANTIC_FIELDS}


def _dumps_norm(fields: dict[str, Any]) -> str:
    return json.dumps(_norm_fields(fields), ensure_ascii=False, sort_keys=True)


def _loads(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def changed_fields(target: dict[str, Any], bucket: dict[str, Any], row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Target fields Ombre doesn't hold yet.

    A field whose input is unchanged since the last projection and whose stored value
    is what Ombre made of it last time (e.g. pinned -> importance 10) is already held.
    """
    current = observed_semantics(bucket)
    last_target = _loads((row or {}).get("target_json"))
    last_observed = _loads((row or {}).get("observed_json"))
    delta = {}
    for name, value in target.items():
        wanted = _norm_field(name, value)
        if wanted == current.get(name):
            continue
        if name in last_target and last_target[name] == wanted and last_observed.get(name) == current.get(name):
            continue
        delta[name] = value
    return delta


def classify(head: "Head", row: dict[str, Any] | None, bucket: dict[str, Any]) -> str:
    """How a bucket relates to the observed baseline.

    baseline  -- unchanged since we last observed it
    ours      -- our own unconfirmed update (intent) landed: every field we sent
                 holds what we sent, and anything else that moved is a field Ombre
                 rewrites itself (OMBRE_REWRITTEN); or it already holds the target
    external  -- changed by someone else inside Ombre
    unknown   -- nothing observed yet

    Limitation: an outside edit to one of OMBRE_REWRITTEN, made inside the window
    between our update landing and its bookkeeping, is taken as ours.
    """
    if not row or not row.get("observed_sha"):
        return "unknown"
    if observed_sha(bucket) == row["observed_sha"]:
        return "baseline"
    intent = _loads(row.get("intent_json"))
    sent = (intent.get("fields") or {}) if intent.get("op", "update") == "update" else {}
    # Any update we sent counts, even one for an older head: the current head's own
    # delta is still computed and sent afterwards (see _decide).
    if sent:
        baseline = _loads(row.get("observed_json"))
        current = observed_semantics(bucket)
        moved = {k for k in SEMANTIC_FIELDS if current.get(k) != baseline.get(k)}
        landed = all(current.get(k) == v or k in OMBRE_REWRITTEN for k, v in sent.items())
        if landed and (moved - set(sent)) <= OMBRE_REWRITTEN:
            return "ours"
    if not changed_fields(target_fields(head), bucket, row):
        return "ours"
    return "external"


def projection_marked(bucket: dict[str, Any]) -> bool:
    """Carries any projection provenance (current worker or the earlier file projector)."""
    meta = bucket.get("metadata") or {}
    return str(meta.get("source_tool") or "") in PROJECTION_SOURCE_TOOLS or bool(meta.get("kernel_lineage_id"))


def created_by_worker(bucket: dict[str, Any]) -> bool:
    return str((bucket.get("metadata") or {}).get("source_tool") or "") == WORKER_SOURCE_TOOL


def _as_bucket(fields: dict[str, Any]) -> dict[str, Any]:
    """The bucket view of a target, for a baseline taken from the Kernel itself."""
    return {"content": fields.get("content"), "metadata": {k: v for k, v in fields.items() if k != "content"}}


def is_system_bucket(bucket: dict[str, Any]) -> bool:
    return bool(SYSTEM_DOMAINS & set(_as_list((bucket.get("metadata") or {}).get("domain"))))


# ------------------------------------------------------------ Ombre runtime

class OmbreRuntime:
    """Ombre's official runtime, reached through the worker in Ombre's interpreter."""

    def __init__(self, python: str, src_dir: str, env_file: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.python, self.src_dir, self.env_file, self.timeout = python, src_dir, env_file, timeout

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OmbreRuntime | None":
        """Configured explicitly (OMBRE_RUNTIME_PYTHON / _SRC / _ENV_FILE); no guessed paths."""
        source = env if env is not None else os.environ
        values = [source.get(k, "").strip() for k in ("OMBRE_RUNTIME_PYTHON", "OMBRE_RUNTIME_SRC", "OMBRE_RUNTIME_ENV_FILE")]
        if not all(values):
            return None
        try:
            timeout = float(source.get("OMBRE_RUNTIME_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
        except ValueError:
            timeout = DEFAULT_TIMEOUT_SECONDS
        return cls(*values, timeout=timeout)

    def batch(self, requests: list[dict[str, Any]]) -> list[Any]:
        """One worker run. Each result is the op's value, or a RequestFailed for that request."""
        try:
            done = subprocess.run(
                [self.python, str(WORKER), self.src_dir, self.env_file],
                input=json.dumps(requests, ensure_ascii=False), capture_output=True,
                text=True, timeout=self.timeout, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeUnavailable(f"worker_unreachable:{type(exc).__name__}") from exc
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            answer = json.loads(lines[-1]) if lines else {}
        except ValueError:
            answer = {}
        if not answer.get("ok"):
            detail = answer.get("detail") or done.stderr[-300:]
            raise RuntimeUnavailable(f"{answer.get('error') or 'worker_failed'}:{detail}")
        return [item["value"] if item.get("ok") else RequestFailed(item.get("error") or "failed")
                for item in answer["results"]]

    def _one(self, request: dict[str, Any]) -> Any:
        result = self.batch([request])[0]
        if isinstance(result, RequestFailed):
            raise RuntimeUnavailable(str(result))
        return result

    def capabilities(self) -> dict[str, Any]:
        return self._one({"op": "capabilities"})

    def get(self, bucket_id: str) -> dict[str, Any] | None:
        return self._one({"op": "get", "bucket_id": bucket_id})

    def create_if_absent(self, bucket_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """get, create only if absent, get again: {"created", "bucket_id", "bucket"}.

        bucket_id is the id Ombre actually used: on a collision Ombre 3.6 silently
        picks a suffixed or random id instead of failing."""
        return self._one({"op": "create_if_absent", "bucket_id": bucket_id, "fields": fields})

    def update(self, bucket_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        """update then get in one worker run; returns what Ombre holds afterwards."""
        results = self.batch([{"op": "update", "bucket_id": bucket_id, "fields": fields},
                              {"op": "get", "bucket_id": bucket_id}])
        for result in results:
            if isinstance(result, RequestFailed):
                raise RuntimeUnavailable(str(result))
        return results[1]

    def list(self, *, include_archive: bool = True) -> list[dict[str, Any]]:
        return self._one({"op": "list", "include_archive": include_archive})


# --------------------------------------------------------- projection state

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_lineage (
    lineage_id TEXT PRIMARY KEY,
    bucket_id TEXT NOT NULL UNIQUE,
    target_state_id TEXT NOT NULL,
    projected_state_id TEXT,
    observed_json TEXT,
    observed_sha TEXT,
    status TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    target_json TEXT,
    intent_json TEXT
)
"""
# target_json: the (normalised) fields last projected, so Ombre's own normalisation of
# an unchanged input is not re-sent. intent_json: an update sent but not yet confirmed.
_STATE_COLUMNS = {"target_json": "TEXT", "intent_json": "TEXT"}
# Buckets Ombre created for us under an id we did not ask for (its silent re-id on a
# collision). They are ours, so never ingested back as new lineages, and never adopted.
_QUARANTINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS projection_quarantine (
    bucket_id TEXT PRIMARY KEY,
    lineage_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
# The projection's own provenance marks on a bucket. WORKER_SOURCE_TOOL is set only by
# our worker's create; the earlier file projector wrote "memory_kernel_projection" and a
# kernel_lineage_id stamp on buckets it created *or rewrote*, so that stamp proves we
# touched the bucket once, not that nobody edited it since.
WORKER_SOURCE_TOOL = "kernel_projection"
PROJECTION_SOURCE_TOOLS = frozenset({WORKER_SOURCE_TOOL, "memory_kernel_projection"})


class ProjectionState:
    """Projection bookkeeping, kept outside both the Kernel and Ombre."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._tx: sqlite3.Connection | None = None
        if not self.path.exists():
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
        os.chmod(self.path, 0o600)
        with self._conn() as conn:
            conn.execute(_STATE_SCHEMA)
            conn.execute(_QUARANTINE_SCHEMA)
            have = {r[1] for r in conn.execute("PRAGMA table_info(projection_lineage)")}
            for name, kind in _STATE_COLUMNS.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE projection_lineage ADD COLUMN {name} {kind}")
        self.lock_dir = Path(f"{self.path}.locks")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._tx is not None:  # inside transaction(): share it, commit at its end
            yield self._tx
            return
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group bookkeeping writes into one commit (one fsync instead of one per row).

        All or nothing: a crash inside loses the whole group, which retries recover
        (create_if_absent gets first; a landed update is recognised by its intent).
        """
        if self._tx is not None:
            yield
            return
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        self._tx = conn
        try:
            with conn:
                yield
        finally:
            self._tx = None
            conn.close()

    def rows_for(self, lineage_ids: list[str]) -> dict[str, dict[str, Any]]:
        wanted = sorted(set(lineage_ids))
        found: dict[str, dict[str, Any]] = {}
        with self._conn() as conn:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                marks = ",".join("?" * len(chunk))
                for row in conn.execute(f"SELECT * FROM projection_lineage WHERE lineage_id IN ({marks})", chunk):
                    found[row["lineage_id"]] = dict(row)
        return found

    def get(self, lineage_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM projection_lineage WHERE lineage_id=?", (lineage_id,)).fetchone()
        return dict(row) if row else None

    def rows(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projection_lineage ORDER BY lineage_id")]

    def target(self, lineage_id: str, bucket_id: str, state_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO projection_lineage (lineage_id, bucket_id, target_state_id, status, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?) ON CONFLICT(lineage_id) DO UPDATE SET "
                "target_state_id=excluded.target_state_id, updated_at=excluded.updated_at, "
                "status=CASE WHEN projected_state_id=excluded.target_state_id THEN status ELSE 'pending' END",
                (lineage_id, bucket_id, state_id, _now()),
            )
        return self.get(lineage_id) or {}

    def projected(self, lineage_id: str, state_id: str, bucket: dict[str, Any], target: dict[str, Any],
                  *, status: str = "projected", error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET projected_state_id=?, target_state_id=?, observed_json=?, "
                "observed_sha=?, target_json=?, intent_json=NULL, status=?, error=?, attempts=0, "
                "updated_at=? WHERE lineage_id=?",
                (state_id, state_id, json.dumps(observed_semantics(bucket), ensure_ascii=False, sort_keys=True),
                 observed_sha(bucket), _dumps_norm(target), status, error, _now(), lineage_id),
            )

    def quarantine(self, bucket_id: str, lineage_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO projection_quarantine (bucket_id, lineage_id, reason, created_at) "
                "VALUES (?, ?, ?, ?)", (bucket_id, lineage_id, reason[:120], _now()))

    def quarantined(self) -> dict[str, dict[str, Any]]:
        with self._conn() as conn:
            return {r["bucket_id"]: dict(r) for r in conn.execute("SELECT * FROM projection_quarantine")}

    def intend(self, lineage_id: str, state_id: str, fields: dict[str, Any], *, op: str = "update") -> None:
        """Record a create or update before sending it, so a crash after Ombre applied
        it is recognised -- and anything else found later is not mistaken for ours."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET intent_json=?, updated_at=? WHERE lineage_id=?",
                (json.dumps({"op": op, "state_id": state_id, "fields": _norm_fields(fields)},
                            ensure_ascii=False, sort_keys=True), _now(), lineage_id),
            )

    @contextmanager
    def locked(self, lineage_ids: list[str]) -> Iterator[None]:
        """Cross-process exclusive lock per lineage, taken in sorted order (no deadlock)."""
        self.lock_dir.mkdir(mode=0o700, exist_ok=True)
        with ExitStack() as stack:
            for lineage_id in sorted(set(lineage_ids)):
                path = self.lock_dir / (hashlib.sha256(lineage_id.encode("utf-8")).hexdigest()[:24] + ".lock")
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX)
                stack.callback(fcntl.flock, fd, fcntl.LOCK_UN)
            yield

    def clear_intent(self, lineage_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE projection_lineage SET intent_json=NULL WHERE lineage_id=?", (lineage_id,))

    def forget(self, lineage_id: str, reason: str = "bucket_missing_in_ombre") -> None:
        """Drop the baseline of a bucket that no longer exists, so it is recreated."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET projected_state_id=NULL, observed_json=NULL, observed_sha=NULL, "
                "target_json=NULL, intent_json=NULL, status='pending', error=?, updated_at=? WHERE lineage_id=?",
                (reason, _now(), lineage_id))

    def failed(self, lineage_id: str, status: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET status=?, error=?, attempts=attempts+1, updated_at=? WHERE lineage_id=?",
                (status, error[:300], _now(), lineage_id),
            )


def state_path_for(kernel_db: str | os.PathLike[str]) -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_PROJECTION_DB") or f"{kernel_db}.projection.db")


# ----------------------------------------------------------------- projection

def project_lineage(head: Head, runtime: OmbreRuntime, state: ProjectionState, *, kernel_db: Path) -> str:
    """Make Ombre hold the lineage's *current* head (``head`` may be stale by the
    time the lock is held): projected / unchanged / pending / diverged / conflict."""
    state.target(head.lineage_id, head.bucket_id, head.state.state_id)
    return _project_batch([head], runtime, state, kernel_db)[0]


def project_heads(heads: list[Head], runtime: OmbreRuntime, state: ProjectionState,
                  *, kernel_db: Path, batch_size: int = BATCH_SIZE) -> dict[str, int]:
    counts = {"projected": 0, "unchanged": 0, "pending": 0, "diverged": 0, "conflict": 0}
    rows = {row["lineage_id"]: row for row in state.rows()}
    todo = []
    with state.transaction():
        for head in heads:
            if _is_projected(rows.get(head.lineage_id), head):
                counts["unchanged"] += 1
                continue
            state.target(head.lineage_id, head.bucket_id, head.state.state_id)
            todo.append(head)
    for start in range(0, len(todo), batch_size):
        for outcome in _project_batch(todo[start:start + batch_size], runtime, state, kernel_db):
            counts[outcome] += 1
    return counts


def _is_projected(row: dict[str, Any] | None, head: Head) -> bool:
    return bool(row) and row["status"] == "projected" and row["projected_state_id"] == head.state.state_id


def _project_batch(heads: list[Head], runtime: OmbreRuntime, state: ProjectionState,
                   kernel_db: Path) -> list[str]:
    """At most two worker runs for the batch: (create_if_absent | get), then update+get.

    Held under each lineage's cross-process lock, so a request-time write and a
    sync never project the same lineage at once (Ombre would mint a second id).
    Under the lock both the bookkeeping *and the Kernel head* are re-read: a sync
    holding an older snapshot must never write a stale head over a newer one.
    """
    ids = [h.lineage_id for h in heads]
    with state.locked(ids):
        outcomes: dict[str, str] = {}
        items = []
        fresh = state.rows_for(ids)
        latest = current_heads(kernel_db, ids)
        with state.transaction():
            for head in heads:
                current = latest.get(head.lineage_id)
                if current is None:
                    outcomes[head.lineage_id] = _pending(head, state, "kernel_head_unresolved")
                    continue
                if current.state.state_id != head.state.state_id:
                    state.target(current.lineage_id, current.bucket_id, current.state.state_id)
                    head = current  # the snapshot was stale: project what the Kernel holds now
                row = fresh.get(head.lineage_id) or {}
                if _is_projected(row, head):
                    outcomes[head.lineage_id] = "unchanged"
                else:
                    items.append((head, row))
        try:
            _run_batch(items, runtime, state, outcomes)
        except RuntimeUnavailable as exc:
            with state.transaction():
                for head, _ in items:
                    if head.lineage_id not in outcomes:
                        outcomes[head.lineage_id] = _pending(head, state, str(exc))
        return [outcomes[head.lineage_id] for head in heads]


def _run_batch(items: list[tuple[Head, dict[str, Any]]], runtime: OmbreRuntime, state: ProjectionState,
               outcomes: dict[str, str]) -> None:
    if not items:
        return
    with state.transaction():  # durable before the creates go out
        for head, row in items:
            if not row.get("observed_sha"):
                state.intend(head.lineage_id, head.state.state_id, target_fields(head), op="create")
    first = runtime.batch([
        # Never observed: create unless Ombre already has it (imported bucket, or a
        # run that crashed after Ombre wrote) -- the worker gets before it creates.
        {"op": "create_if_absent", "bucket_id": head.bucket_id,
         "fields": {**target_fields(head), "bucket_type": bucket_type_for(head)}}
        if not row.get("observed_sha") else {"op": "get", "bucket_id": head.bucket_id}
        for head, row in items
    ])
    updates: list[tuple[Head, dict[str, Any]]] = []
    # One commit for the batch's first-phase outcomes and the intents of the updates
    # about to be sent; it must be durable before the updates go out.
    with state.transaction():
        for (head, row), result in zip(items, first):
            outcome, delta = _first_phase(head, row, result, state)
            if delta:
                updates.append((head, delta))
                state.intend(head.lineage_id, head.state.state_id, delta)
            else:
                outcomes[head.lineage_id] = outcome
    if not updates:
        return
    requests = []
    for head, delta in updates:
        requests += [{"op": "update", "bucket_id": head.bucket_id, "fields": delta},
                     {"op": "get", "bucket_id": head.bucket_id}]
    results = runtime.batch(requests)
    with state.transaction():
        for index, (head, _) in enumerate(updates):
            updated, after = results[2 * index], results[2 * index + 1]
            if isinstance(updated, RequestFailed):
                state.clear_intent(head.lineage_id)  # Ombre answered: nothing was committed
            failure = next((r for r in (updated, after) if isinstance(r, RequestFailed)), None)
            outcomes[head.lineage_id] = (_pending(head, state, failure) if failure is not None
                                         else _record(head, state, after))


def _first_phase(head: Head, row: dict[str, Any], result: Any,
                 state: ProjectionState) -> tuple[str, dict[str, Any]]:
    """Outcome of (create_if_absent | get) for one lineage, or the delta to update."""
    if isinstance(result, RequestFailed):
        return _pending(head, state, result), {}
    if not row.get("observed_sha"):
        actual = result.get("bucket_id") or head.bucket_id
        if actual != head.bucket_id:
            # Ombre re-id'd our create: that bucket is ours but under a foreign id.
            # Quarantine it so it is never ingested back as a new lineage.
            state.quarantine(actual, head.lineage_id, "ombre_reassigned_id")
            state.failed(head.lineage_id, "conflict", f"ombre_assigned_other_id:{actual}")
            return "conflict", {}
        if result["created"]:
            return _record(head, state, result["bucket"]), {}
        result = result["bucket"]
        adopted = _adopt(head, row, result, state)
        if adopted:
            return adopted, {}
        row = state.get(head.lineage_id) or {}  # _adopt recorded our own create as baseline
    return _decide(head, row, result, state)


def _adopt(head: Head, row: dict[str, Any], bucket: dict[str, Any] | None, state: ProjectionState) -> str | None:
    """A bucket already sits at our id and we have no baseline for it: whose is it?

    A bucket without a baseline is never updated from here. A marker on the
    bucket (our source_tool, the old projector's stamp) proves who made it, not
    that nobody edited it since. Only our own unconfirmed create, matching what
    we sent, is taken as baseline -- then None lets the normal path project a
    newer head. Anything else that belongs to this lineage is diverged, so the
    edit is ingested; anything else at all is a conflict.
    """
    if bucket is None:
        return None
    if bucket.get("deleted"):
        state.failed(head.lineage_id, "conflict", "bucket_deleted_in_ombre")
        return "conflict"
    target = target_fields(head)
    if not changed_fields(target, bucket):
        return _record(head, state, bucket)  # already holds the target: take it as baseline
    intent = _loads(row.get("intent_json"))
    if created_by_worker(bucket) and intent.get("op") == "create" and _holds(bucket, intent.get("fields") or {}):
        # Our create landed and nothing changed it since; Ombre's own rewrites allowed.
        state.projected(head.lineage_id, intent["state_id"], bucket, intent["fields"])
        return None
    stamped = (bucket.get("metadata") or {}).get("kernel_lineage_id")
    if created_by_worker(bucket) or head.lineage_id == "lin:ombre:" + head.bucket_id or stamped == head.lineage_id:
        # This lineage's own bucket (our worker's, imported from it, or written by the
        # earlier file projector), edited inside Ombre -- or we can no longer prove it
        # wasn't (bookkeeping lost). Never overwrite: take the Kernel head as the
        # baseline, so the next scan sees the difference and ingests it as a revision.
        state.projected(head.lineage_id, head.state.state_id, _as_bucket(target), target,
                        status="diverged", error="edited_in_ombre_before_baseline")
        return "diverged"
    state.failed(head.lineage_id, "conflict", "bucket_id_taken_by_unowned_bucket")
    return "conflict"


def _holds(bucket: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Bucket still holds these (normalised) fields, up to the ones Ombre rewrites itself."""
    current = observed_semantics(bucket)
    return bool(fields) and all(current.get(k) == v or k in OMBRE_REWRITTEN for k, v in fields.items())


def _decide(head: Head, row: dict[str, Any], bucket: dict[str, Any] | None,
            state: ProjectionState) -> tuple[str, dict[str, Any]]:
    if bucket is None:
        # Gone without a trace (a rebuilt vault): forget the baseline so the next
        # run recreates it. A soft-deleted bucket is a conflict instead.
        state.forget(head.lineage_id)
        return _pending(head, state, "bucket_missing_in_ombre"), {}
    if bucket.get("deleted"):
        state.failed(head.lineage_id, "conflict", "bucket_deleted_in_ombre")
        return "conflict", {}
    kind = classify(head, row, bucket)
    if kind == "external":
        state.failed(head.lineage_id, "diverged", "edited_in_ombre")
        return "diverged", {}
    delta = changed_fields(target_fields(head), bucket, row)
    return ("", delta) if delta else (_record(head, state, bucket), {})


def _record(head: Head, state: ProjectionState, bucket: dict[str, Any] | None) -> str:
    if bucket is None:
        return _pending(head, state, "bucket_missing_after_write")
    state.projected(head.lineage_id, head.state.state_id, bucket, target_fields(head))
    return "projected"


def _pending(head: Head, state: ProjectionState, error: str) -> str:
    state.failed(head.lineage_id, "pending", str(error))
    return "pending"


def scan_ombre(heads: list[Head], runtime: OmbreRuntime, state: ProjectionState) -> dict[str, Any]:
    """Compare Ombre with the observed baselines.

    diverged      -- edited inside Ombre: (head, bucket), to ingest as a revision
    missing       -- gone without a trace (rebuilt vault): to recreate
    soft_deleted  -- deleted inside Ombre: a decision to keep, never revived
    unmanaged     -- created by Ombre on its own: to ingest as new lineages
    orphaned      -- projection-made buckets no lineage owns (quarantined re-ids,
                     or marked by us): never ingested, reported for cleanup
    """
    buckets = {b["id"]: b for b in runtime.list(include_archive=True)}
    rows = {row["lineage_id"]: row for row in state.rows()}
    quarantined = state.quarantined()
    managed = {row["bucket_id"] for row in rows.values()} | {h.bucket_id for h in heads}
    diverged, missing, soft_deleted = [], [], []
    for head in heads:
        row = rows.get(head.lineage_id)
        if not row or not row.get("observed_sha") or row["status"] not in ("projected", "diverged", "pending", "conflict"):
            continue
        bucket = buckets.get(row["bucket_id"])
        if bucket is None:
            missing.append(head.lineage_id)
        elif bucket.get("deleted"):
            soft_deleted.append(head.lineage_id)
        elif classify(head, row, bucket) == "external":
            diverged.append((head, bucket))
    unmanaged, orphaned = [], []
    for bucket_id, bucket in sorted(buckets.items()):
        if bucket_id in managed or bucket.get("deleted") or is_system_bucket(bucket):
            continue
        (orphaned if bucket_id in quarantined or projection_marked(bucket) else unmanaged).append(bucket)
    return {"diverged": diverged, "missing": missing, "soft_deleted": soft_deleted,
            "unmanaged": unmanaged, "orphaned": orphaned}


def apply_lifecycle(scan: dict[str, Any], state: ProjectionState) -> None:
    """Act on what the scan found before projecting: recreate missing, keep deletions."""
    for lineage_id in scan["missing"]:
        state.forget(lineage_id)
    for lineage_id in scan["soft_deleted"]:
        state.failed(lineage_id, "conflict", "bucket_deleted_in_ombre")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kernel -> Ombre projection status")
    parser.add_argument("--kernel-db", required=True, type=Path)
    parser.add_argument("command", choices=("capabilities", "status", "project"))
    args = parser.parse_args(argv)
    runtime = OmbreRuntime.from_env()
    if runtime is None:
        print(json.dumps({"ok": False, "error": "ombre_runtime_unconfigured"}))
        return 2
    if args.command == "capabilities":
        print(json.dumps(runtime.capabilities(), ensure_ascii=False))
        return 0
    state = ProjectionState(state_path_for(args.kernel_db))
    heads, problems = select_heads(args.kernel_db)
    if args.command == "project":
        result: dict[str, Any] = project_heads(heads, runtime, state, kernel_db=args.kernel_db)
    else:
        scan = scan_ombre(heads, runtime, state)
        result = {key: len(scan[key]) for key in ("diverged", "missing", "soft_deleted", "unmanaged", "orphaned")}
        result["heads"] = len(heads)
    result["problems"] = problems
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
