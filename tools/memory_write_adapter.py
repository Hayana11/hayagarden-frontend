"""Formal memory.write provider adapter.

Validation lives at this provider boundary, while the existing
tools.memory_tool.save_memory remains the single persistence owner.
"""
from __future__ import annotations

from typing import Any

from tools import memory_tool

MAX_CONTENT_SIZE = 4000

def write_memory(db_path: str, *, content: Any) -> dict[str, Any]:
    if not isinstance(content, str):
        return {"status": "INVALID_CONTENT"}
    text = content.strip()
    if not text:
        return {"status": "INVALID_CONTENT"}
    if len(text) > MAX_CONTENT_SIZE:
        return {"status": "CONTENT_TOO_LONG", "max_content_size": MAX_CONTENT_SIZE}
    previous_db_path = memory_tool.DB_PATH
    memory_tool.DB_PATH = str(db_path or "").strip() or previous_db_path
    try:
        new_id = memory_tool.save_memory(text, type="MEMORY", author="fyodor", layer="long-term")
    finally:
        memory_tool.DB_PATH = previous_db_path
    return {"status": "CREATED", "id": int(new_id)}

if __name__ == "__main__":
    import json, sys
    payload = json.loads(sys.stdin.read() or "{}")
    if payload.get("operation") != "write_memory":
        raise ValueError("unknown memory write operation")
    print(json.dumps(write_memory(payload.get("db_path"), content=payload.get("content")), ensure_ascii=False, sort_keys=True))
