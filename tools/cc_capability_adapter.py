"""UH-A0 v1.0 Claude Code capability adapter (P3).

Maps frozen capability_manifest bindings onto a generation-stable Claude Code
physical tool surface.  This module does *not* enforce turn leases, implement
ASK / PreToolUse fences, merge Chat/Wake residents, enable Web/GitHub, or
change the production Daily live tool profile.  Those are later slices.

Physical surface is independent of turn_lease.  Lease decisions belong to P4.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from chat.cc_runtime import EXPECTED_CLAUDE_CODE_VERSION
from tools.capability_manifest import (
    P1_ENABLED_CAPABILITY_IDS,
    P1_RESERVED_CAPABILITY_IDS,
    get_capability,
)
from tools.cc_usage_observability import sha256_canonical_json

TOOL_PROFILE_UH_A0 = "uh_a0"

UH_A0_BUILTIN_TOOLS: tuple[str, ...] = (
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
)

FORBIDDEN_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash",
    "Edit",
    "Write",
    "Agent",
)

# Home MCP tools that exist on the shared Home server but are outside the
# UH-A0 P3 unified surface.  Listed for real availability exclusion via
# --disallowedTools (not treated as a substitute availability whitelist).
NON_P3_HOME_MCP_TOOLS: tuple[str, ...] = (
    "mcp__home__exec_vps",
    "mcp__home__light_on",
    "mcp__home__light_off",
    "mcp__home__light_bedside_warm",
    "mcp__home__light_bedside_neutral",
    "mcp__home__set_brightness",
    "mcp__home__set_color_temp",
    "mcp__home__collect_chat_moment",
)

HOME_MCP_CAPABILITY_IDS: tuple[str, ...] = (
    "memory.search",
    "home.light.status",
    "todo.read",
    "todo.write",
    "countdown.read",
    "ledger.read",
    "ledger.budget.read",
    "ledger.write",
)

NATIVE_FILE_CAPABILITY_IDS: tuple[str, ...] = (
    "files.read",
    "files.find",
    "code.search",
)

EXTERNAL_READ_CAPABILITY_IDS: tuple[str, ...] = ("web.search", "web.read")

DEFAULT_HOME_MCP_URL = "http://127.0.0.1:3100/mcp"
UH_A0_MCP_CONFIG_FILENAME = "cc-tools-uh-a0.json"
UH_A0_SETTINGS_FILENAME = "cc-settings-uh-a0.json"
DEFAULT_TURN_LEASE_FILENAME = ".uh-a0-current-turn-lease.json"


def resolve_uh_a0_turn_lease_path(cwd=None, *, env=None):
    environ = env or os.environ
    configured = str(environ.get("UH_A0_TURN_LEASE_PATH") or "").strip()
    if configured:
        return configured
    return str(Path(cwd or Path.cwd()) / DEFAULT_TURN_LEASE_FILENAME)


def build_uh_a0_settings():
    return {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": "python3 -m tools.execution_fence pretooluse"}]}]}}


def write_uh_a0_settings(cwd):
    out = Path(cwd) / UH_A0_SETTINGS_FILENAME
    out.write_text(json.dumps(build_uh_a0_settings(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(out)


def _claude_binding(capability_id: str) -> str:
    entry = get_capability(capability_id)
    if entry is None:
        raise KeyError(f"unknown capability_id: {capability_id}")
    binding = (entry.get("provider_bindings") or {}).get("claude_code")
    if not isinstance(binding, str) or not binding.strip():
        raise KeyError(f"missing claude_code binding for {capability_id}")
    if capability_id in P1_RESERVED_CAPABILITY_IDS:
        raise KeyError(f"RESERVED capability cannot enter P3 surface: {capability_id}")
    if capability_id not in P1_ENABLED_CAPABILITY_IDS:
        raise KeyError(f"capability is not P1-enabled: {capability_id}")
    return binding.strip()


def uh_a0_home_mcp_tools() -> tuple[str, ...]:
    """Exact Home MCP CC names for P1-enabled Home capabilities."""
    return tuple(_claude_binding(cid) for cid in HOME_MCP_CAPABILITY_IDS)


def uh_a0_native_bindings() -> dict[str, str]:
    return {cid: _claude_binding(cid) for cid in NATIVE_FILE_CAPABILITY_IDS}


def uh_a0_external_read_tools() -> tuple[str, ...]:
    """Exact Claude Code built-ins for enabled External Read capabilities."""
    return tuple(_claude_binding(cid) for cid in EXTERNAL_READ_CAPABILITY_IDS)


def resolve_home_mcp_url(legacy_mcp_config_path: str | os.PathLike[str] | None = None) -> str:
    path = Path(legacy_mcp_config_path) if legacy_mcp_config_path else None
    if path and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or {}
            home = servers.get("home") or {}
            url = str(home.get("url") or "").strip()
            if url:
                return url
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    return DEFAULT_HOME_MCP_URL


def build_uh_a0_mcp_config(
    *,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Strict UH-A0 MCP config: Home only. No brain/codebase/workspace."""
    return {
        "mcpServers": {
            "home": {
                "type": "http",
                "url": resolve_home_mcp_url(legacy_mcp_config_path),
                "headers": {"X-UH-A0-Profile": "uh_a0"},
            }
        }
    }


def write_uh_a0_mcp_config(
    cwd: str | os.PathLike[str],
    *,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
) -> str:
    root = Path(cwd)
    out = root / UH_A0_MCP_CONFIG_FILENAME
    payload = build_uh_a0_mcp_config(legacy_mcp_config_path=legacy_mcp_config_path)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(out)


def diagnose_tool_search(
    *,
    env: Mapping[str, str] | None = None,
    actual_version: str | None = None,
) -> dict[str, Any]:
    """Read-only ToolSearch environment diagnosis for P3 loading choice."""
    environ = env or os.environ
    base_url = str(environ.get("ANTHROPIC_BASE_URL") or "").strip()
    enable_raw = environ.get("ENABLE_TOOL_SEARCH")
    enable_set = enable_raw is not None and str(enable_raw).strip() != ""
    enable_value = str(enable_raw).strip() if enable_set else None

    if not base_url:
        base_url_class = "absent"
        host_class = "first_party"
    else:
        base_url_class = "custom"
        lowered = base_url.lower()
        if "api.anthropic.com" in lowered:
            host_class = "first_party"
            base_url_class = "first_party"
        else:
            host_class = "custom"

    # Without ENABLE_TOOL_SEARCH and without a live deferred-load / tool_reference
    # proof, do not claim ToolSearch AVAILABLE — even on first-party hosts.
    if host_class == "custom":
        proxy_evidence = "UNKNOWN"
        status = "ENVIRONMENT_BLOCKED"
        detail = (
            "custom ANTHROPIC_BASE_URL present without tool_reference proof; "
            "ENABLE_TOOL_SEARCH alone is insufficient"
        )
    elif not enable_set:
        proxy_evidence = "UNKNOWN"
        status = "PRELOAD_FALLBACK"
        detail = (
            "ENABLE_TOOL_SEARCH unset; no live tool_reference deferred-load proof; "
            "preload only the fixed P3 Home surface"
        )
    elif enable_value.lower() in {"0", "false", "off", "no"}:
        proxy_evidence = "UNKNOWN"
        status = "PRELOAD_FALLBACK"
        detail = "ENABLE_TOOL_SEARCH explicitly disabled"
    else:
        # Flag enabled on first-party, but P3 still lacks a captured live
        # deferred-load demonstration in this construction slice.
        proxy_evidence = "UNKNOWN"
        status = "PRELOAD_FALLBACK"
        detail = (
            "ENABLE_TOOL_SEARCH set but no captured live tool_reference proof; "
            "keep preload fallback for the fixed P3 surface"
        )

    return {
        "expected_claude_code_version": EXPECTED_CLAUDE_CODE_VERSION,
        "actual_claude_code_version": actual_version or "",
        "anthropic_base_url_class": base_url_class,
        "host_class": host_class,
        "enable_tool_search": enable_value,
        "enable_tool_search_set": enable_set,
        "proxy_tool_reference_evidence": proxy_evidence,
        "tool_search_status": status,
        "detail": detail,
    }


def loading_plan_from_manifest() -> dict[str, str]:
    """capability_id -> loading_policy for P3 Home + native file caps."""
    out: dict[str, str] = {}
    for cid in HOME_MCP_CAPABILITY_IDS + NATIVE_FILE_CAPABILITY_IDS:
        entry = get_capability(cid)
        assert entry is not None
        out[cid] = str(entry["loading_policy"])
    return out


def short_intent_instructions() -> str:
    """Return compact natural-language guidance, not manifest policy fields."""
    return (
        "When an answer depends on real facts, prefer the available read tools "
        "instead of guessing. Use a tool when the current turn genuinely needs "
        "its information; do not call tools just to demonstrate capability."
    )


def physical_surface_names() -> tuple[str, ...]:
    return UH_A0_BUILTIN_TOOLS + uh_a0_home_mcp_tools()


def physical_surface_fingerprint() -> str:
    return sha256_canonical_json(
        {
            "built_ins": list(UH_A0_BUILTIN_TOOLS),
            "home_mcp": list(uh_a0_home_mcp_tools()),
            "forbidden_built_ins": list(FORBIDDEN_BUILTIN_TOOLS),
            "non_p3_home": list(NON_P3_HOME_MCP_TOOLS),
        }
    )


def build_uh_a0_spawn_plan(
    *,
    cwd: str | os.PathLike[str] | None = None,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
    turn_lease: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    actual_version: str | None = None,
    write_mcp_config: bool = True,
) -> dict[str, Any]:
    """Build generation-stable UH-A0 Claude Code spawn plan.

    ``turn_lease`` is accepted and ignored for physical surface construction so
    callers cannot accidentally couple lease contents to tool availability.
    """
    _ = turn_lease  # explicit: lease must not reshape physical surface
    diagnosis = diagnose_tool_search(env=env, actual_version=actual_version)
    home_tools = uh_a0_home_mcp_tools()
    native = uh_a0_native_bindings()
    loading = loading_plan_from_manifest()

    if diagnosis["tool_search_status"] == "ENVIRONMENT_BLOCKED":
        home_loading_mode = "blocked"
    elif diagnosis["tool_search_status"] == "AVAILABLE":
        home_loading_mode = "toolsearch_deferred"
    else:
        home_loading_mode = "preload_fallback"

    mcp_config = build_uh_a0_mcp_config(legacy_mcp_config_path=legacy_mcp_config_path)
    target_cwd = Path(cwd or (Path(legacy_mcp_config_path).parent if legacy_mcp_config_path else Path.cwd()))
    mcp_path = ""
    if write_mcp_config:
        mcp_path = write_uh_a0_mcp_config(target_cwd, legacy_mcp_config_path=legacy_mcp_config_path)
    settings_path = write_uh_a0_settings(target_cwd)
    turn_lease_path = resolve_uh_a0_turn_lease_path(target_cwd, env=env)

    allowlist = list(UH_A0_BUILTIN_TOOLS) + list(home_tools)
    disallowed = list(FORBIDDEN_BUILTIN_TOOLS) + list(NON_P3_HOME_MCP_TOOLS)
    built_in_csv = ",".join(UH_A0_BUILTIN_TOOLS)
    allowed_csv = ",".join(allowlist)
    disallowed_csv = ",".join(disallowed)

    spawn_extra_args = [
        "--settings",
        settings_path,
        "--mcp-config",
        mcp_path or json.dumps(mcp_config, ensure_ascii=False, separators=(",", ":")),
        "--strict-mcp-config",
        "--allowedTools",
        allowed_csv,
        "--disallowedTools",
        disallowed_csv,
    ]

    plan = {
        "tool_profile": TOOL_PROFILE_UH_A0,
        "built_in_tools": UH_A0_BUILTIN_TOOLS,
        "built_in_tools_csv": built_in_csv,
        "home_mcp_tools": home_tools,
        "native_bindings": native,
        "surface_allowlist": tuple(allowlist),
        "surface_allowlist_csv": allowed_csv,
        "disallowed_tools": tuple(disallowed),
        "disallowed_tools_csv": disallowed_csv,
        "mcp_config": mcp_config,
        "mcp_config_path": mcp_path,
        "settings_path": settings_path,
        "turn_lease_path": turn_lease_path,
        "spawn_extra_args": spawn_extra_args,
        "physical_surface_fingerprint": physical_surface_fingerprint(),
        "loading_plan": loading,
        "memory_search_loading": loading["memory.search"],
        "home_loading_mode": home_loading_mode,
        "tool_search": diagnosis,
        "intent_instructions": short_intent_instructions(),
        # Visibility claim for reports: what Claude can see under UH-A0 plan.
        "claude_visible_built_ins": UH_A0_BUILTIN_TOOLS,
        "claude_visible_mcp_tools": home_tools,
        "claude_absent_servers": ("brain", "codebase", "workspace"),
    }
    return plan


def assert_reserved_absent_from_surface(surface: Sequence[str]) -> None:
    reserved_bindings: list[str] = []
    for cid in sorted(P1_RESERVED_CAPABILITY_IDS):
        entry = get_capability(cid) or {}
        binding = (entry.get("provider_bindings") or {}).get("claude_code")
        if isinstance(binding, str) and binding.strip():
            reserved_bindings.append(binding.strip())
        elif isinstance(binding, (list, tuple)):
            reserved_bindings.extend(str(x).strip() for x in binding if str(x).strip())
    surface_set = set(surface)
    overlap = surface_set.intersection(reserved_bindings)
    # Also forbid raw reserved built-in names that may lack bindings.
    overlap.update(surface_set.intersection(FORBIDDEN_BUILTIN_TOOLS))
    if overlap:
        raise AssertionError(f"RESERVED tools leaked into surface: {sorted(overlap)}")

