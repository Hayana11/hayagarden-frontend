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
from tools.capability_state import (
    CapabilityStateError,
    RUNTIME_STATE_DENY,
    RUNTIME_STATE_INHERIT,
    RUNTIME_STATE_OFF,
    RUNTIME_STATE_ON,
    read_capability_state,
)

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
    "countdown.read",
)

INTERNAL_MCP_CAPABILITY_IDS: tuple[str, ...] = ()

CAPABILITY_PROXY_CAPABILITY_IDS: tuple[str, ...] = (
    "memory.search",
    "memory.write",
    "diary.write",
    "task.timer.start",
    "self_trigger.schedule",
    "self_trigger.cancel",
    "home.light.status",
    "todo.read",
    "todo.write",
    "ledger.read",
    "ledger.budget.read",
    "ledger.write",
)

INTERNAL_MCP_LEGACY_CAPABILITY_IDS: tuple[str, ...] = (
    "todo.read",
    "todo.write",
    "ledger.read",
    "ledger.budget.read",
    "ledger.write",
    "memory.search",
    "memory.write",
    "home.light.status",
)

# Legacy Internal MCP registrations remain available to other providers but
# are explicitly denied from the Claude UH-A0 surface.
INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__internal__get_ledger",
    "mcp__internal__get_ledger_budget",
    "mcp__internal__get_todos",
    "mcp__internal__search_memories",
    "mcp__internal__write_memory",
    "mcp__internal__add_todo",
    "mcp__internal__add_ledger",
)

NATIVE_FILE_CAPABILITY_IDS: tuple[str, ...] = (
    "files.read",
    "files.find",
    "code.search",
)

EXTERNAL_READ_CAPABILITY_IDS: tuple[str, ...] = ("web.search", "web.read")

DEFAULT_HOME_MCP_URL = "http://127.0.0.1:3100/mcp"
DEFAULT_INTERNAL_MCP_URL = "http://127.0.0.1:3101/mcp"
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


def _provider_binding(capability_id: str, provider: str) -> str:
    entry = get_capability(capability_id)
    if entry is None:
        raise KeyError(f"unknown capability_id: {capability_id}")
    binding = (entry.get("provider_bindings") or {}).get(provider)
    if not isinstance(binding, str) or not binding.strip():
        raise KeyError(f"missing {provider} binding for {capability_id}")
    return binding.strip()


def _claude_binding(capability_id: str) -> str:
    binding = _provider_binding(capability_id, "claude_code")
    if capability_id in P1_RESERVED_CAPABILITY_IDS:
        raise KeyError(f"RESERVED capability cannot enter P3 surface: {capability_id}")
    if capability_id not in P1_ENABLED_CAPABILITY_IDS:
        raise KeyError(f"capability is not P1-enabled: {capability_id}")
    return binding


def uh_a0_home_mcp_tools() -> tuple[str, ...]:
    """Exact Home MCP CC names for non-Todo P1 Home capabilities."""
    return tuple(_claude_binding(cid) for cid in HOME_MCP_CAPABILITY_IDS)


def uh_a0_internal_mcp_tools() -> tuple[str, ...]:
    """Exact Internal MCP CC names for Daily read capabilities."""
    return tuple(_claude_binding(cid) for cid in INTERNAL_MCP_CAPABILITY_IDS)


def uh_a0_capability_proxy_tools() -> tuple[str, ...]:
    """Exact Claude CC names for capability-facing proxy tools."""
    return tuple(_claude_binding(cid) for cid in CAPABILITY_PROXY_CAPABILITY_IDS)


def uh_a0_home_legacy_tools() -> tuple[str, ...]:
    """Home capability names retained for Wake/legacy and forbidden in Daily."""
    return tuple(
        _provider_binding(cid, "home_mcp")
        for cid in INTERNAL_MCP_LEGACY_CAPABILITY_IDS
        if (get_capability(cid).get("provider_bindings") or {}).get("home_mcp")
    )


def uh_a0_home_compatibility_tools() -> tuple[str, ...]:
    """Home registrations retained only as non-UH-A0 compatibility seams."""
    return (_provider_binding("diary.write", "home_mcp"),)


def uh_a0_native_bindings() -> dict[str, str]:
    return {cid: _claude_binding(cid) for cid in NATIVE_FILE_CAPABILITY_IDS}


def uh_a0_external_read_tools() -> tuple[str, ...]:
    """Exact Claude Code built-ins for enabled External Read capabilities."""
    return tuple(_claude_binding(cid) for cid in EXTERNAL_READ_CAPABILITY_IDS)


def _resolve_mcp_url(server_name: str, default_url: str, legacy_mcp_config_path=None) -> str:
    path = Path(legacy_mcp_config_path) if legacy_mcp_config_path else None
    if path and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            server = (data.get("mcpServers") or {}).get(server_name) or {}
            url = str(server.get("url") or "").strip()
            if url:
                return url
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
    return default_url


def resolve_home_mcp_url(legacy_mcp_config_path: str | os.PathLike[str] | None = None) -> str:
    return _resolve_mcp_url("home", DEFAULT_HOME_MCP_URL, legacy_mcp_config_path)


def resolve_internal_mcp_url(legacy_mcp_config_path: str | os.PathLike[str] | None = None) -> str:
    return _resolve_mcp_url("internal", DEFAULT_INTERNAL_MCP_URL, legacy_mcp_config_path)


def _resolve_capability_proxy_db_path(
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the explicit SQLite path handed to the stdio capability proxy."""
    environ = env or os.environ
    configured = str(environ.get("TODO_INTERNAL_DB_PATH") or "").strip()
    if configured:
        return configured
    repo_root = str(
        environ.get("UH_A0_REPO_ROOT")
        or (Path(__file__).resolve().parent.parent)
    ).strip()
    return str(Path(repo_root) / "memories.db")


def _resolve_task_timer_commands_db_path(
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve the separate commands DB used only by task.timer.start."""
    environ = env or os.environ
    configured = str(environ.get("TASK_TIMER_COMMANDS_DB_PATH") or "").strip()
    if configured:
        return configured
    repo_root = str(
        environ.get("UH_A0_REPO_ROOT")
        or (Path(__file__).resolve().parent.parent)
    ).strip()
    return str(Path(repo_root) / "commands.db")


def build_uh_a0_mcp_config(
    *,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Strict UH-A0 config with Home compatibility and Internal Todo."""
    return {
        "mcpServers": {
            "home": {
                "type": "http",
                "url": resolve_home_mcp_url(legacy_mcp_config_path),
                "headers": {"X-UH-A0-Profile": "uh_a0"},
            },
            "internal": {
                "type": "http",
                "url": resolve_internal_mcp_url(legacy_mcp_config_path),
                "headers": {"X-UH-A0-Profile": "uh_a0"},
            },
            "capability": {
                "type": "stdio",
                "command": os.environ.get("UH_A0_NODE_COMMAND") or "node",
                "args": [str(Path(__file__).resolve().parent.parent / "capability-proxy-mcp-server.js")],
                "env": {
                    "UH_A0_REPO_ROOT": str(Path(__file__).resolve().parent.parent),
                    "TODO_INTERNAL_DB_PATH": _resolve_capability_proxy_db_path(env),
                    "TASK_TIMER_COMMANDS_DB_PATH": _resolve_task_timer_commands_db_path(env),
                },
            },
        }
    }

def write_uh_a0_mcp_config(
    cwd: str | os.PathLike[str],
    *,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    root = Path(cwd)
    out = root / UH_A0_MCP_CONFIG_FILENAME
    payload = build_uh_a0_mcp_config(
        legacy_mcp_config_path=legacy_mcp_config_path,
        env=env,
    )
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
    """capability_id -> loading_policy for the UH-A0 provider surface."""
    out: dict[str, str] = {}
    for cid in (
        HOME_MCP_CAPABILITY_IDS
        + INTERNAL_MCP_CAPABILITY_IDS
        + CAPABILITY_PROXY_CAPABILITY_IDS
        + NATIVE_FILE_CAPABILITY_IDS
    ):
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


def _surface_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Hash the complete visible and forbidden physical UH-A0 surface."""
    return sha256_canonical_json({
        "built_ins": list(snapshot["built_in_tools"]),
        "home_mcp": list(snapshot["home_mcp_tools"]),
        "internal_mcp": list(snapshot["internal_mcp_tools"]),
        "runtime_hidden_home_mcp": list(snapshot["runtime_hidden_home_mcp_tools"]),
        "runtime_hidden_internal_mcp": list(snapshot["runtime_hidden_internal_mcp_tools"]),
        "capability_proxy": list(snapshot["capability_proxy_tools"]),
        "forbidden_built_ins": list(FORBIDDEN_BUILTIN_TOOLS),
        "non_p3_home": list(NON_P3_HOME_MCP_TOOLS),
    })


def _surface_snapshot() -> dict[str, Any]:
    """Build one fail-closed, runtime-state-aware UH-A0 surface snapshot."""
    home_bindings = {cid: _claude_binding(cid) for cid in HOME_MCP_CAPABILITY_IDS}
    internal_bindings = {cid: _claude_binding(cid) for cid in INTERNAL_MCP_CAPABILITY_IDS}
    proxy_bindings = {cid: _claude_binding(cid) for cid in CAPABILITY_PROXY_CAPABILITY_IDS}
    legacy_home_tools = uh_a0_home_legacy_tools()
    compatibility_home_tools = uh_a0_home_compatibility_tools()
    native_file_bindings = {cid: _claude_binding(cid) for cid in NATIVE_FILE_CAPABILITY_IDS}
    external_bindings = {cid: _claude_binding(cid) for cid in EXTERNAL_READ_CAPABILITY_IDS}
    all_capability_ids = (
        HOME_MCP_CAPABILITY_IDS
        + INTERNAL_MCP_CAPABILITY_IDS
        + CAPABILITY_PROXY_CAPABILITY_IDS
        + NATIVE_FILE_CAPABILITY_IDS
        + EXTERNAL_READ_CAPABILITY_IDS
    )
    visible_states = {RUNTIME_STATE_INHERIT, RUNTIME_STATE_ON}
    try:
        states = {cid: read_capability_state(cid) for cid in all_capability_ids}
        if any(state not in {RUNTIME_STATE_INHERIT, RUNTIME_STATE_ON, RUNTIME_STATE_OFF, RUNTIME_STATE_DENY} for state in states.values()):
            raise CapabilityStateError("runtime capability state is invalid")
        status = "OK"
        diagnostic = ""
    except Exception as exc:
        states = {}
        status = "FAIL_CLOSED"
        diagnostic = str(exc)

    if status == "FAIL_CLOSED":
        visible_home_ids: tuple[str, ...] = ()
        visible_internal_ids: tuple[str, ...] = ()
        visible_proxy_ids: tuple[str, ...] = ()
        visible_native_file_ids: tuple[str, ...] = ()
        visible_external_ids: tuple[str, ...] = ()
        hidden_home = compatibility_home_tools + tuple(home_bindings.values()) + legacy_home_tools
        hidden_internal = INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS + tuple(internal_bindings.values())
        hidden_proxy = tuple(proxy_bindings.values())
    else:
        visible_home_ids = tuple(cid for cid in HOME_MCP_CAPABILITY_IDS if states[cid] in visible_states)
        visible_internal_ids = tuple(cid for cid in INTERNAL_MCP_CAPABILITY_IDS if states[cid] in visible_states)
        visible_proxy_ids = tuple(cid for cid in CAPABILITY_PROXY_CAPABILITY_IDS if states[cid] in visible_states)
        visible_native_file_ids = tuple(cid for cid in NATIVE_FILE_CAPABILITY_IDS if states[cid] in visible_states)
        visible_external_ids = tuple(cid for cid in EXTERNAL_READ_CAPABILITY_IDS if states[cid] in visible_states)
        hidden_home = compatibility_home_tools + legacy_home_tools + tuple(home_bindings[cid] for cid in HOME_MCP_CAPABILITY_IDS if states[cid] not in visible_states)
        hidden_internal = INTERNAL_MCP_SHADOW_DISALLOWED_TOOLS + tuple(internal_bindings[cid] for cid in INTERNAL_MCP_CAPABILITY_IDS if states[cid] not in visible_states)
        hidden_proxy = tuple(proxy_bindings[cid] for cid in CAPABILITY_PROXY_CAPABILITY_IDS if states[cid] not in visible_states)

    built_in_tools = tuple(native_file_bindings[cid] for cid in visible_native_file_ids) + tuple(external_bindings[cid] for cid in visible_external_ids)
    home_tools = tuple(home_bindings[cid] for cid in visible_home_ids)
    internal_tools = tuple(internal_bindings[cid] for cid in visible_internal_ids)
    proxy_tools = tuple(proxy_bindings[cid] for cid in visible_proxy_ids)
    return {
        "runtime_state_status": status,
        "runtime_state_diagnostic": diagnostic,
        "built_in_tools": built_in_tools,
        "home_mcp_tools": home_tools,
        "internal_mcp_tools": internal_tools,
        "capability_proxy_tools": proxy_tools,
        "native_bindings": {cid: native_file_bindings[cid] for cid in visible_native_file_ids},
        "runtime_hidden_home_mcp_tools": hidden_home,
        "runtime_hidden_internal_mcp_tools": hidden_internal,
        "runtime_hidden_capability_proxy_tools": hidden_proxy,
    }

def physical_surface_names() -> tuple[str, ...]:
    snapshot = _surface_snapshot()
    return (
        snapshot["built_in_tools"]
        + snapshot["home_mcp_tools"]
        + snapshot["internal_mcp_tools"]
        + snapshot["capability_proxy_tools"]
    )


def physical_surface_fingerprint() -> str:
    return _surface_fingerprint(_surface_snapshot())


def build_uh_a0_spawn_plan(
    *,
    cwd: str | os.PathLike[str] | None = None,
    legacy_mcp_config_path: str | os.PathLike[str] | None = None,
    turn_lease: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    actual_version: str | None = None,
    write_mcp_config: bool = True,
) -> dict[str, Any]:
    """Build a generation-stable UH-A0 plan with runtime-state HIDE.

    "turn_lease" is accepted and ignored for physical surface construction so
    lease contents cannot reshape the generation surface.
    """
    _ = turn_lease
    diagnosis = diagnose_tool_search(env=env, actual_version=actual_version)
    surface = _surface_snapshot()
    home_tools = surface["home_mcp_tools"]
    internal_tools = surface["internal_mcp_tools"]
    proxy_tools = surface["capability_proxy_tools"]
    native = surface["native_bindings"]
    loading = loading_plan_from_manifest()

    if diagnosis["tool_search_status"] == "ENVIRONMENT_BLOCKED":
        home_loading_mode = "blocked"
    elif diagnosis["tool_search_status"] == "AVAILABLE":
        home_loading_mode = "toolsearch_deferred"
    else:
        home_loading_mode = "preload_fallback"

    mcp_config = build_uh_a0_mcp_config(
        legacy_mcp_config_path=legacy_mcp_config_path,
        env=env,
    )
    target_cwd = Path(cwd or (Path(legacy_mcp_config_path).parent if legacy_mcp_config_path else Path.cwd()))
    mcp_path = ""
    if write_mcp_config:
        mcp_path = write_uh_a0_mcp_config(
            target_cwd,
            legacy_mcp_config_path=legacy_mcp_config_path,
            env=env,
        )
    settings_path = write_uh_a0_settings(target_cwd)
    turn_lease_path = resolve_uh_a0_turn_lease_path(target_cwd, env=env)

    built_in_tools = tuple(surface["built_in_tools"])
    allowlist = (
        list(built_in_tools)
        + list(home_tools)
        + list(internal_tools)
        + list(proxy_tools)
    )
    disallowed = (
        list(FORBIDDEN_BUILTIN_TOOLS)
        + list(NON_P3_HOME_MCP_TOOLS)
        + list(surface["runtime_hidden_home_mcp_tools"])
        + list(surface["runtime_hidden_internal_mcp_tools"])
        + list(surface["runtime_hidden_capability_proxy_tools"])
    )
    built_in_csv = ",".join(built_in_tools)
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
        "built_in_tools": built_in_tools,
        "built_in_tools_csv": built_in_csv,
        "home_mcp_tools": home_tools,
        "internal_mcp_tools": internal_tools,
        "capability_proxy_tools": proxy_tools,
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
        "physical_surface_fingerprint": _surface_fingerprint(surface),
        "loading_plan": loading,
        "memory_search_loading": loading["memory.search"],
        "home_loading_mode": home_loading_mode,
        "tool_search": diagnosis,
        "runtime_state_status": surface["runtime_state_status"],
        "runtime_state_diagnostic": surface["runtime_state_diagnostic"],
        "intent_instructions": short_intent_instructions(),
        # Visibility claim for reports: what Claude can see under UH-A0 plan.
        "claude_visible_built_ins": built_in_tools,
        "claude_visible_mcp_tools": home_tools + internal_tools + proxy_tools,
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

