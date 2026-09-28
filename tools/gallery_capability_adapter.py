"""UH-A0 Gallery capability adapter with server-trusted turn provenance."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from chat.gallery_provenance import resolve_trusted_gallery_turn
from chat.gallery_service import recall_gallery_photo, save_gallery_image, screenshot_chat
from tools.execution_fence import evaluate_tool_call, read_current_turn_lease


_PHYSICAL_TO_OPERATION = {
    "gallery_save": "mcp__capability__gallery_save",
    "gallery_recall": "mcp__capability__gallery_recall",
    "gallery_screenshot": "mcp__capability__gallery_screenshot",
}


def _verified_turn(operation: str, tool_input: Mapping[str, Any], expected: Any) -> str:
    lease, record = read_current_turn_lease(env=os.environ)
    decision = evaluate_tool_call(
        _PHYSICAL_TO_OPERATION[operation],
        tool_input,
        lease,
        expected_turn_id=record.get("turn_id"),
    )
    verified = str(decision.get("verified_turn_id") or "").strip()
    expected_id = str(expected or "").strip()
    if decision.get("lease_decision") != "ALLOW" or not verified or verified != expected_id:
        raise ValueError("gallery capability lease changed or is not allowed")
    return verified


def execute(payload: Mapping[str, Any]) -> dict[str, Any]:
    operation = str(payload.get("operation") or "").strip()
    if operation not in _PHYSICAL_TO_OPERATION:
        raise ValueError("unknown gallery capability operation")
    raw_input = payload.get("tool_input")
    if not isinstance(raw_input, Mapping):
        raise ValueError("tool_input must be an object")
    tool_input = dict(raw_input)
    turn_id = _verified_turn(operation, tool_input, payload.get("verified_turn_id"))
    db_path = str(payload.get("db_path") or os.environ.get("HAYA_DB_PATH") or "").strip()
    if not db_path:
        raise ValueError("HAYA_DB_PATH is required")

    if operation == "gallery_save":
        trusted = resolve_trusted_gallery_turn(db_path, turn_id)
        return save_gallery_image(
            trusted,
            db_path=db_path,
            attachment=str(tool_input.get("attachment") or ""),
            image_index=tool_input.get("image_index"),
            note=str(tool_input.get("note") or ""),
            album=str(tool_input.get("album") or "") or None,
            first_impression=str(tool_input.get("first_impression") or ""),
        )
    if operation == "gallery_recall":
        result = recall_gallery_photo(
            keyword=str(tool_input.get("keyword") or "") or None,
            emotion=str(tool_input.get("emotion") or "") or None,
            pid=str(tool_input.get("pid") or "") or None,
            inspect_question=str(tool_input.get("inspect_question") or "") or None,
            record_selection=False,
            touch_memory=False,
        )
        if result is None:
            return {"ok": False, "error": "gallery photo not found"}
        return {"ok": True, **result}
    return screenshot_chat(
        str(tool_input.get("viewpoint") or "fyodor"),
        repo_root=os.environ.get("UH_A0_REPO_ROOT") or Path(__file__).resolve().parent.parent,
        attachments_root=os.environ.get("HAYAGARDEN_ATTACHMENTS_ROOT") or "/opt/frontend",
    )


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        result = execute(payload)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:300]}
    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

