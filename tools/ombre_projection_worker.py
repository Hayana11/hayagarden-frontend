"""Ombre projection worker: runs inside Ombre's own interpreter and runtime.

The Memory Kernel projection never touches Ombre's files. It starts this
worker with Ombre's Python, pointing at Ombre's source tree and env file, and
the worker drives Ombre exclusively through ``BucketManager.create / update /
get`` (plus ``get_including_archive`` / ``list_all`` to read deletions and
buckets Ombre created on its own, and ``touch_many`` to reinforce memories a
reader actually used). Vault location, validation, normalisation,
locking, the ledger and derived indexes are all Ombre's.

``hold`` is never called: its similarity merge is the right behaviour for
Ombre's own writes, but an exact projection needs one bucket per lineage.

Importing Ombre costs over a second, so one process serves a batch of
requests: ``create_if_absent`` is get -> create -> get in a single call.

Protocol: argv = [worker, ombre_src_dir, ombre_env_file]; stdin = a JSON list
of requests; stdout = one JSON object {"ok": bool, "results"|"error": ...}, one
{"ok", "value"|"error"} result per request.
Before any request the worker checks the Ombre version and the BucketManager
signatures it depends on and refuses to run if they don't match.

This file must stay importable without anything from this repository.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys

SUPPORTED_VERSIONS = ("3.6.",)
REQUIRED_CREATE_PARAMS = (
    "content", "tags", "importance", "domain", "valence", "arousal", "bucket_type",
    "name", "pinned", "source_tool", "bucket_id_override", "imported", "defer_derived_index",
)
# Lifecycle and bookkeeping are Ombre's; only these semantics are projected.
UPDATE_FIELDS = ("content", "name", "domain", "tags", "importance", "valence", "arousal", "pinned")


class Incompatible(RuntimeError):
    pass


def _load_env_file(path: str) -> None:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


# Ombre's own read-side surfacing policy (SurfacePolicyVM), loaded with the runtime and
# required: Kernel readers depend on its per-mode verdicts.
_SURFACE_POLICY = None
SURFACE_MODES = ("automatic", "search", "dream", "importance", "spontaneous")


def _bucket_view(bucket: dict | None, *, deleted: bool = False) -> dict | None:
    if bucket is None:
        return None
    metadata = dict(bucket.get("metadata") or {})
    view = {
        "id": str(bucket.get("id") or metadata.get("id") or ""),
        "content": bucket.get("content") or "",
        "metadata": json.loads(json.dumps(metadata, ensure_ascii=False, default=str)),
        "deleted": deleted or bool(metadata.get("deleted_at") or metadata.get("tombstone")),
    }
    if _SURFACE_POLICY is not None:
        # One verdict per kind of read: automatic recall, explicit search, dream, ...
        modes = {}
        for mode in SURFACE_MODES:
            decision = _SURFACE_POLICY.evaluate_bucket(bucket, mode)
            modes[mode] = {"allowed": bool(decision.allowed), "reasons": list(decision.reasons)}
        view["lifecycle"] = {"modes": modes}
    return view


def load_runtime(src_dir: str):
    version_path = os.path.join(src_dir, "VERSION")
    with open(version_path, encoding="utf-8") as handle:
        version = handle.read().strip()
    if not version.startswith(SUPPORTED_VERSIONS):
        raise Incompatible(f"unsupported Ombre version {version!r}")
    sys.path.insert(0, src_dir)
    from bucket_manager import BucketManager  # noqa: E402 (Ombre's own module)
    from utils import load_config  # noqa: E402

    params = inspect.signature(BucketManager.create).parameters
    missing = [name for name in REQUIRED_CREATE_PARAMS if name not in params]
    for name in ("update", "get", "get_including_archive", "list_all"):
        if not callable(getattr(BucketManager, name, None)):
            missing.append(name)
    if missing:
        raise Incompatible("BucketManager lacks: " + ", ".join(missing))
    global _SURFACE_POLICY
    try:
        from ombrebrain.policy.surfacing import SurfacePolicyVM  # noqa: E402 (Ombre's own module)
        policy = SurfacePolicyVM.default()
        probe = {"id": "probe", "metadata": {"type": "dynamic"}}
        for mode in SURFACE_MODES:
            policy.evaluate_bucket(probe, mode)
    except Exception as exc:  # a reader would show what Ombre retired: refuse to run
        raise Incompatible(f"SurfacePolicyVM unavailable: {type(exc).__name__}: {exc}"[:200]) from exc
    _SURFACE_POLICY = policy
    return version, BucketManager(load_config())


async def _get(manager, bucket_id: str) -> dict | None:
    bucket = await manager.get(bucket_id)
    if bucket is not None:
        return _bucket_view(bucket)
    archived = await manager.get_including_archive(bucket_id)
    return _bucket_view(archived, deleted=True) if archived else None


async def handle(manager, request: dict) -> object:
    op = request.get("op")
    if op == "get":
        return await _get(manager, request["bucket_id"])
    if op == "create_if_absent":
        # Idempotent create: a bucket that already exists (e.g. written by a run
        # that crashed before its bookkeeping committed) is returned, not duplicated.
        existing = await _get(manager, request["bucket_id"])
        if existing is not None:
            return {"created": False, "bucket": existing}
        fields = request["fields"]
        created = await manager.create(
            fields["content"],
            tags=list(fields.get("tags") or []),
            importance=int(fields.get("importance") or 5),
            domain=list(fields.get("domain") or []),
            valence=float(fields.get("valence", 0.5)),
            arousal=float(fields.get("arousal", 0.3)),
            bucket_type=fields.get("bucket_type") or "dynamic",
            name=fields.get("name") or None,
            pinned=bool(fields.get("pinned")),
            source_tool="kernel_projection",
            bucket_id_override=request["bucket_id"],
            imported=True,
            defer_derived_index=True,
        )
        # Ombre 3.6 does not fail on an id collision: it silently picks a suffixed or
        # random id. Report the id it actually used so the caller can refuse it.
        created = str(created or "")
        return {"created": True, "bucket_id": created, "bucket": await _get(manager, created) if created else None}
    if op == "update":
        fields = {k: v for k, v in request["fields"].items() if k in UPDATE_FIELDS}
        # BucketManager.update returns False when it refused or committed nothing.
        if not await manager.update(request["bucket_id"], **fields):
            raise RuntimeError("ombre_update_not_committed")
        return {"updated": True}
    if op == "touch":
        # Explicit reinforcement of memories a caller actually used. touch only moves
        # last_active / activation_count, never a projected semantic field.
        touch_many = getattr(manager, "touch_many", None)
        if not callable(touch_many):
            raise Incompatible("BucketManager lacks: touch_many")
        await touch_many([str(b) for b in request.get("bucket_ids") or []], ripple=False)
        return {"touched": len(request.get("bucket_ids") or [])}
    if op == "list":
        buckets = await manager.list_all(include_archive=bool(request.get("include_archive")))
        return [_bucket_view(bucket) for bucket in buckets]
    raise ValueError(f"unknown op {op!r}")


async def run(src_dir: str, requests: list[dict]) -> dict:
    version, manager = load_runtime(src_dir)
    results = []
    for request in requests:
        # One failing request (e.g. Ombre's validation) must not hide the others' outcomes.
        try:
            if request.get("op") == "capabilities":
                value = {"version": version, "update_fields": list(UPDATE_FIELDS)}
            else:
                value = await handle(manager, request)
            results.append({"ok": True, "value": value})
        except Exception as exc:
            results.append({"ok": False, "error": f"{type(exc).__name__}:{str(exc)[:200]}"})
    return {"ok": True, "results": results}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(json.dumps({"ok": False, "error": "usage: worker <ombre_src_dir> <ombre_env_file>"}))
        return 2
    _, src_dir, env_file = argv
    try:
        _load_env_file(env_file)
        requests = json.loads(sys.stdin.read() or "[]")
        # Ombre logs to stdout/stderr; keep stdout for the protocol answer only.
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            answer = asyncio.run(run(src_dir, requests))
        finally:
            sys.stdout = real_stdout
    except Incompatible as exc:
        answer = {"ok": False, "error": "incompatible", "detail": str(exc)}
    except Exception as exc:
        answer = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}
    print(json.dumps(answer, ensure_ascii=False))
    return 0 if answer.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
