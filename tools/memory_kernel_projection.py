"""B-2: one-way projection from the Memory Kernel into an Ombre vault.

The Kernel is the authority; the vault is a rebuildable projection that Ombre
indexes, decays and surfaces. Each lineage maps to exactly one bucket. The
projection writes semantic fields (body, title, domain, tags, importance,
valence/arousal, resolved) and never touches lifecycle fields that Ombre owns
(activation_count, last_active, weight, change_log, dont_surface, deleted_at,
folder placement of an existing bucket).

Head selection is a policy outside the Kernel: a lineage's head is the State
with no outgoing Delta. A lineage with several heads is reported, not guessed.

Offline only for B-2: run it against a vault no Ombre process is serving.

Usage:
    python -m tools.memory_kernel_projection --kernel-db FILE --vault DIR plan|apply|verify
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.memory_kernel import MemoryKernel, State

PROJECTOR = "memory_kernel_projection"
PROJECTOR_VERSION = 1
# Fields this projection owns; everything else in a bucket belongs to Ombre.
SEMANTIC_FIELDS = ("name", "domain", "tags", "importance", "valence", "arousal", "resolved")
KERNEL_FIELDS = ("kernel_lineage_id", "kernel_state_id", "kernel_content_sha256")
_FOLDERS = {"dynamic": "dynamic", "permanent": "permanent", "archived": "archive", "plan": "plans/active"}
# Ombre's handover bucket is its own runtime state, never Kernel-managed.
SYSTEM_DOMAINS = {"呼吸间"}
_UNSAFE = re.compile(r'[\\/:*?"<>|\n\r\t]')


@dataclass(frozen=True)
class Head:
    lineage_id: str
    state: State

    @property
    def bucket_id(self) -> str:
        # Imported Ombre lineages keep their bucket; everything else gets a stable id.
        if self.lineage_id.startswith("lin:ombre:"):
            return self.lineage_id[len("lin:ombre:"):]
        return hashlib.sha256(self.lineage_id.encode("utf-8")).hexdigest()[:12]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return re.sub(r"\[\[|\]\]", "", text or "").strip()


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


def bucket_semantics(head: Head) -> dict[str, Any]:
    state = head.state
    value = state.structured_value if isinstance(state.structured_value, dict) else {}
    title = str(value.get("title") or "").strip() or _clean(state.content)[:20]
    return {
        "name": title,
        "domain": _as_list(value.get("domain")) or ["未分类"],
        "tags": _as_list(value.get("tags")),
        "importance": _importance(value.get("importance")),
        "valence": value.get("valence") if value.get("valence") is not None else 0.5,
        "arousal": value.get("arousal") if value.get("arousal") is not None else 0.3,
        "resolved": state.status == "resolved",
    }


def _new_bucket_type(head: Head) -> str:
    value = head.state.structured_value if isinstance(head.state.structured_value, dict) else {}
    legacy = value.get("legacy") or {}
    if legacy.get("ombre_type") in _FOLDERS:
        return legacy["ombre_type"]
    return "permanent" if head.state.representation == "fact" else "dynamic"


def _split(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = yaml.safe_load(text[3:end]) or {}
    return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")


def _render(meta: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=True)
    return f"---\n{front}---\n\n{body}"


def index_vault(vault: Path) -> dict[str, tuple[Path, dict[str, Any], str]]:
    found = {}
    if not vault.exists():
        return found
    for path in sorted(vault.rglob("*.md")):
        if any(part.startswith(("_", ".")) for part in path.relative_to(vault).parts):
            continue
        meta, body = _split(path.read_text(encoding="utf-8"))
        bucket_id = str(meta.get("id") or "").strip()
        if bucket_id:
            found[bucket_id] = (path, meta, body)
    return found


def _live_unmanaged(existing, managed: set[str]) -> list[str]:
    return sorted(
        b for b, (_, meta, _) in existing.items()
        if b not in managed and not meta.get("deleted_at") and not meta.get("tombstone")
        and not SYSTEM_DOMAINS & set(_as_list(meta.get("domain")))
    )


def plan(heads: list[Head], vault: Path) -> dict[str, Any]:
    existing = index_vault(vault)
    actions = []
    for head in heads:
        semantics = bucket_semantics(head)
        body = head.state.content
        kernel_meta = {
            "kernel_lineage_id": head.lineage_id,
            "kernel_state_id": head.state.state_id,
            "kernel_content_sha256": _sha(body),
        }
        current = existing.get(head.bucket_id)
        if current is None:
            kind = _new_bucket_type(head)
            actions.append({"op": "create", "bucket_id": head.bucket_id, "type": kind,
                            "semantics": semantics, "kernel": kernel_meta, "body": body,
                            "created": head.state.created_at})
            continue
        path, meta, old_body = current
        if meta.get("deleted_at") or meta.get("tombstone"):
            actions.append({"op": "conflict", "bucket_id": head.bucket_id, "reason": "bucket_deleted_in_ombre"})
            continue
        changed = [k for k in SEMANTIC_FIELDS if _norm_field(k, meta.get(k)) != _norm_field(k, semantics[k])]
        if _clean(old_body) != _clean(body):
            changed.append("body")
        stale_kernel = any(meta.get(k) != v for k, v in kernel_meta.items())
        if changed or stale_kernel:
            actions.append({"op": "update", "bucket_id": head.bucket_id, "path": str(path),
                            "changed": changed, "semantics": semantics, "kernel": kernel_meta, "body": body})
        else:
            actions.append({"op": "unchanged", "bucket_id": head.bucket_id})
    return {"actions": actions, "unmanaged": _live_unmanaged(existing, {h.bucket_id for h in heads})}


def _norm_field(name: str, value: Any) -> Any:
    if name in ("domain", "tags"):
        return sorted(str(v) for v in (value or []))
    if name == "resolved":
        return bool(value)
    if name in ("importance",):
        return int(value) if value is not None else None
    if name in ("valence", "arousal"):
        return round(float(value), 6) if value is not None else None
    return value


def _new_path(vault: Path, action: dict[str, Any]) -> Path:
    folder = _FOLDERS[action["type"]]
    domain = _UNSAFE.sub("_", str(action["semantics"]["domain"][0]))[:40] or "未分类"
    name = _UNSAFE.sub("_", str(action["semantics"]["name"]))[:40].strip() or action["bucket_id"]
    return vault / folder / domain / f"{name}_{action['bucket_id']}.md"


def apply(actions: dict[str, Any], vault: Path) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "unchanged": 0, "conflict": 0}
    for action in actions["actions"]:
        counts[action["op"]] += 1
        if action["op"] == "create":
            meta = {
                "id": action["bucket_id"],
                "type": action["type"],
                "created": action["created"],
                "last_active": action["created"],
                "activation_count": 0,
                "source_tool": PROJECTOR,
                **{k: v for k, v in action["semantics"].items() if k != "resolved" or v},
                **action["kernel"],
            }
            path = _new_path(vault, action)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_render(meta, action["body"]), encoding="utf-8")
        elif action["op"] == "update":
            path = Path(action["path"])
            meta, old_body = _split(path.read_text(encoding="utf-8"))
            # Keep Ombre's own rendering (e.g. [[links]]) unless the text really changed.
            body = action["body"] if "body" in action["changed"] else old_body
            for key, value in action["semantics"].items():
                if key == "resolved" and not value:
                    meta.pop("resolved", None)
                else:
                    meta[key] = value
            meta.update(action["kernel"])
            path.write_text(_render(meta, body), encoding="utf-8")
    return counts


def verify(heads: list[Head], reference: Path) -> dict[str, Any]:
    """Would a vault rebuilt from the Kernel carry the same memories as reference?"""
    existing = index_vault(reference)
    mismatches, missing = [], []
    for head in heads:
        current = existing.get(head.bucket_id)
        if current is None:
            missing.append(head.bucket_id)
            continue
        _, meta, body = current
        semantics = bucket_semantics(head)
        diff = [k for k in SEMANTIC_FIELDS if _norm_field(k, meta.get(k)) != _norm_field(k, semantics[k])]
        if _clean(body) != _clean(head.state.content):
            diff.append("body")
        if diff:
            mismatches.append({"bucket_id": head.bucket_id, "fields": diff})
    return {
        "heads": len(heads),
        "matched": len(heads) - len(missing) - len(mismatches),
        "missing_in_reference": missing,
        "mismatches": mismatches,
        "reference_unmanaged": _live_unmanaged(existing, {h.bucket_id for h in heads}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--kernel-db", required=True, type=Path)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("mode", choices=("plan", "apply", "verify"))
    args = parser.parse_args(argv)
    heads, problems = select_heads(args.kernel_db)
    if args.mode == "verify":
        report: dict[str, Any] = verify(heads, args.vault)
    else:
        planned = plan(heads, args.vault)
        report = {"counts": {op: sum(1 for a in planned["actions"] if a["op"] == op)
                             for op in ("create", "update", "unchanged", "conflict")},
                  "unmanaged": planned["unmanaged"],
                  "conflicts": [a for a in planned["actions"] if a["op"] == "conflict"]}
        if args.mode == "apply":
            report["applied"] = apply(planned, args.vault)
    report["problems"] = problems
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    summary = {k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
