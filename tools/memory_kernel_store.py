"""B-3: Kernel-first memory writes for the live runtime, and Ombre catch-up.

remember() commits Evidence + State + formation Delta to the Kernel first and
then projects that lineage into the Ombre vault. A failed projection never
loses the memory: the Kernel row is already committed and the next sync()
retries it.

sync() keeps the two sides converged without ever letting Ombre be the
authority: buckets Ombre created on its own (letters, plans, anything a tool
wrote directly) become new lineages, and buckets edited inside Ombre (trace,
the dashboard) become Kernel revisions with a Delta, before the projection
runs. Lifecycle fields stay Ombre's.

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
import sys
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools import memory_kernel_projection as projection
from tools.memory_kernel import Delta, Evidence, MemoryKernel, State
from tools.memory_kernel_switch import FLAG_KEY, write_enabled  # noqa: F401 (re-exported)

DEFAULT_KERNEL_DB = "/var/lib/hayagarden/memory-kernel.db"
DEFAULT_OMBRE_VAULT = "/var/lib/hayagarden/ombre-shadow-3.6.14-r1/vault"


def kernel_db_path() -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_DB") or DEFAULT_KERNEL_DB)


def vault_path() -> Path:
    return Path(os.environ.get("MEMORY_KERNEL_OMBRE_VAULT") or DEFAULT_OMBRE_VAULT)


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


def _project(heads: list[projection.Head], vault: Path) -> dict[str, int]:
    planned = projection.plan(heads, vault, existing=projection.find_buckets(vault, [h.bucket_id for h in heads]))
    return projection.apply(planned, vault)


MEMORY_TYPES = ("MEMORY", "FACT", "MISS")


def memory_semantics(memory_type: str, layer: str, pinned: Any, author: str | None) -> dict[str, Any]:
    """The legacy write semantics a memory must keep (see projection bucket placement)."""
    return {"type": memory_type, "layer": layer or "recent", "pinned": bool(pinned), "author": author or ""}


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
    kernel_db: str | os.PathLike[str] | None = None,
    vault: str | os.PathLike[str] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        return {"status": "INVALID_CONTENT"}
    if memory_type not in MEMORY_TYPES:
        raise ValueError(f"not a memory type: {memory_type}")
    text = content.strip()
    db = Path(kernel_db or kernel_db_path())
    target = Path(vault or vault_path())
    now = now or _now()

    wanted = _norm(text)
    for lineage_id, state_id, existing in projection.head_contents(db):
        if _norm(existing) == wanted:
            return {"status": "DEDUPLICATED", "state_id": state_id, "lineage_id": lineage_id,
                    "bucket_id": projection.bucket_id_for(lineage_id), "content_sha256": _sha(text)}

    uid = uuid.uuid4().hex[:16]
    lineage = f"lin:k:{uid}"
    evidence = Evidence(
        evidence_id=f"ev:k:{uid}",
        source_type=f"memory_write:{source}",
        source_ref=str((provenance or {}).get("turn_id") or (provenance or {}).get("source_ref") or f"{source}:{uid}"),
        observed_at=now,
        occurred_at=now,
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
        created_at=now,
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
    try:
        _project([head], target)
        ombre_status = "PROJECTED"
    except Exception as exc:  # the Kernel row is committed; sync() retries
        ombre_status = f"PROJECTION_FAILED:{type(exc).__name__}"
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


def ingest_from_ombre(kernel_db: Path, vault: Path, *, now: str | None = None) -> dict[str, Any]:
    """Pull Ombre-side creations and edits into the Kernel; never the reverse."""
    now = now or _now()
    kernel = MemoryKernel(kernel_db)
    heads, _ = projection.select_heads(kernel_db)
    existing = projection.index_vault(vault)
    planned = projection.plan(heads, vault)
    by_bucket = {h.bucket_id: h for h in heads}
    created, revised, errors = [], [], []

    for bucket_id in planned["unmanaged"]:
        _, meta, body = existing[bucket_id]
        text = projection._clean(body)
        if not text:
            continue
        try:
            _ingest_created(kernel, bucket_id, meta, body, text, now)
            created.append(bucket_id)
        except Exception as exc:
            errors.append({"bucket_id": bucket_id, "stage": "created", "error": type(exc).__name__})

    for action in planned["actions"]:
        if action["op"] != "diverged":
            continue
        _, meta, body = existing[action["bucket_id"]]
        try:
            _ingest_edit(kernel, by_bucket[action["bucket_id"]], meta, body, now)
            revised.append(action["bucket_id"])
        except Exception as exc:
            errors.append({"bucket_id": action["bucket_id"], "stage": "edited", "error": type(exc).__name__})
    return {"created": created, "revised": revised, "errors": errors}


def _ingest_created(kernel: MemoryKernel, bucket_id: str, meta: dict[str, Any], body: str, text: str, now: str) -> None:
    uid = uuid.uuid4().hex[:8]
    evidence = Evidence(
        evidence_id=f"ev:ombre:{bucket_id}:{uid}", source_type="ombre_bucket",
        source_ref=f"ombre:{bucket_id}", observed_at=now, occurred_at=_iso(meta.get("created"), now),
        content=body, integrity=_sha(body), origin_kind="derived",
        provenance={"system": "ombre vault", "ingested_by": "memory_kernel_store.ingest",
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


def _ingest_edit(kernel: MemoryKernel, head: projection.Head, meta: dict[str, Any], body: str, now: str) -> None:
    bucket_id = head.bucket_id
    version = len(kernel.lineage_history(head.lineage_id)) + 1
    suffix = head.lineage_id[len("lin:"):]
    uid = uuid.uuid4().hex[:8]
    evidence = Evidence(
        evidence_id=f"ev:{suffix}:v{version}:{uid}", source_type="ombre_bucket_edit",
        source_ref=f"ombre:{bucket_id}", observed_at=now, occurred_at=now,
        content=body, integrity=_sha(body), origin_kind="derived",
        provenance={"system": "ombre vault", "ingested_by": "memory_kernel_store.ingest",
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


def sync(kernel_db: Path | None = None, vault: Path | None = None) -> dict[str, Any]:
    db = Path(kernel_db or kernel_db_path())
    target = Path(vault or vault_path())
    ingested = ingest_from_ombre(db, target)
    heads, problems = projection.select_heads(db)
    applied = projection.apply(projection.plan(heads, target), target)
    return {"ingested": ingested, "applied": applied, "problems": problems}


def status(kernel_db: Path | None = None, vault: Path | None = None) -> dict[str, Any]:
    db = Path(kernel_db or kernel_db_path())
    target = Path(vault or vault_path())
    heads, problems = projection.select_heads(db)
    planned = projection.plan(heads, target)
    counts: dict[str, int] = {}
    for action in planned["actions"]:
        counts[action["op"]] = counts.get(action["op"], 0) + 1
    return {"heads": len(heads), "plan": counts, "unmanaged": len(planned["unmanaged"]),
            "problems": problems}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("sync", "status"))
    args = parser.parse_args(argv)
    result = sync() if args.command == "sync" else status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
