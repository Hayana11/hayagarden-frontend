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
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tools.memory_kernel import MemoryKernel, State

WORKER = Path(__file__).with_name("ombre_projection_worker.py")
# What the projection owns in a bucket; lifecycle (resolved, activation, decay,
# archive placement, dont_surface) stays Ombre's.
SEMANTIC_FIELDS = ("content", "name", "domain", "tags", "importance", "valence", "arousal", "pinned")
# Ombre's handover bucket is its own runtime state, never Kernel-managed.
SYSTEM_DOMAINS = {"呼吸间"}
DEFAULT_TIMEOUT_SECONDS = 60.0
# Lineages per worker run: importing Ombre costs ~1.4s, a request a few ms.
BATCH_SIZE = 50


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
    return {
        "content": state.content,
        "name": str(value.get("title") or "").strip() or _clean(state.content)[:20],
        "domain": _as_list(value.get("domain")) or ["未分类"],
        "tags": _as_list(value.get("tags")),
        "importance": _importance(value.get("importance")),
        "valence": value.get("valence") if value.get("valence") is not None else 0.5,
        "arousal": value.get("arousal") if value.get("arousal") is not None else 0.3,
        "pinned": bool(memory.get("pinned", legacy.get("pinned", False))),
    }


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


def changed_fields(target: dict[str, Any], bucket: dict[str, Any]) -> dict[str, Any]:
    current = observed_semantics(bucket)
    return {k: v for k, v in target.items() if _norm_field(k, v) != current.get(k)}


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
        """get, create only if absent, get again: {"created": bool, "bucket": view}."""
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
    updated_at TEXT NOT NULL
)
"""


class ProjectionState:
    """Projection bookkeeping, kept outside both the Kernel and Ombre."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        if not self.path.exists():
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600))
        os.chmod(self.path, 0o600)
        with self._conn() as conn:
            conn.execute(_STATE_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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

    def projected(self, lineage_id: str, state_id: str, bucket: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET projected_state_id=?, target_state_id=?, observed_json=?, "
                "observed_sha=?, status='projected', error=NULL, attempts=0, updated_at=? WHERE lineage_id=?",
                (state_id, state_id, json.dumps(observed_semantics(bucket), ensure_ascii=False, sort_keys=True),
                 observed_sha(bucket), _now(), lineage_id),
            )

    def forget(self, lineage_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET projected_state_id=NULL, observed_json=NULL, observed_sha=NULL, "
                "updated_at=? WHERE lineage_id=?", (_now(), lineage_id))

    def failed(self, lineage_id: str, status: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE projection_lineage SET status=?, error=?, attempts=attempts+1, updated_at=? WHERE lineage_id=?",
                (status, error[:300], _now(), lineage_id),
            )


def state_path_for(kernel_db: str | os.PathLike[str]) -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_PROJECTION_DB") or f"{kernel_db}.projection.db")


# ----------------------------------------------------------------- projection

def project_lineage(head: Head, runtime: OmbreRuntime, state: ProjectionState) -> str:
    """Make Ombre hold ``head``; returns projected / pending / diverged / conflict."""
    row = state.target(head.lineage_id, head.bucket_id, head.state.state_id)
    return _project_batch([(head, row)], runtime, state)[0]


def project_heads(heads: list[Head], runtime: OmbreRuntime, state: ProjectionState,
                  *, batch_size: int = BATCH_SIZE) -> dict[str, int]:
    counts = {"projected": 0, "unchanged": 0, "pending": 0, "diverged": 0, "conflict": 0}
    todo = []
    for head in heads:
        row = state.get(head.lineage_id)
        if row and row["status"] == "projected" and row["projected_state_id"] == head.state.state_id:
            counts["unchanged"] += 1
            continue
        todo.append((head, state.target(head.lineage_id, head.bucket_id, head.state.state_id)))
    for start in range(0, len(todo), batch_size):
        for outcome in _project_batch(todo[start:start + batch_size], runtime, state):
            counts[outcome] += 1
    return counts


def _project_batch(items: list[tuple[Head, dict[str, Any]]], runtime: OmbreRuntime,
                   state: ProjectionState) -> list[str]:
    """At most two worker runs for the whole batch: (create_if_absent | get), then update+get."""
    outcomes: dict[str, str] = {}
    try:
        first = runtime.batch([
            # Never observed: create unless Ombre already has it (imported bucket, or a
            # run that crashed after Ombre wrote) -- the worker gets before it creates.
            {"op": "create_if_absent", "bucket_id": head.bucket_id,
             "fields": {**target_fields(head), "bucket_type": bucket_type_for(head)}}
            if not row.get("observed_sha") else {"op": "get", "bucket_id": head.bucket_id}
            for head, row in items
        ])
        updates: list[tuple[Head, dict[str, Any]]] = []
        for (head, row), result in zip(items, first):
            if isinstance(result, RequestFailed):
                outcomes[head.lineage_id] = _pending(head, state, result)
                continue
            if not row.get("observed_sha"):
                if result["created"]:
                    outcomes[head.lineage_id] = _record(head, state, result["bucket"])
                    continue
                result = result["bucket"]
            outcome, delta = _decide(head, row, result, state)
            if delta:
                updates.append((head, delta))
            elif outcome:
                outcomes[head.lineage_id] = outcome
        if updates:
            requests = []
            for head, delta in updates:
                requests += [{"op": "update", "bucket_id": head.bucket_id, "fields": delta},
                             {"op": "get", "bucket_id": head.bucket_id}]
            results = runtime.batch(requests)
            for index, (head, _) in enumerate(updates):
                updated, after = results[2 * index], results[2 * index + 1]
                failure = next((r for r in (updated, after) if isinstance(r, RequestFailed)), None)
                outcomes[head.lineage_id] = (_pending(head, state, failure) if failure is not None
                                             else _record(head, state, after))
    except RuntimeUnavailable as exc:
        for head, _ in items:
            if head.lineage_id not in outcomes:
                outcomes[head.lineage_id] = _pending(head, state, str(exc))
    return [outcomes[head.lineage_id] for head, _ in items]


def _decide(head: Head, row: dict[str, Any], bucket: dict[str, Any] | None,
            state: ProjectionState) -> tuple[str | None, dict[str, Any]]:
    if bucket is None:
        # Gone without a trace (a rebuilt vault): forget the baseline so the next
        # run recreates it. A soft-deleted bucket is a conflict instead.
        state.forget(head.lineage_id)
        return _pending(head, state, "bucket_missing_in_ombre"), {}
    if bucket.get("deleted"):
        state.failed(head.lineage_id, "conflict", "bucket_deleted_in_ombre")
        return "conflict", {}
    if row.get("observed_sha") and observed_sha(bucket) != row["observed_sha"]:
        state.failed(head.lineage_id, "diverged", "edited_in_ombre")
        return "diverged", {}
    delta = changed_fields(target_fields(head), bucket)
    return (None, delta) if delta else (_record(head, state, bucket), {})


def _record(head: Head, state: ProjectionState, bucket: dict[str, Any] | None) -> str:
    if bucket is None:
        return _pending(head, state, "bucket_missing_after_write")
    state.projected(head.lineage_id, head.state.state_id, bucket)
    return "projected"


def _pending(head: Head, state: ProjectionState, error: str) -> str:
    state.failed(head.lineage_id, "pending", str(error))
    return "pending"


def scan_ombre(heads: list[Head], runtime: OmbreRuntime, state: ProjectionState) -> dict[str, Any]:
    """Compare Ombre with the observed baselines: edits, deletions, and buckets we don't manage."""
    buckets = {b["id"]: b for b in runtime.list(include_archive=True)}
    rows = {row["lineage_id"]: row for row in state.rows()}
    managed = {row["bucket_id"] for row in rows.values()} | {h.bucket_id for h in heads}
    diverged, deleted = [], []
    for head in heads:
        row = rows.get(head.lineage_id)
        if not row or row["status"] not in ("projected", "diverged") or not row.get("observed_sha"):
            continue
        bucket = buckets.get(row["bucket_id"])
        if bucket is None or bucket.get("deleted"):
            deleted.append(head.lineage_id)
        elif observed_sha(bucket) != row["observed_sha"]:
            diverged.append((head, bucket))
    unmanaged = [
        b for bucket_id, b in sorted(buckets.items())
        if bucket_id not in managed and not b.get("deleted") and not is_system_bucket(b)
    ]
    return {"diverged": diverged, "deleted": deleted, "unmanaged": unmanaged}


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
        result: dict[str, Any] = project_heads(heads, runtime, state)
    else:
        scan = scan_ombre(heads, runtime, state)
        result = {"heads": len(heads), "diverged": len(scan["diverged"]), "deleted": len(scan["deleted"]),
                  "unmanaged": len(scan["unmanaged"])}
    result["problems"] = problems
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
