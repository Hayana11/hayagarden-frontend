"""CC Wake tool surface: only MCP-backed wake tools are open.

Relay Wake keeps the full WAKE_TOOLS table. Claude Code Wake must not advertise
tools that have no MCP counterpart — and must never pretend it called them.
"""
from __future__ import annotations

from typing import Iterable, Sequence

# Logical wake tool name → Claude Code --allowedTools entry.
WAKE_TO_CC_MCP = {
    'search_memories': 'mcp__home__search_memories',
    'get_light_status': 'mcp__home__get_light_status',
    'get_todos': 'mcp__home__get_todos',
    'add_todo': 'mcp__home__add_todo',
    'get_countdowns': 'mcp__home__get_countdowns',
    'get_ledger': 'mcp__home__get_ledger',
    'add_ledger': 'mcp__home__add_ledger',
    'get_ledger_budget': 'mcp__home__get_ledger_budget',
}

# codebase_* read tools share one MCP server allow entry.
CODEBASE_PREFIX = 'codebase_'
CODEBASE_MCP = 'mcp__codebase'

# Optional atmosphere tools (no WAKE_TOOLS twin; open on CC Wake for nightwatch).
CC_WAKE_EXTRA_MCP = (
    'mcp__brain__breath',
    'mcp__brain__grow',
    'mcp__brain__hold',
    'mcp__brain__pulse',
    'mcp__brain__trace',
    'mcp__home__light_on',
    'mcp__home__light_off',
    'mcp__home__light_bedside_warm',
    'mcp__home__light_bedside_neutral',
)

# Soft nudge candidates that exist on the CC surface.
CC_WAKE_READONLY_HINTS = (
    'search_memories',
    'get_light_status',
    'get_todos',
    'get_countdowns',
    'get_ledger',
)


def is_cc_wake_tool(name: str) -> bool:
    name = str(name or '').strip()
    if not name:
        return False
    if name in WAKE_TO_CC_MCP:
        return True
    if name.startswith(CODEBASE_PREFIX):
        return True
    return False


def filter_wake_tools_for_cc(tools: Sequence[dict] | None) -> list[dict]:
    """Drop wake tools that have no CC MCP twin (schema kept for inspect_only)."""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if is_cc_wake_tool(tool.get('name', '')):
            out.append(tool)
    return out


def cc_wake_allowed_tools(tools: Sequence[dict] | None = None) -> str:
    """Build --allowedTools CSV for the independent CC Wake resident."""
    names = set(CC_WAKE_EXTRA_MCP)
    if tools is None:
        names.update(WAKE_TO_CC_MCP.values())
        names.add(CODEBASE_MCP)
    else:
        for tool in tools:
            name = str((tool or {}).get('name') or '')
            mcp = WAKE_TO_CC_MCP.get(name)
            if mcp:
                names.add(mcp)
            elif name.startswith(CODEBASE_PREFIX):
                names.add(CODEBASE_MCP)
    return ','.join(sorted(names))


def cc_wake_tool_names(tools: Sequence[dict] | None) -> list[str]:
    return [str(t.get('name')) for t in (tools or []) if t.get('name')]


def relay_only_tool_names(all_tool_names: Iterable[str]) -> list[str]:
    return sorted({n for n in all_tool_names if n and not is_cc_wake_tool(n)})


def cc_wake_nudge_text(t_hours: float, tool_names: Sequence[str] | None = None) -> str:
    """Prompt-side nudge; never demand tools that are not on the CC surface."""
    available = [n for n in (tool_names or CC_WAKE_READONLY_HINTS) if is_cc_wake_tool(n)]
    readonly = [n for n in available if n in CC_WAKE_READONLY_HINTS] or list(CC_WAKE_READONLY_HINTS)
    lines = [
        '【Wake 工具边界】',
        '你可用的工具只有 MCP 已接入的：' + '、'.join(readonly) + '，以及灯控/待办/记账/codebase 只读。',
        '没有位置、设备、留言板、相册、截图、联网搜索等工具——不要假装查过。',
    ]
    if float(t_hours or 0.0) >= 1.0:
        lines.append(
            '空闲已超过一小时：若心里不确定，可先调用一个只读工具了解现状，再决定 ACTION。'
            '不要无理由选 none，也不要编造工具结果。'
        )
    return '\n'.join(lines)
