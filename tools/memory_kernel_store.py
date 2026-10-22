"""B-3: Kernel-first memory writes for the live runtime, and Ombre catch-up.

remember() commits Evidence + State + formation Delta to the Kernel first and
then projects that lineage into Ombre through Ombre's own runtime (see
memory_kernel_projection). If Ombre is unconfigured or unavailable the lineage
stays ``pending`` in the projection state and the next sync() retries it; the
Kernel write already succeeded, so nothing ever falls back to posts.

sync() keeps the two sides converged without ever letting Ombre be the
authority: buckets Ombre created on its own (letters, plans, anything a tool
wrote directly) become new lineages, and buckets edited inside Ombre (trace,
the dashboard) become Kernel revisions with a Delta, before the projection
runs. Lifecycle fields stay Ombre's.

Ombre is reached only when OMBRE_RUNTIME_PYTHON / OMBRE_RUNTIME_SRC /
OMBRE_RUNTIME_ENV_FILE are set; there is no default vault path.

The switch is runtime_config key ``memory.kernel_write`` in memories.db, so
gateway, internal-mcp and the capability proxy all read the same flag.

Usage:
    python -m tools.memory_kernel_store sync|status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools import memory_kernel_projection as projection
from tools.memory_kernel import Delta, Evidence, MemoryKernel, State
from tools.memory_kernel_switch import FLAG_KEY, write_enabled  # noqa: F401 (re-exported)

DEFAULT_KERNEL_DB = "/var/lib/hayagarden/memory-kernel.db"
DEFAULT_POSTS_DB = "/opt/frontend/memories.db"


def kernel_db_path() -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_DB") or DEFAULT_KERNEL_DB)


def posts_db_path() -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_POSTS_DB") or DEFAULT_POSTS_DB)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(value: Any, fallback: str) -> str:
    """Ombre wrote naive UTC timestamps before 3.x; the Kernel requires an offset."""
    if not value:
        return fallback
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKC", re.sub(r"\[\[|\]\]", "", text or "")).lower()
    return "".join(ch for ch in folded if ch.isalnum())


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(v) for v in (value or [])]


def default_runtime() -> projection.OmbreRuntime | None:
    return projection.OmbreRuntime.from_env()


_DEFAULT = object()


def _runtime(value: Any) -> projection.OmbreRuntime | None:
    return default_runtime() if value is _DEFAULT else value


def _project_one(head: projection.Head, db: Path, runtime: projection.OmbreRuntime | None) -> str:
    """Never raises: the Kernel row is committed, a failure only leaves it pending."""
    try:
        state = projection.ProjectionState(projection.state_path_for(db))
        state.target(head.lineage_id, head.bucket_id, head.state.state_id)
        if runtime is None:
            state.failed(head.lineage_id, "pending", "ombre_runtime_unconfigured")
            return "PENDING:unconfigured"
        outcome = projection.project_lineage(head, runtime, state, kernel_db=db)
    except Exception as exc:  # e.g. bookkeeping failed after Ombre wrote: retry gets first
        return f"PENDING:{type(exc).__name__}"
    return "PROJECTED" if outcome in ("projected", "unchanged") else outcome.upper()


MEMORY_TYPES = ("MEMORY", "FACT", "MISS")


def memory_semantics(memory_type: str, layer: str, pinned: Any, author: str | None) -> dict[str, Any]:
    """The legacy write semantics a memory must keep (see projection bucket placement)."""
    return {"type": memory_type, "layer": layer or "recent", "pinned": bool(pinned), "author": author or ""}


def find_head(content: str, *, kernel_db: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """The head already holding this memory (same normalised text), if any."""
    db = Path(kernel_db or kernel_db_path())
    if not db.is_file():
        return None
    text = content.strip()
    wanted = _norm(text)
    for lineage_id, state_id, existing in projection.head_contents(db):
        if _norm(existing) == wanted:
            state_db = projection.state_path_for(db)
            row = projection.ProjectionState(state_db).get(lineage_id) if state_db.is_file() else None
            ombre = "PROJECTED" if row and row["status"] == "projected" else "PENDING"
            return {"status": "DEDUPLICATED", "state_id": state_id, "lineage_id": lineage_id,
                    "bucket_id": projection.bucket_id_for(lineage_id), "ombre_status": ombre,
                    "content_sha256": _sha(text)}
    return None


def remember(
    content: Any,
    *,
    source: str,
    provenance: dict[str, Any] | None = None,
    title: str | None = None,
    tags: Any = None,
    importance: int | None = None,
    memory_type: str = "MEMORY",
    layer: str = "recent",
    pinned: Any = False,
    author: str | None = None,
    valence: float | None = None,
    arousal: float | None = None,
    occurred_at: str | None = None,
    kernel_db: str | os.PathLike[str] | None = None,
    runtime: Any = _DEFAULT,
    now: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        return {"status": "INVALID_CONTENT"}
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"not a memory type: {memory_type}")
    text = content.strip()
    db = Path(kernel_db or kernel_db_path())
    now = now or _now()

    existing = find_head(text, kernel_db=db)
    if existing is not None:
        return existing

    uid = uuid.uuid4().hex[:16]
    lineage = f"lin:k:{uid}"
    evidence = Evidence(
        evidence_id=f"ev:k:{uid}",
        source_type=f"memory_write:{source}",
        source_ref=str((provenance or {}).get("turn_id") or (provenance or {}).get("source_ref") or f"{source}:{uid}"),
        observed_at=now,
        occurred_at=occurred_at or now,
        content=text,
        integrity=_sha(text),
        provenance=_jsonable({"source": source, **(provenance or {})}),
        origin_kind="derived",
    )
    state = State(
        state_id=f"st:k:{uid}:v1",
        lineage_id=lineage,
        scope="unclassified",
        subject_ref="unresolved",
        representation="fact" if memory_type == "FACT" else "memory",
        content=text,
        structured_value=_jsonable({
            "title": title or "", "domain": [], "tags": _tags(tags), "importance": importance,
            "valence": valence, "arousal": arousal,
            "memory": memory_semantics(memory_type, layer, pinned, author),
        }),
        evidence_refs=(evidence.evidence_id,),
        confidence=0.6,
        epistemic_status="asserted_by_companion",
        status="active",
        created_at=occurred_at or now,
    )
    formation = Delta(
        delta_id=f"dl:k:{uid}:formation",
        target_scope=state.scope,
        before_ref=None,
        after_ref=state.state_id,
        trigger_evidence_refs=(evidence.evidence_id,),
        derived_by={"processor": "memory_kernel_store.remember", "source": source},
        confidence=0.6,
        occurred_at=now,
        review_status="accepted_by_policy",
    )
    kernel = MemoryKernel(db)
    kernel.create_state(state, formation, evidence=(evidence,))
    head = projection.Head(lineage, state)
    ombre_status = _project_one(head, db, _runtime(runtime))
    return {"status": "CREATED", "state_id": state.state_id, "lineage_id": lineage,
            "bucket_id": head.bucket_id, "ombre_status": ombre_status,
            "content_sha256": _sha(text)}


def _bucket_value(meta: dict[str, Any], *, ingested: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    value = dict(base or {})
    value.update({
        "title": str(meta.get("name") or ""),
        "domain": _tags(meta.get("domain")),
        "tags": _tags(meta.get("tags")),
        "importance": meta.get("importance"),
        "valence": meta.get("valence"),
        "arousal": meta.get("arousal"),
    })
    legacy = dict(value.get("legacy") or {})
    legacy.setdefault("ombre_type", meta.get("type"))
    legacy["last_ingest"] = ingested
    value["legacy"] = legacy
    memory = dict(value.get("memory") or {})
    memory.setdefault("type", "MEMORY")
    memory.setdefault("layer", "core" if meta.get("type") == "permanent" else "recent")
    # A pin set inside Ombre (anchor / trace) is a semantic decision the Kernel keeps.
    memory["pinned"] = bool(meta.get("pinned"))
    value["memory"] = memory
    return _jsonable(value)


def ingest_from_ombre(
    kernel_db: Path, runtime: projection.OmbreRuntime, state: projection.ProjectionState,
    *, now: str | None = None,
) -> dict[str, Any]:
    """Pull Ombre-side creations and edits into the Kernel; never the reverse."""
    now = now or _now()
    kernel = MemoryKernel(kernel_db)
    heads, _ = projection.select_heads(kernel_db)
    scan = projection.scan_ombre(heads, runtime, state)
    projection.apply_lifecycle(scan, state)
    created, revised, errors = [], [], []

    for bucket in scan["unmanaged"]:
        body = bucket.get("content") or ""
        text = projection._clean(body)
        if not text:
            continue
        try:
            _ingest_created(kernel, bucket["id"], bucket.get("metadata") or {}, body, text, now)
            created.append(bucket["id"])
        except Exception as exc:
            errors.append({"bucket_id": bucket["id"], "stage": "created", "error": type(exc).__name__})

    for head, bucket in scan["diverged"]:
        try:
            with state.locked([head.lineage_id]):
                # Re-check under the lock: another process may have revised this lineage
                # or recorded the bucket since the scan (no double revision, no branch).
                current = projection.current_heads(kernel_db, [head.lineage_id]).get(head.lineage_id)
                row = state.get(head.lineage_id)
                if current is None or current.state.state_id != head.state.state_id or \
                        projection.classify(head, row, bucket) != "external":
                    continue
                new_state_id = _ingest_edit(kernel, head, bucket.get("metadata") or {}, bucket.get("content") or "", now)
                # What Ombre holds now *is* the new head: it becomes the observed baseline.
                new_head = projection.Head(head.lineage_id, kernel.get_state(new_state_id))
                state.target(head.lineage_id, head.bucket_id, new_state_id)
                state.projected(head.lineage_id, new_state_id, bucket, projection.target_fields(new_head))
            revised.append(bucket["id"])
        except Exception as exc:
            errors.append({"bucket_id": bucket["id"], "stage": "edited", "error": type(exc).__name__})
    return {"created": created, "revised": revised, "missing": scan["missing"],
            "soft_deleted": scan["soft_deleted"], "orphaned": [b["id"] for b in scan["orphaned"]],
            "errors": errors}


def _ingest_created(kernel: MemoryKernel, bucket_id: str, meta: dict[str, Any], body: str, text: str, now: str) -> None:
    uid = uuid.uuid4().hex[:8]
    evidence = Evidence(
        evidence_id=f"ev:ombre:{bucket_id}:{uid}", source_type="ombre_bucket",
        source_ref=f"ombre:{bucket_id}", observed_at=now, occurred_at=_iso(meta.get("created"), now),
        content=body, integrity=_sha(body), origin_kind="derived",
        provenance={"system": "ombre", "ingested_by": "memory_kernel_store.ingest",
                    "source_tool": str(meta.get("source_tool") or "")},
    )
    state = State(
        state_id=f"st:ombre:{bucket_id}:v1", lineage_id=f"lin:ombre:{bucket_id}",
        scope="unclassified", subject_ref="unresolved", representation="memory",
        content=text, structured_value=_bucket_value(meta, ingested="created_in_ombre"),
        evidence_refs=(evidence.evidence_id,), confidence=0.5,
        epistemic_status="ingested_from_ombre", status="active", created_at=now,
    )
    kernel.create_state(state, Delta(
        delta_id=f"dl:ombre:{bucket_id}:formation", target_scope=state.scope, before_ref=None,
        after_ref=state.state_id, trigger_evidence_refs=(evidence.evidence_id,),
        derived_by={"processor": "memory_kernel_store.ingest", "reason": "created_in_ombre"},
        confidence=0.5, occurred_at=now, review_status="accepted_from_ombre",
    ), evidence=(evidence,))


def _ingest_edit(kernel: MemoryKernel, head: projection.Head, meta: dict[str, Any], body: str, now: str) -> str:
    bucket_id = head.bucket_id
    version = len(kernel.lineage_history(head.lineage_id)) + 1
    suffix = head.lineage_id[len("lin:"):]
    uid = uuid.uuid4().hex[:8]
    evidence = Evidence(
        evidence_id=f"ev:{suffix}:v{version}:{uid}", source_type="ombre_bucket_edit",
        source_ref=f"ombre:{bucket_id}", observed_at=now, occurred_at=now,
        content=body, integrity=_sha(body), origin_kind="derived",
        provenance={"system": "ombre", "ingested_by": "memory_kernel_store.ingest",
                    "previous_state": head.state.state_id},
    )
    previous = head.state
    state = State(
        state_id=f"st:{suffix}:v{version}", lineage_id=head.lineage_id, scope=previous.scope,
        subject_ref=previous.subject_ref, representation=previous.representation,
        content=projection._clean(body),
        structured_value=_bucket_value(meta, ingested="edited_in_ombre", base=previous.structured_value),
        evidence_refs=(evidence.evidence_id,), confidence=previous.confidence,
        epistemic_status=previous.epistemic_status, status=previous.status, created_at=now,
    )
    kernel.revise_state(state, Delta(
        delta_id=f"dl:{suffix}:v{version}", target_scope=previous.scope, before_ref=previous.state_id,
        after_ref=state.state_id, trigger_evidence_refs=(evidence.evidence_id,),
        derived_by={"processor": "memory_kernel_store.ingest", "reason": "edited_in_ombre"},
        confidence=previous.confidence, occurred_at=now, review_status="accepted_from_ombre",
    ), evidence=(evidence,))
    return state.state_id


def backfill_from_posts(posts_db: Path, kernel_db: Path, runtime: Any = _DEFAULT) -> dict[str, Any]:
    """Bring memories that fell back to posts (Kernel write failed) into the Kernel."""
    if not posts_db.is_file():
        return {"done": [], "errors": []}
    conn = sqlite3.connect(str(posts_db), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_kernel_backfill'"
        ).fetchone():
            return {"done": [], "errors": []}
        pending = conn.execute(
            "SELECT b.post_id, p.* FROM memory_kernel_backfill b LEFT JOIN posts p ON p.id = b.post_id "
            "WHERE b.done_at IS NULL ORDER BY b.post_id"
        ).fetchall()
        done, errors = [], []
        for row in pending:
            post_id = row["post_id"]
            try:
                if row["id"] is None:
                    state_id = None  # the post was deleted before backfill: nothing to keep
                else:
                    result = remember(
                        row["content"], source="legacy_fallback",
                        provenance={"source_ref": f"posts:{post_id}", "post_id": post_id},
                        tags=row["tags"], importance=row["importance"] or None,
                        memory_type=row["type"], layer=row["layer"], pinned=row["pinned"],
                        author=row["author"], occurred_at=_iso_beijing(row["created_at"]),
                        kernel_db=kernel_db, runtime=runtime,
                    )
                    state_id = result["state_id"]
                conn.execute(
                    "UPDATE memory_kernel_backfill SET done_at=?, state_id=? WHERE post_id=?",
                    (_now(), state_id, post_id),
                )
                conn.commit()
                done.append(post_id)
            except Exception as exc:
                errors.append({"post_id": post_id, "error": type(exc).__name__})
        return {"done": done, "errors": errors}
    finally:
        conn.close()


def _iso_beijing(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace(" ", "T", 1))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.isoformat()


def sync(kernel_db: Path | None = None, posts_db: Path | None = None, *, runtime: Any = _DEFAULT) -> dict[str, Any]:
    db = Path(kernel_db or kernel_db_path())
    ombre = _runtime(runtime)
    backfilled = backfill_from_posts(Path(posts_db or posts_db_path()), db, ombre)
    report: dict[str, Any] = {"backfilled": backfilled}
    if ombre is None:
        heads, problems = projection.select_heads(db)
        return {**report, "ombre": "unconfigured", "problems": problems}
    state = projection.ProjectionState(projection.state_path_for(db))
    try:
        report["ingested"] = ingest_from_ombre(db, ombre, state)
    except projection.RuntimeUnavailable as exc:
        heads, problems = projection.select_heads(db)
        return {**report, "ombre": f"unavailable:{exc}"[:300], "problems": problems}
    heads, problems = projection.select_heads(db)
    report["projected"] = projection.project_heads(heads, ombre, state, kernel_db=db)
    report["problems"] = problems
    return report


_SYNC_RUNS = """
CREATE TABLE IF NOT EXISTS projection_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    error TEXT
)
"""
_SYNC_RUNS_KEPT = 2000  # about a week at one run every five minutes


def _sync_outcome(report: dict[str, Any]) -> tuple[str, str | None]:
    """ok / failed (Ombre not reachable or not configured, backfill errors)."""
    ombre = str(report.get("ombre") or "")
    if ombre:
        return "failed", ombre[:300]
    errors = list((report.get("backfilled") or {}).get("errors") or [])
    errors += list((report.get("ingested") or {}).get("errors") or [])
    if errors:
        return "failed", json.dumps(errors[:5], ensure_ascii=False)[:300]
    return "ok", None


def _sync_summary(report: dict[str, Any]) -> dict[str, Any]:
    ingested = report.get("ingested") or {}
    return {
        "backfilled": len((report.get("backfilled") or {}).get("done") or []),
        "ingested_created": len(ingested.get("created") or []),
        "ingested_revised": len(ingested.get("revised") or []),
        "scan": {k: len(ingested.get(k) or []) for k in ("missing", "soft_deleted", "orphaned")},
        "projected": report.get("projected") or {},
        "problems": len(report.get("problems") or []),
    }


def run_sync(kernel_db: Path | None = None, posts_db: Path | None = None, *, runtime: Any = _DEFAULT,
             clock=None) -> dict[str, Any]:
    """The scheduled sync: one at a time, every run recorded in projection_sync_runs.

    Request-time projection and sync already serialise per lineage (ProjectionState.locked);
    this lock only keeps two scheduled runs from overlapping when one is slow.
    """
    import fcntl
    import time

    clock = clock or time.monotonic
    db = Path(kernel_db or kernel_db_path())
    state_db = projection.state_path_for(db)
    projection.ProjectionState(state_db)  # creates the 0600 file and its tables
    lock_path = Path(f"{state_db}.sync.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"outcome": "skipped", "reason": "previous sync still running"}
        started, began = _now(), clock()
        try:
            report = sync(db, posts_db, runtime=runtime)
            outcome, error = _sync_outcome(report)
        except Exception as exc:  # recorded, then re-raised for the timer to see
            report, outcome, error = {}, "failed", f"{type(exc).__name__}:{exc}"[:300]
            _record_sync(state_db, started, clock() - began, outcome, {}, error)
            raise
        summary = _sync_summary(report)
        _record_sync(state_db, started, clock() - began, outcome, summary, error)
        return {"outcome": outcome, "error": error, **summary}
    finally:
        os.close(fd)


def _record_sync(state_db: Path, started: str, seconds: float, outcome: str,
                 summary: dict[str, Any], error: str | None) -> None:
    conn = sqlite3.connect(str(state_db), timeout=10)
    try:
        with conn:
            conn.execute(_SYNC_RUNS)
            conn.execute(
                "INSERT INTO projection_sync_runs (started_at, finished_at, duration_ms, outcome, summary_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (started, _now(), int(seconds * 1000), outcome, json.dumps(summary, ensure_ascii=False), error))
            conn.execute("DELETE FROM projection_sync_runs WHERE id <= (SELECT MAX(id) FROM projection_sync_runs) - ?",
                         (_SYNC_RUNS_KEPT,))
    finally:
        conn.close()


def sync_health(kernel_db: Path | None = None) -> dict[str, Any]:
    """Last run, last success and the current run of consecutive failures."""
    state_db = projection.state_path_for(Path(kernel_db or kernel_db_path()))
    if not state_db.is_file():
        return {"runs": 0}
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projection_sync_runs'").fetchone():
            return {"runs": 0}
        last = conn.execute("SELECT * FROM projection_sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        ok = conn.execute("SELECT * FROM projection_sync_runs WHERE outcome='ok' ORDER BY id DESC LIMIT 1").fetchone()
        failures = conn.execute(
            "SELECT COUNT(*) FROM projection_sync_runs WHERE outcome != 'ok' AND id > COALESCE("
            "(SELECT MAX(id) FROM projection_sync_runs WHERE outcome='ok'), 0)").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM projection_sync_runs").fetchone()[0]
    finally:
        conn.close()

    def brief(row):
        return None if row is None else {
            "started_at": row["started_at"], "finished_at": row["finished_at"], "duration_ms": row["duration_ms"],
            "outcome": row["outcome"], "summary": json.loads(row["summary_json"]), "error": row["error"]}
    return {"runs": runs, "last_run": brief(last), "last_success": brief(ok), "consecutive_failures": failures}


def status(kernel_db: Path | None = None) -> dict[str, Any]:
    """Projection bookkeeping only; never calls Ombre."""
    db = Path(kernel_db or kernel_db_path())
    heads, problems = projection.select_heads(db)
    rows = {r["lineage_id"]: r for r in projection.ProjectionState(projection.state_path_for(db)).rows()}
    counts: dict[str, int] = {}
    for head in heads:
        row = rows.get(head.lineage_id)
        if row is None:
            key = "untracked"
        elif row["status"] == "projected" and row["projected_state_id"] != head.state.state_id:
            key = "pending"
        else:
            key = row["status"]
        counts[key] = counts.get(key, 0) + 1
    return {"heads": len(heads), "projection": counts, "problems": problems, "sync": sync_health(db)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("sync", "status"))
    args = parser.parse_args(argv)
    result = run_sync() if args.command == "sync" else status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A failed scheduled sync must show as a failed unit, not a quiet success.
    return 1 if result.get("outcome") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
