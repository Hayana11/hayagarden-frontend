"""Formal memory.write provider adapter.

Validation lives at this provider boundary, while the existing
tools.memory_tool.save_memory remains the single persistence owner.
"""
from __future__ import annotations

from typing import Any

from tools import memory_tool

MAX_CONTENT_SIZE = 4000


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
