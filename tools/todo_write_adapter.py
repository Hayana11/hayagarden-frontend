"""API Relay Todo write confirmation and trusted execution adapter."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tools.confirmation_store import PendingAction, deferred_payload
from tools.todo_write_execution import CAPABILITY_ID, TOOL_NAME

API_OWNER_ID = "api-chat"
APPROVAL_PROMPT = "要把这件事记进待办吗？"
INTERNAL_TOKEN_ENV = "TODO_INTERNAL_EXECUTION_TOKEN"
INTERNAL_ROUTE = "/internal/todos/execute"


def deferred_todo_payload(action: PendingAction) -> dict[str, Any]:
    payload = deferred_payload(action, approval_prompt=APPROVAL_PROMPT)
    payload["capability_id"] = CAPABILITY_ID
    return payload


def execution_payload(action: PendingAction, *, owner_id: str) -> dict[str, Any]:
    return {
        "pending_action_id": action.pending_action_id,
        "approval_id": action.approval_id,
        "capability_id": action.capability_id,
        "tool_name": action.tool_name,
        "tool_input": dict(action.tool_input),
        "owner_id": owner_id,
    }


def format_result(result: Mapping[str, Any], tool_input: Mapping[str, Any]) -> str:
    if bool(result.get("ok")):
        return f"已添加待办 #{result.get('id', '?')}: {tool_input.get('content', '')}"
    return f"工具执行失败：{result.get('error') or 'Todo 写入失败'}"


def decode_result(raw: str) -> dict[str, Any]:
    try:
        result = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Todo execution returned invalid JSON") from exc
    if not isinstance(result, dict) or "ok" not in result:
        raise RuntimeError("Todo execution returned malformed result")
    return result
