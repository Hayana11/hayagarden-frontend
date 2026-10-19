"""Metadata for the pinned upstream Ombre native Claude Code surface.

This module contains only the upstream-owned identity and tool names observed
from the pinned public MCP endpoint. It does not implement Ombre behavior or
translate the tools into Hayagarden capabilities.
"""

UPSTREAM_REPOSITORY = "P0luz/Ombre-Brain"
UPSTREAM_VERSION = "3.6.14"
UPSTREAM_SHA = "115d831d979bf67efc7150f976e2cb34c333c935"

UPSTREAM_BRAIN_SERVER = "brain"
UPSTREAM_BRAIN_ALLOW_RULE = "mcp__brain__*"

# Exact raw names returned by tools/list at UPSTREAM_SHA, in server order.
UPSTREAM_BRAIN_TOOL_NAMES: tuple[str, ...] = (
    "breath",
    "breath_search",
    "breath_advanced",
    "hold",
    "grow",
    "trace",
    "dream",
    "anchor",
    "release",
    "pulse",
    "plan",
    "letter_write",
    "letter_lock_update",
    "letter_read",
    "feel",
    "I",
)

UPSTREAM_BRAIN_TOOL_BINDINGS: tuple[str, ...] = tuple(
    f"mcp__{UPSTREAM_BRAIN_SERVER}__{name}"
    for name in UPSTREAM_BRAIN_TOOL_NAMES
)

OFFICIAL_PROMPT_PATH = "/opt/ombre-next/docs/CLAUDE_PROMPT.md"
OFFICIAL_HOOK_PATH = "/opt/ombre-next/.claude/hooks/session_breath.py"
