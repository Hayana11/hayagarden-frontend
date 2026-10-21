"""B-2: one-way projection from the Memory Kernel into an Ombre vault.

The Kernel is the authority; the vault is a rebuildable projection that Ombre
indexes, decays and surfaces. Each lineage maps to exactly one bucket. The
projection writes semantic fields (body, title, domain, tags, importance,
valence/arousal) and never touches lifecycle fields that Ombre owns
(resolved — the decay engine auto-resolves —, activation_count, last_active,
weight, change_log, dont_surface, deleted_at, folder of an existing bucket).

Head selection is a policy outside the Kernel: a lineage's head is the State
with no outgoing Delta. A lineage with several heads is reported, not guessed.

Every projected bucket carries kernel_semantics_sha256. When a bucket's
semantics no longer match that stamp it was edited inside Ombre (trace, the
dashboard): the projection reports it as ``diverged`` and leaves it for
memory_kernel_store to ingest as a Kernel revision, never overwriting it.

Writes are safe against a live Ombre: each bucket is rewritten under Ombre's
own per-bucket file lease and replaced atomically; Ombre's external-change
poller then refreshes its indexes.

Usage:
    python -m tools.memory_kernel_projection --kernel-db FILE --vault DIR plan|apply|verify
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tools.memory_kernel import MemoryKernel, State

PROJECTOR = "memory_kernel_projection"
PROJECTOR_VERSION = 1
# Fields this projection owns; everything else in a bucket belongs to Ombre.
SEMANTIC_FIELDS = ("name", "domain", "tags", "importance", "valence", "arousal")
KERNEL_FIELDS = ("kernel_lineage_id", "kernel_state_id", "kernel_content_sha256", "kernel_semantics_sha256")
LEASE_TIMEOUT_SECONDS = 10.0
_FOLDERS = {"dynamic": "dynamic", "permanent": "permanent", "archived": "archive", "plan": "plans/active"}
# Ombre's handover bucket is its own runtime state, never Kernel-managed.
SYSTEM_DOMAINS = {"呼吸间"}
_UNSAFE = re.compile(r'[\\/:*?"<>|\n\r\t]')
# The pure-Python parser costs ~25 ms per bucket; libyaml makes a full scan cheap.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


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


def bucket_id_for(lineage_id: str) -> str:
    return Head(lineage_id, None).bucket_id  # type: ignore[arg-type]


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
    }


def semantics_sha(body: str, semantics: dict[str, Any]) -> str:
    """Fingerprint of everything the projection owns in one bucket."""
    normalized = {k: _norm_field(k, semantics.get(k)) for k in SEMANTIC_FIELDS}
    payload = json.dumps([_clean(body), normalized], ensure_ascii=False, sort_keys=True)
    return _sha(payload)


def bucket_current_semantics(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: meta.get(k) for k in SEMANTIC_FIELDS}


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
    meta = yaml.load(text[3:end], Loader=_YAML_LOADER) or {}
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


def is_system_bucket(meta: dict[str, Any]) -> bool:
    return bool(SYSTEM_DOMAINS & set(_as_list(meta.get("domain"))))


def _live_unmanaged(existing, managed: set[str]) -> list[str]:
    return sorted(
        b for b, (_, meta, _) in existing.items()
        if b not in managed and not meta.get("deleted_at") and not meta.get("tombstone")
        and not is_system_bucket(meta)
    )


def find_buckets(vault: Path, bucket_ids: list[str]) -> dict[str, tuple[Path, dict[str, Any], str]]:
    """Locate a few buckets by Ombre's ``<name>_<id>.md`` naming without parsing the vault."""
    found = {}
    for bucket_id in bucket_ids:
        for path in vault.rglob(f"*_{bucket_id}.md") if vault.exists() else ():
            if any(part.startswith(("_", ".")) for part in path.relative_to(vault).parts):
                continue
            meta, body = _split(path.read_text(encoding="utf-8"))
            if str(meta.get("id") or "").strip() == bucket_id:
                found[bucket_id] = (path, meta, body)
                break
    return found


def plan(heads: list[Head], vault: Path, existing=None) -> dict[str, Any]:
    """Plan against the whole vault, or against a pre-supplied partial index."""
    partial = existing is not None
    existing = existing if partial else index_vault(vault)
    actions = []
    for head in heads:
        semantics = bucket_semantics(head)
        body = head.state.content
        head_sha = semantics_sha(body, semantics)
        kernel_meta = {
            "kernel_lineage_id": head.lineage_id,
            "kernel_state_id": head.state.state_id,
            "kernel_content_sha256": _sha(body),
            "kernel_semantics_sha256": head_sha,
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
        current_sha = semantics_sha(old_body, bucket_current_semantics(meta))
        stamped = meta.get("kernel_semantics_sha256")
        if current_sha != head_sha and stamped and current_sha != stamped:
            # Edited inside Ombre after the last projection: ingest, don't overwrite.
            actions.append({"op": "diverged", "bucket_id": head.bucket_id, "path": str(path),
                            "lineage_id": head.lineage_id, "state_id": head.state.state_id})
            continue
        changed = [k for k in SEMANTIC_FIELDS if _norm_field(k, meta.get(k)) != _norm_field(k, semantics[k])]
        if _clean(old_body) != _clean(body):
            changed.append("body")
        stale_kernel = any(meta.get(k) != v for k, v in kernel_meta.items())
        if changed or stale_kernel:
            actions.append({"op": "update", "bucket_id": head.bucket_id, "path": str(path),
                            "changed": changed, "semantics": semantics, "kernel": kernel_meta, "body": body,
                            "expected_sha": current_sha})
        else:
            actions.append({"op": "unchanged", "bucket_id": head.bucket_id})
    unmanaged = [] if partial else _live_unmanaged(existing, {h.bucket_id for h in heads})
    return {"actions": actions, "unmanaged": unmanaged}


def _norm_field(name: str, value: Any) -> Any:
    if name in ("domain", "tags"):
        return sorted(str(v) for v in (value or []))
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


@contextlib.contextmanager
def bucket_lease(vault: Path, bucket_id: str, timeout: float = LEASE_TIMEOUT_SECONDS):
    """Take Ombre's own per-bucket OS lease (see bucket_manager._filesystem_turn)."""
    lock_dir = vault / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"bucket-{bucket_id}".encode("utf-8")).hexdigest()
    fd = os.open(lock_dir / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"bucket lease busy: {bucket_id}")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".kernel-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def apply(actions: dict[str, Any], vault: Path) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "unchanged": 0, "conflict": 0, "diverged": 0, "raced": 0}
    for action in actions["actions"]:
        if action["op"] == "create":
            with bucket_lease(vault, action["bucket_id"]):
                if find_buckets(vault, [action["bucket_id"]]):
                    counts["raced"] += 1
                    continue
                meta = {
                    "id": action["bucket_id"],
                    "type": action["type"],
                    "created": action["created"],
                    "last_active": action["created"],
                    "activation_count": 0,
                    "source_tool": PROJECTOR,
                    **action["semantics"],
                    **action["kernel"],
                }
                _atomic_write(_new_path(vault, action), _render(meta, action["body"]))
        elif action["op"] == "update":
            path = Path(action["path"])
            with bucket_lease(vault, action["bucket_id"]):
                meta, old_body = _split(path.read_text(encoding="utf-8"))
                if semantics_sha(old_body, bucket_current_semantics(meta)) != action["expected_sha"]:
                    # Ombre (or a person) changed it after planning; next sync re-plans.
                    counts["raced"] += 1
                    continue
                # Keep Ombre's own rendering (e.g. [[links]]) unless the text really changed.
                body = action["body"] if "body" in action["changed"] else old_body
                meta.update(action["semantics"])
                meta.update(action["kernel"])
                _atomic_write(path, _render(meta, body))
        counts[action["op"]] += 1
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
                             for op in ("create", "update", "unchanged", "conflict", "diverged")},
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
