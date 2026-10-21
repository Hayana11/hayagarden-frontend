"""Formal memory.write provider adapter.

Validation lives at this provider boundary. With runtime_config
memory.kernel_write on, the Memory Kernel is the persistence owner (B-3);
otherwise tools.memory_tool.save_memory remains the owner.
"""
from __future__ import annotations

from typing import Any

from tools import memory_tool

MAX_CONTENT_SIZE = 4000


def _write_via_kernel(db_path: str, text: str, surface: str | None, turn_id: str | None):
    """B-3: Kernel-first when memory.kernel_write is on; None falls back to posts."""
    try:
        from tools.memory_kernel_switch import write_enabled

        if not write_enabled(db_path):
            return None
        from tools import memory_kernel_store

        result = memory_kernel_store.remember(
            text,
            source="memory_write",
            provenance={"surface": surface or "", "turn_id": turn_id or ""},
        )
    except Exception:
        return None
    if result.get("status") not in ("CREATED", "DEDUPLICATED"):
        return None
    # Callers only distinguish CREATED; a duplicate is already remembered.
    return {
        "status": "CREATED",
        "id": None,
        "kernel_state_id": result.get("state_id"),
        "ombre_bucket_id": result.get("bucket_id"),
        "deduplicated": result["status"] == "DEDUPLICATED",
    }


def write_memory(
    db_path: str,
    *,
    content: Any,
    shadow_surface: str | None = None,
    shadow_turn_id: str | None = None,
    shadow_request_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str):
        return {"status": "INVALID_CONTENT"}
    text = content.strip()
    if not text:
        return {"status": "INVALID_CONTENT"}
    if len(text) > MAX_CONTENT_SIZE:
        return {"status": "CONTENT_TOO_LONG", "max_content_size": MAX_CONTENT_SIZE}
    previous_db_path = memory_tool.DB_PATH
    resolved_db_path = str(db_path or "").strip() or previous_db_path
    kernel_result = _write_via_kernel(resolved_db_path, text, shadow_surface, shadow_turn_id)
    if kernel_result is not None:
        return kernel_result
    memory_tool.DB_PATH = resolved_db_path
    try:
        new_id = memory_tool.save_memory(text, type="MEMORY", author="fyodor", layer="long-term")
    finally:
        memory_tool.DB_PATH = previous_db_path
    result = {"status": "CREATED", "id": int(new_id)}
    try:
        from tools.memory_interop_shadow import observe_memory_write

        observe_memory_write(
            posts_db_path=resolved_db_path,
            result=result,
            content=text,
            source_surface=shadow_surface,
            turn_id=shadow_turn_id,
            request_id=shadow_request_id,
        )
    except Exception:
        pass
    return result


if __name__ == "__main__":
    import json, sys
    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("operation") != "write_memory":
        raise ValueError("unknown memory write operation")
    print(json.dumps(write_memory(
        payload.get("db_path"),
        content=payload.get("content"),
        shadow_surface=payload.get("shadow_surface"),
        shadow_turn_id=payload.get("shadow_turn_id") or payload.get("verified_turn_id"),
        shadow_request_id=payload.get("shadow_request_id"),
    ), ensure_ascii=False, sort_keys=True))
