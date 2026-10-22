"""B-1: one-shot import of legacy memories into a fresh Memory Kernel database.

Sources are read-only: the Ombre vault (Markdown buckets) and the legacy
``posts`` rows of type MEMORY / FACT / MISS. Every source record becomes one
derived Evidence that keeps its raw text verbatim; near-duplicates across both
sources are grouped into a single State whose evidence_refs cite every member.

Ombre lifecycle fields (activation_count, last_active, decay, archive folder,
dont_surface) are deliberately not imported: they belong to the retrieval
plugin, not to the Kernel. Nothing here touches chat, Ombre or posts.

Usage:
    python -m tools.memory_kernel_import --vault DIR --posts-db FILE \
        --kernel-db NEW_FILE --report FILE [--dry-run]
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from tools.memory_kernel import Delta, Evidence, MemoryKernel, State

IMPORTER = "memory_kernel_import"
IMPORTER_VERSION = 1
LEGACY_POST_TYPES = ("MEMORY", "FACT", "MISS")
# posts.created_at defaults to Beijing local time; old Ombre wrote naive UTC.
POSTS_TZ = timezone(timedelta(hours=8))
OMBRE_NAIVE_TZ = timezone.utc
DUPLICATE_RATIO = 0.88
# Ombre's handover bucket is runtime state, not a memory.
SYSTEM_DOMAINS = {"呼吸间"}
# Short deployment/acceptance probes written into posts during rollouts.
PROBE_PATTERN = re.compile(r"\bprobe\b|\bsmoke\b|\bacceptance\b|\bE2E\b|\bcutover\b|探针", re.I)
PROBE_MAX_CHARS = 200
_OMBRE_TYPE_RANK = {"permanent": 0, "plan": 1, "dynamic": 2, "archived": 3}


@dataclass
class SourceItem:
    key: str
    system: str
    raw: str
    text: str
    created_at: str
    meta: dict[str, Any] = field(default_factory=dict)
    norm: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.norm = _normalize(self.text)


def _clean(text: str) -> str:
    """Drop Ombre's auto-inserted [[wikilink]] brackets; keep every character."""
    return re.sub(r"\[\[|\]\]", "", text or "").strip()


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if ch.isalnum())


def _iso(value: Any, naive_tz: timezone) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=naive_tz)
    return parsed.isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and value != value:
        return None
    return value


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    body = text[end + 4:]
    return (meta if isinstance(meta, dict) else {}), body.lstrip("\n")


def load_ombre_buckets(vault: Path) -> tuple[list[SourceItem], list[dict[str, Any]]]:
    items, _deleted, skipped = _scan_ombre(vault)
    return items, skipped


def _scan_ombre(vault: Path) -> tuple[list[SourceItem], list[SourceItem], list[dict[str, Any]]]:
    """Return live buckets, owner-deleted buckets (for matching only), skips."""
    items, deleted, skipped = [], [], []
    for path in sorted(vault.rglob("*.md")):
        rel = path.relative_to(vault).as_posix()
        if any(part.startswith(("_", ".")) for part in path.relative_to(vault).parts):
            continue
        meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        bucket_id = str(meta.get("id") or "").strip()
        domains = [str(d) for d in (meta.get("domain") or [])]
        reason = None
        if not bucket_id:
            reason = "no_bucket_id"
        elif meta.get("deleted_at"):
            reason = "ombre_deleted"
        elif SYSTEM_DOMAINS & set(domains):
            reason = "ombre_system_bucket"
        elif not _clean(body):
            reason = "empty_body"
        if reason:
            skipped.append({"key": f"ombre:{bucket_id or rel}", "path": rel, "reason": reason})
            if reason == "ombre_deleted":
                deleted.append(SourceItem(f"ombre:{bucket_id}", "ombre", body, _clean(body), ""))
            continue
        items.append(SourceItem(
            key=f"ombre:{bucket_id}",
            system="ombre",
            raw=body,
            text=_clean(body),
            created_at=_iso(meta.get("created") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc), OMBRE_NAIVE_TZ),
            meta={
                "path": rel,
                "title": str(meta.get("name") or ""),
                "ombre_type": str(meta.get("type") or ""),
                "domain": domains,
                "tags": [str(t) for t in (meta.get("tags") or [])],
                "importance": meta.get("importance"),
                "valence": meta.get("valence"),
                "arousal": meta.get("arousal"),
                "pinned": bool(meta.get("pinned")),
                "resolved": bool(meta.get("resolved")),
            },
        ))
    return items, deleted, skipped


def drop_cleaned_posts(posts: list[SourceItem], deleted: list[SourceItem], live: list[SourceItem]):
    """Carry the owner's Ombre cleanup over to posts copies, and drop probes.

    A post that also matches a live bucket is kept: only the duplicate copies
    were cleaned, and the post is still evidence for the surviving memory.
    """
    kept, skipped = [], []
    for item in posts:
        match = next((d.key for d in deleted if _similar(item, d)), None)
        if match and any(_similar(item, b) for b in live):
            match = None
        if match:
            skipped.append({"key": item.key, "reason": "matches_ombre_deleted", "match": match})
        elif len(item.text) < PROBE_MAX_CHARS and PROBE_PATTERN.search(item.text):
            skipped.append({"key": item.key, "reason": "deployment_probe"})
        else:
            kept.append(item)
    return kept, skipped


def load_legacy_posts(db_path: Path) -> tuple[list[SourceItem], list[dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in LEGACY_POST_TYPES)
        rows = conn.execute(
            f"SELECT * FROM posts WHERE type IN ({placeholders}) ORDER BY id",
            LEGACY_POST_TYPES,
        ).fetchall()
    finally:
        conn.close()
    items, skipped = [], []
    for row in rows:
        content = row["content"] or ""
        if not content.strip():
            skipped.append({"key": f"posts:{row['id']}", "reason": "empty_body"})
            continue
        keys = row.keys()
        items.append(SourceItem(
            key=f"posts:{row['id']}",
            system="posts",
            raw=content,
            text=_clean(content),
            created_at=_iso(row["created_at"], POSTS_TZ),
            meta={
                "post_type": row["type"],
                "author": row["author"],
                "layer": row["layer"] if "layer" in keys else None,
                "tags": row["tags"] if "tags" in keys else None,
                "importance": row["importance"] if "importance" in keys else None,
                "pinned": bool(row["pinned"]) if "pinned" in keys else False,
                "resolved": bool(row["resolved"]) if "resolved" in keys else False,
                "title": (row["summary_title"] or "") if "summary_title" in keys else "",
            },
        ))
    return items, skipped


def _similar(a: SourceItem, b: SourceItem) -> bool:
    x, y = a.norm, b.norm
    if not x or not y:
        return False
    if x == y:
        return True
    shorter, longer = sorted((len(x), len(y)))
    if shorter / longer < 0.7:
        return False
    matcher = difflib.SequenceMatcher(None, x, y, autojunk=False)
    return (
        matcher.real_quick_ratio() >= DUPLICATE_RATIO
        and matcher.quick_ratio() >= DUPLICATE_RATIO
        and matcher.ratio() >= DUPLICATE_RATIO
    )


def group_duplicates(items: list[SourceItem]) -> list[list[SourceItem]]:
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if find(i) != find(j) and _similar(items[i], items[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[SourceItem]] = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)
    return sorted(groups.values(), key=lambda g: min(m.created_at for m in g))


def _canonical(group: list[SourceItem]) -> SourceItem:
    """Prefer the Ombre copy (newest curated text), then the most complete one."""
    def rank(item: SourceItem):
        ombre_rank = _OMBRE_TYPE_RANK.get(item.meta.get("ombre_type", ""), 9)
        return (item.system != "ombre", ombre_rank, -len(item.text), item.created_at)
    return sorted(group, key=rank)[0]


_LAYER_RANK = {"core": 0, "long-term": 1, "recent": 2}


def group_memory(group: list[SourceItem], canonical: SourceItem) -> dict[str, Any]:
    """Legacy write semantics of a merged memory: the strongest member wins."""
    posts = [m for m in group if m.system == "posts"]
    types = {m.meta.get("post_type") for m in posts}
    layers = [m.meta.get("layer") or "recent" for m in posts]
    if any(m.meta.get("ombre_type") == "permanent" for m in group):
        layers.append("core")
    return {
        "type": "FACT" if "FACT" in types else ("MISS" if types == {"MISS"} else "MEMORY"),
        "layer": min(layers, key=lambda layer: _LAYER_RANK.get(layer, 3)) if layers else "recent",
        "pinned": any(m.meta.get("pinned") for m in group),
        "author": str(canonical.meta.get("author") or (posts[0].meta.get("author") if posts else "") or ""),
    }


def build_records(groups: list[list[SourceItem]], now: str):
    evidences: list[Evidence] = []
    transitions: list[tuple[State, Delta]] = []
    for group in groups:
        for item in group:
            evidences.append(Evidence(
                evidence_id=f"ev:{item.key}",
                source_type="ombre_bucket" if item.system == "ombre" else "legacy_post",
                source_ref=item.key,
                observed_at=now,
                occurred_at=item.created_at,
                content=item.raw,
                integrity=_sha256(item.raw),
                provenance=_jsonable({
                    "system": "ombre vault" if item.system == "ombre" else "memories.db posts",
                    "imported_by": IMPORTER,
                    "importer_version": IMPORTER_VERSION,
                    **{k: v for k, v in item.meta.items() if k in ("path", "post_type", "author", "layer")},
                }),
                origin_kind="derived",
            ))
        canonical = _canonical(group)
        lineage = f"lin:{canonical.key}"
        state_id = f"st:{canonical.key}:v1"
        scope = "unclassified"
        refs = tuple(f"ev:{m.key}" for m in group)
        is_fact = any(m.meta.get("post_type") == "FACT" for m in group)
        meta = canonical.meta
        state = State(
            state_id=state_id,
            lineage_id=lineage,
            scope=scope,
            subject_ref="legacy:unresolved",
            representation="fact" if is_fact else "memory",
            content=canonical.text,
            structured_value=_jsonable({
                "title": meta.get("title") or "",
                "domain": meta.get("domain") or [],
                "tags": meta.get("tags") or [],
                "importance": meta.get("importance"),
                "valence": meta.get("valence"),
                "arousal": meta.get("arousal"),
                "memory": group_memory(group, canonical),
                "legacy": {
                    "canonical": canonical.key,
                    "members": [m.key for m in group],
                    "ombre_type": meta.get("ombre_type"),
                    "pinned": any(m.meta.get("pinned") for m in group),
                },
            }),
            evidence_refs=refs,
            confidence=0.5,
            epistemic_status="legacy_unreviewed",
            status="resolved" if all(m.meta.get("resolved") for m in group) else "active",
            created_at=min(m.created_at for m in group),
        )
        delta = Delta(
            delta_id=f"dl:{canonical.key}:formation",
            target_scope=scope,
            before_ref=None,
            after_ref=state_id,
            trigger_evidence_refs=refs,
            derived_by={"processor": IMPORTER, "version": IMPORTER_VERSION,
                        "dedupe": f"normalized SequenceMatcher >= {DUPLICATE_RATIO}"},
            confidence=0.5,
            occurred_at=now,
            review_status="imported_unreviewed",
        )
        transitions.append((state, delta))
    return evidences, transitions


def _snippet(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def build_report(items, groups, skipped) -> dict[str, Any]:
    merged = [g for g in groups if len(g) > 1]
    return {
        "importer": IMPORTER,
        "version": IMPORTER_VERSION,
        "sources": {
            "ombre": sum(1 for i in items if i.system == "ombre"),
            "posts": sum(1 for i in items if i.system == "posts"),
        },
        "states": len(groups),
        "evidence": len(items),
        "duplicate_groups": len(merged),
        "cross_source_groups": sum(1 for g in merged if len({m.system for m in g}) > 1),
        "skipped": skipped,
        "groups": [
            {
                "canonical": _canonical(g).key,
                "members": [{"key": m.key, "created_at": m.created_at, "text": _snippet(m.text)} for m in g],
            }
            for g in merged
        ],
    }


def run(vault: Path, posts_db: Path, kernel_db: Path | None, *, now: str | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).isoformat()
    ombre_items, ombre_deleted, ombre_skipped = _scan_ombre(vault)
    post_items, post_skipped = load_legacy_posts(posts_db)
    post_items, cleaned = drop_cleaned_posts(post_items, ombre_deleted, ombre_items)
    items = ombre_items + post_items
    groups = group_duplicates(items)
    evidences, transitions = build_records(groups, now)
    report = build_report(items, groups, ombre_skipped + post_skipped + cleaned)
    if kernel_db is not None:
        if kernel_db.exists() and kernel_db.stat().st_size > 0:
            raise FileExistsError(f"refusing to import into an existing database: {kernel_db}")
        kernel = MemoryKernel(kernel_db)
        kernel.initialize()
        by_id = {evidence.evidence_id: evidence for evidence in evidences}
        for state, delta in transitions:
            # One transaction per memory: its Evidence, State and formation Delta.
            kernel.create_state(state, delta, evidence=tuple(by_id[ref] for ref in state.evidence_refs))
        report["kernel_db"] = str(kernel_db)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--posts-db", required=True, type=Path)
    parser.add_argument("--kernel-db", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.dry_run and args.kernel_db is None:
        parser.error("--kernel-db is required unless --dry-run")
    report = run(args.vault, args.posts_db, None if args.dry_run else args.kernel_db)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("sources", "states", "evidence", "duplicate_groups", "cross_source_groups")}, ensure_ascii=False))
    print(f"skipped: {len(report['skipped'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
