"""Canonical catalog and local adapter for the external MCP model surface.

The catalog is a read-only projection of the production External MCP graph.
Only a connected server with a present candidate, matching server revision,
and an intact SDK snapshot can enter the model surface.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any

from .external_server_registry import CONNECTED_STATE
from .external_tool_registry import PRESENT, fingerprint_raw_tool
from .external_mcp_production import open_external_mcp_production
from .external_mcp_invocation import (
    FAILED_PRE_CALL,
    OUTCOME_UNKNOWN,
    SUCCEEDED,
    TOOL_ERROR,
)

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}
_SURFACE_PREFIX = "mcp__external__"
_MAX_DESCRIPTION = 12000
_MAX_SURFACE_TOOL_NAME = 64
_SAFE_SLUG = re.compile(r"[^a-z0-9]+")
_HEX = frozenset("0123456789abcdef")


def _slug(value: object, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _SAFE_SLUG.sub("_", text).strip("_")
    return text[:64] or fallback


def surface_tool_name(server_display_name: str, remote_tool_name: str, control_id: str) -> str:
    server_slug = _slug(server_display_name, "server")
    tool_slug = _slug(remote_tool_name, "tool")
    short_hash = hashlib.sha256(control_id.encode("utf-8")).hexdigest()[:10]
    readable_budget = _MAX_SURFACE_TOOL_NAME - len(short_hash) - 4
    server_budget = max(1, readable_budget // 2)
    tool_budget = max(1, readable_budget - server_budget)
    return (
        f"{server_slug[:server_budget]}__"
        f"{tool_slug[:tool_budget]}__{short_hash}"
    )


def _valid_fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX for character in value.lower())
    )


def _schema(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = raw.get("inputSchema", raw.get("input_schema", _EMPTY_SCHEMA))
    return dict(value) if isinstance(value, Mapping) else dict(_EMPTY_SCHEMA)


def _snapshot(candidate: Mapping[str, Any], snapshots: tuple[Mapping[str, Any], ...]) -> dict[str, Any] | None:
    fingerprint = candidate.get("current_fingerprint")
    source_revision = candidate.get("current_source_registry_revision")
    if not _valid_fingerprint(fingerprint) or not isinstance(source_revision, int):
        return None
    for item in snapshots:
        if (
            item.get("fingerprint") == fingerprint
            and item.get("source_registry_revision") == source_revision
            and isinstance(item.get("raw_snapshot_json"), str)
        ):
            try:
                raw = json.loads(item["raw_snapshot_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(raw, Mapping):
                return None
            if fingerprint_raw_tool(raw) != fingerprint:
                return None
            return dict(raw)
    return None


def _tool_row(server: Any, candidate: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    control_id = str(candidate.get("control_id") or "")
    remote_name = str(candidate.get("tool_name") or "")
    source_revision = candidate.get("current_source_registry_revision")
    fingerprint = candidate.get("current_fingerprint")
    current = (
        server.lifecycle_state == CONNECTED_STATE
        and candidate.get("presence_state") == PRESENT
        and candidate.get("current_source_registry_revision") == server.revision
        and snapshot is not None
        and _valid_fingerprint(fingerprint)
    )
    raw_description = str((snapshot or {}).get("description") or remote_name).strip()
    description = f"{server.display_name}: {raw_description}"[:_MAX_DESCRIPTION]
    return {
        "control_id": control_id,
        "remote_tool_name": remote_name,
        "description": description,
        "input_schema": _schema(snapshot or {}),
        "fingerprint": fingerprint,
        "source_registry_revision": source_revision,
        "surface_tool_name": surface_tool_name(server.display_name, remote_name, control_id),
        "available": current,
    }


def _catalog_for_graph(graph: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for server in graph.server_registry.list():
        tools = [
            _tool_row(
                server,
                candidate,
                _snapshot(candidate, graph.candidate_registry.list_snapshots(server.server_id, candidate["tool_name"])),
            )
            for candidate in graph.candidate_registry.list_candidates(server.server_id)
        ]
        result.append({
            "server_id": server.server_id,
            "display_name": server.display_name,
            "lifecycle_state": server.lifecycle_state,
            "transport": server.transport,
            "revision": server.revision,
            "tools": tools,
        })
    return result


def list_external_surface(graph: Any | None = None) -> list[dict[str, Any]]:
    """Return the one canonical external catalog.

    When no graph is supplied, the production graph is opened for this
    request only. Storage failures fail closed to an empty model surface.
    """
    if graph is not None:
        return _catalog_for_graph(graph)
    try:
        with open_external_mcp_production() as production:
            return _catalog_for_graph(production)
    except Exception:
        return []


def current_external_tools(catalog: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], ...]:
    source = list_external_surface() if catalog is None else catalog
    return tuple(
        tool
        for server in source
        if server.get("lifecycle_state") == CONNECTED_STATE
        for tool in server.get("tools", ())
        if tool.get("available") is True
    )


def model_tool_definitions(catalog: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [
        {
            "name": tool["surface_tool_name"],
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
        }
        for tool in current_external_tools(catalog)
    ]


def _bounded_local_result(status: str, reason: str | None = None) -> dict[str, Any]:
    if status == TOOL_ERROR:
        return {
            "status": TOOL_ERROR,
            "error": {"code": "EXTERNAL_TOOL_ERROR", "summary": "External MCP tool returned an error."},
        }
    code = str(reason or "EXTERNAL_CALL_UNAVAILABLE").upper()
    code = re.sub(r"[^A-Z0-9_]", "_", code)[:64] or "EXTERNAL_CALL_UNAVAILABLE"
    return {
        "status": status if status in {FAILED_PRE_CALL, OUTCOME_UNKNOWN} else FAILED_PRE_CALL,
        "error": {"code": code, "summary": "External MCP call was not completed."},
    }


def invoke_external_surface(
    surface_name: object,
    tool_input: Any,
    *,
    turn_id: str | None,
) -> dict[str, Any]:
    if not isinstance(surface_name, str) or not surface_name.strip():
        return _bounded_local_result(FAILED_PRE_CALL, "INVALID_SURFACE_TOOL")
    if not isinstance(tool_input, Mapping):
        return _bounded_local_result(FAILED_PRE_CALL, "INVALID_TOOL_INPUT")
    if not isinstance(turn_id, str) or not turn_id.strip():
        return _bounded_local_result(FAILED_PRE_CALL, "TURN_RECORD_UNAVAILABLE")
    try:
        with open_external_mcp_production() as graph:
            selected = next(
                (
                    tool
                    for tool in current_external_tools(_catalog_for_graph(graph))
                    if tool["surface_tool_name"] == surface_name
                ),
                None,
            )
            if selected is None:
                return _bounded_local_result(FAILED_PRE_CALL, "SURFACE_TOOL_UNAVAILABLE")
            response = graph.runtime.invoke(
                selected["control_id"],
                dict(tool_input),
                None,
                expected_turn_id=turn_id,
            )
    except Exception:
        return _bounded_local_result(OUTCOME_UNKNOWN, "EXTERNAL_RUNTIME_UNAVAILABLE")
    status = response.get("status")
    mcp_result = response.get("mcp_result")
    preserves_call_result = (
        isinstance(mcp_result, Mapping)
        and isinstance(mcp_result.get("content"), list)
        and (
            "isError" not in mcp_result
            or isinstance(mcp_result.get("isError"), bool)
        )
    )
    if status == SUCCEEDED:
        return {
            "status": SUCCEEDED,
            "result": dict(mcp_result) if preserves_call_result else None,
        }
    if status == TOOL_ERROR and preserves_call_result:
        return {"status": TOOL_ERROR, "result": dict(mcp_result)}
    if status == TOOL_ERROR:
        return _bounded_local_result(TOOL_ERROR)
    return _bounded_local_result(status or FAILED_PRE_CALL, response.get("reason_code"))


def _turn_id_from_environment() -> str | None:
    from .execution_fence import read_current_turn_lease
    _, record = read_current_turn_lease(env=os.environ)
    value = record.get("turn_id")
    return value if isinstance(value, str) and value.strip() else None


def _main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    if not isinstance(request, Mapping):
        raise ValueError("request must be an object")
    operation = request.get("operation")
    if operation == "list":
        response = {"tools": model_tool_definitions()}
    elif operation == "call":
        response = invoke_external_surface(
            request.get("name"),
            request.get("arguments", {}),
            turn_id=_turn_id_from_environment(),
        )
    else:
        raise ValueError("unsupported operation")
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception:
        sys.stdout.write(json.dumps({
            "status": FAILED_PRE_CALL,
            "error": {"code": "SURFACE_ADAPTER_FAILED", "summary": "External MCP surface adapter failed."},
        }, ensure_ascii=False, separators=(",", ":")))
        raise SystemExit(0)


__all__ = [
    "current_external_tools",
    "invoke_external_surface",
    "list_external_surface",
    "model_tool_definitions",
    "surface_tool_name",
]
