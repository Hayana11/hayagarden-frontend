"""Canonical CC tool-surface fingerprint for observability.

Each resident generation captures one ordered snapshot of the allowlisted tool
surface.  MCP ``tools/list`` return order is preserved; schema or ordering drift
invalidates the fingerprint.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from tools.cc_usage_observability import sha256_canonical_json, sha256_text

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

LiveToolListsProvider = Callable[
    [Optional[str]],
    list[tuple[str, list[dict[str, Any]]]],
]

# Brain MCP core tools (parameterless); schema drift still changes fingerprint.
_BRAIN_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "mcp__brain__breath": _EMPTY_SCHEMA,
    "mcp__brain__grow": _EMPTY_SCHEMA,
    "mcp__brain__hold": _EMPTY_SCHEMA,
    "mcp__brain__pulse": _EMPTY_SCHEMA,
    "mcp__brain__trace": _EMPTY_SCHEMA,
}

# Home MCP allowlisted tools — mirrors mcp-http-server.js registrations.
_HOME_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "mcp__home__light_on": _EMPTY_SCHEMA,
    "mcp__home__light_off": _EMPTY_SCHEMA,
    "mcp__home__get_light_status": _EMPTY_SCHEMA,
    "mcp__home__light_bedside_warm": _EMPTY_SCHEMA,
    "mcp__home__light_bedside_neutral": _EMPTY_SCHEMA,
    "mcp__home__get_todos": _EMPTY_SCHEMA,
    "mcp__home__add_todo": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "due_date": {"type": "string"},
        },
        "required": ["content"],
    },
    "mcp__home__get_countdowns": _EMPTY_SCHEMA,
    "mcp__home__get_ledger": {
        "type": "object",
        "properties": {"month": {"type": "string"}},
    },
    "mcp__home__add_ledger": {
        "type": "object",
        "properties": {
            "amount": {"type": "number"},
            "category": {"type": "string"},
            "note": {"type": "string"},
            "date": {"type": "string"},
        },
        "required": ["amount"],
    },
    "mcp__home__get_ledger_budget": {
        "type": "object",
        "properties": {"month": {"type": "string"}},
    },
    "mcp__home__search_memories": {
        "type": "object",
        "properties": {"keyword": {"type": "string"}},
        "required": ["keyword"],
    },
    "mcp__home__collect_chat_moment": {
        "type": "object",
        "properties": {
            "turn_key": {"type": "string"},
            "previous_turns": {"type": "integer"},
            "caption": {"type": "string"},
        },
    },
}


def _parse_allowed_tools_ordered(allowed_tools: Optional[str]) -> list[str]:
    """Preserve allowlist CSV order; dedupe by first occurrence only."""
    seen: set[str] = set()
    out: list[str] = []
    for part in str(allowed_tools or "").split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _static_schema_registry() -> dict[str, dict[str, Any]]:
    registry = dict(_BRAIN_TOOL_SCHEMAS)
    registry.update(_HOME_TOOL_SCHEMAS)
    try:
        from codebase.client import CODEBASE_TOOLS

        codebase_surface = []
        for tool in CODEBASE_TOOLS:
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                schema = _EMPTY_SCHEMA
            registry[f"mcp__codebase__{name}"] = schema
            codebase_surface.append({"name": name, "input_schema": schema})
        registry["mcp__codebase"] = {
            "type": "object",
            "properties": {
                "_codebase_tools_sha256": {
                    "const": sha256_canonical_json(codebase_surface),
                },
            },
        }
    except Exception:
        registry.setdefault("mcp__codebase", _EMPTY_SCHEMA)
    try:
        from tools import workspace_registry

        for tool in workspace_registry.build_resident_tool_defs():
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            schema = tool.get("input_schema")
            registry[f"mcp__workspace__{name}"] = (
                schema if isinstance(schema, dict) else _EMPTY_SCHEMA
            )
    except Exception:
        pass
    return registry


def _mcp_cc_name(server: str, tool_name: str) -> str:
    return f"mcp__{server}__{tool_name}"


def _fetch_mcp_tools(url: str, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Best-effort MCP tools/list over streamable HTTP."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    req = urllib.request.Request(
        url.rstrip("/"),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    if isinstance(data, dict) and "result" in data:
        tools = (data.get("result") or {}).get("tools") or []
    else:
        tools = []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        schema = tool.get("inputSchema") or tool.get("input_schema") or _EMPTY_SCHEMA
        if not isinstance(schema, dict):
            schema = _EMPTY_SCHEMA
        out.append({"name": name, "input_schema": schema})
    return out


def _default_live_tool_lists(
    mcp_config_path: Optional[str],
) -> list[tuple[str, list[dict[str, Any]]]]:
    if not mcp_config_path:
        return []
    path = Path(mcp_config_path)
    if not path.is_file():
        return []
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    servers = config.get("mcpServers")
    if not isinstance(servers, Mapping):
        return []
    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for server, meta in servers.items():
        if not isinstance(meta, Mapping):
            continue
        url = str(meta.get("url") or "").strip()
        if not url:
            continue
        try:
            tools = _fetch_mcp_tools(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
        ordered.append((str(server), tools))
    return ordered


def _build_ordered_surface(
    allowlist: list[str],
    *,
    live_tool_lists: list[tuple[str, list[dict[str, Any]]]],
    static_registry: Mapping[str, dict[str, Any]],
    used_live: bool,
) -> tuple[list[dict[str, Any]], list[str], str]:
    allowed = set(allowlist)
    surface: list[dict[str, Any]] = []
    seen: set[str] = set()
    missing: list[str] = []

    for _server, tools in live_tool_lists:
        for tool in tools:
            cc_name = _mcp_cc_name(_server, str(tool.get("name") or ""))
            if cc_name not in allowed or cc_name in seen:
                continue
            schema = tool.get("input_schema")
            if not isinstance(schema, dict):
                schema = _EMPTY_SCHEMA
            surface.append({"name": cc_name, "input_schema": schema})
            seen.add(cc_name)

    for name in allowlist:
        if name in seen:
            continue
        schema = static_registry.get(name)
        if schema is None:
            missing.append(name)
            schema = _EMPTY_SCHEMA
        surface.append({"name": name, "input_schema": schema})
        seen.add(name)

    if not allowlist:
        source = "unavailable"
        status = "unavailable"
    elif used_live and not missing:
        source = "mcp_list_tools"
        status = "available"
    elif used_live and missing:
        source = "mcp_list_tools"
        status = "partial"
    elif missing:
        source = "static_registry"
        status = "partial"
    else:
        source = "static_registry"
        status = "available"
    return surface, missing, status if allowlist else "unavailable"


def capture_tool_surface_snapshot(
    allowed_tools: Optional[str],
    *,
    mcp_config_path: Optional[str] = None,
    prefer_live_mcp: bool = True,
    live_tool_lists_provider: Optional[LiveToolListsProvider] = None,
) -> dict[str, Any]:
    """Capture one generation-scoped tool surface snapshot."""
    allowlist = _parse_allowed_tools_ordered(allowed_tools)
    static = _static_schema_registry()
    provider = live_tool_lists_provider or _default_live_tool_lists
    live_lists = provider(mcp_config_path) if prefer_live_mcp else []
    used_live = bool(live_lists)
    surface, missing, status = _build_ordered_surface(
        allowlist,
        live_tool_lists=live_lists,
        static_registry=static,
        used_live=used_live,
    )
    source = "mcp_list_tools" if used_live and allowlist else (
        "static_registry" if allowlist else None
    )
    if not allowlist:
        source = None
    elif status == "partial" and not used_live:
        source = "static_registry"

    # Preserve list order; do not sort the surface array.
    text = json.dumps(surface, ensure_ascii=False, separators=(",", ":"))
    return {
        "tool_schema_text": text,
        "tool_schema_sha256": sha256_text(text),
        "tool_schema_source": source,
        "tool_schema_measurement_status": status,
        "tool_count": len(allowlist),
        "allowed_tool_count": len(allowlist),
        "missing_tool_schemas": missing,
    }
