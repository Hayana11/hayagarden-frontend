"""Canonical CC tool-surface fingerprint for observability.

Builds a stable ordered list of allowlisted tool names plus input schemas so
schema or ordering changes invalidate the tools fingerprint.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from tools.cc_usage_observability import sha256_canonical_json, sha256_text

_EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

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


def _parse_allowed_tools(allowed_tools: Optional[str]) -> list[str]:
    return sorted({x.strip() for x in str(allowed_tools or "").split(",") if x.strip()})


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
        # CC allowlist uses the umbrella server entry for codebase.
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


def _live_mcp_registry(mcp_config_path: Optional[str]) -> dict[str, dict[str, Any]]:
    if not mcp_config_path:
        return {}
    path = Path(mcp_config_path)
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = config.get("mcpServers")
    if not isinstance(servers, Mapping):
        return {}
    registry: dict[str, dict[str, Any]] = {}
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
        for tool in tools:
            cc_name = _mcp_cc_name(str(server), str(tool["name"]))
            registry[cc_name] = tool["input_schema"]
    return registry


def build_canonical_tool_surface(
    allowed_tools: Optional[str],
    *,
    mcp_config_path: Optional[str] = None,
    prefer_live_mcp: bool = True,
) -> dict[str, Any]:
    """Return canonical tool surface metadata for observability."""
    names = _parse_allowed_tools(allowed_tools)
    static = _static_schema_registry()
    live = _live_mcp_registry(mcp_config_path) if prefer_live_mcp else {}
    source = "static_registry"
    if live:
        source = "mcp_list_tools"

    surface: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in names:
        schema = live.get(name) or static.get(name)
        if schema is None:
            missing.append(name)
            schema = _EMPTY_SCHEMA
        surface.append({"name": name, "input_schema": schema})

    text = json.dumps(surface, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    status = "available"
    if not names:
        status = "unavailable"
    elif missing and not live:
        status = "partial"
    elif missing:
        status = "partial"

    return {
        "tool_schema_text": text,
        "tool_schema_sha256": sha256_text(text),
        "tool_schema_source": source if names else None,
        "tool_schema_measurement_status": status,
        "tool_count": len(names),
        "allowed_tool_count": len(names),
        "missing_tool_schemas": missing,
    }


@lru_cache(maxsize=8)
def cached_tool_surface(
    allowed_tools: str,
    mcp_config_path: str,
    mcp_mtime: float,
) -> dict[str, Any]:
    del mcp_mtime  # bust cache when mcp config file changes
    return build_canonical_tool_surface(
        allowed_tools,
        mcp_config_path=mcp_config_path or None,
    )


def resolve_tool_surface(
    allowed_tools: Optional[str],
    *,
    mcp_config_path: Optional[str] = None,
) -> dict[str, Any]:
    allowed = str(allowed_tools or "")
    path = str(mcp_config_path or "")
    mtime = 0.0
    if path:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0.0
    if allowed and path:
        return dict(cached_tool_surface(allowed, path, mtime))
    return build_canonical_tool_surface(allowed, mcp_config_path=path or None)
