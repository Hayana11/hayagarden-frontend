"""CC Wake tool surface: only MCP-backed wake tools are open.

Relay Wake keeps the full WAKE_TOOLS table. Claude Code Wake must not advertise
tools that have no MCP counterpart — and must never pretend it called them.

The capability text here is the single source of truth for CC Wake prompts;
``build_system(capability_profile='cc_wake')`` injects it and must not also
inject the generic Relay tool brochure.
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

CC_WAKE_READONLY_LOGICAL = (
    'search_memories',
    'get_light_status',
    'get_todos',
    'get_countdowns',
    'get_ledger',
    'get_ledger_budget',
)

CC_WAKE_WRITE_LOGICAL = (
    'add_todo',
    'add_ledger',
)

# Explicit writable MCP (no WAKE_TOOLS twin for light switches).
CC_WAKE_WRITE_MCP = (
    'mcp__home__light_on',
    'mcp__home__light_off',
    'mcp__home__light_bedside_warm',
    'mcp__home__light_bedside_neutral',
    'mcp__home__add_todo',
    'mcp__home__add_ledger',
)

CC_WAKE_READONLY_MCP = (
    'mcp__home__search_memories',
    'mcp__home__get_light_status',
    'mcp__home__get_todos',
    'mcp__home__get_countdowns',
    'mcp__home__get_ledger',
    'mcp__home__get_ledger_budget',
    CODEBASE_MCP,
)

# Single brochure for CC Wake — must match --allowedTools exactly.
CC_WAKE_CAPABILITY_TEXT = (
    '（Wake·Claude Code 工具面——仅此一份，以此为准。\n'
    '只读可用：search_memories、get_light_status、get_todos、get_countdowns、'
    'get_ledger、get_ledger_budget、codebase 只读'
    '（describe_project / read_file / list_directory / search_code / '
    'find_references / git_view / explain_history）。\n'
    '可写可用：light_on、light_off、light_bedside_warm、light_bedside_neutral、'
    'add_todo、add_ledger。\n'
    '不可用：位置、手机状态、留言板、联网搜索、GitHub、Playwright 读网页、'
    'Pocket、截图、相册、desire 工具、self_trigger、wake_settings、发文件/选择器。\n'
    '没有的工具不要假装调用过；若其它段落与本段冲突，以本段为准。）'
)

CC_WAKE_STABLE_NOTE = (
    '\n## Wake 说明\n'
    '这是自主唤醒检查，不是日常聊天窗口。'
    '工具权限以上一段「Wake·Claude Code 工具面」为准；'
    '不要假设留言板、联网或截图可用。\n'
)

# Soft nudge candidates (readonly only — never demand write tools).
CC_WAKE_READONLY_HINTS = CC_WAKE_READONLY_LOGICAL


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
    """Build --allowedTools CSV for the independent CC Wake resident.

    Always includes the fixed readonly + writable MCP set that matches
    ``CC_WAKE_CAPABILITY_TEXT``. Optional ``tools`` only further restricts
    logical WAKE_TOOLS; it never adds brain / board / web tools.
    """
    names = set(CC_WAKE_READONLY_MCP) | set(CC_WAKE_WRITE_MCP)
    if tools is not None:
        # Restrict to tools present in this run's filtered table (+ light writes).
        allowed_logical = {
            str((tool or {}).get('name') or '')
            for tool in tools
            if isinstance(tool, dict)
        }
        restricted = set(CC_WAKE_WRITE_MCP)  # light switches stay (no logical twin)
        for logical, mcp in WAKE_TO_CC_MCP.items():
            if logical in allowed_logical:
                restricted.add(mcp)
        if any(n.startswith(CODEBASE_PREFIX) for n in allowed_logical):
            restricted.add(CODEBASE_MCP)
        # Keep readonly MCP that map from allowed logical names.
        for logical in CC_WAKE_READONLY_LOGICAL:
            mcp = WAKE_TO_CC_MCP.get(logical)
            if mcp and logical in allowed_logical:
                restricted.add(mcp)
        names = restricted
    return ','.join(sorted(names))


def cc_wake_tool_names(tools: Sequence[dict] | None) -> list[str]:
    return [str(t.get('name')) for t in (tools or []) if t.get('name')]


def relay_only_tool_names(all_tool_names: Iterable[str]) -> list[str]:
    return sorted({n for n in all_tool_names if n and not is_cc_wake_tool(n)})


def cc_wake_nudge_text(
    t_hours: float,
    tool_names: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
) -> str:
    """Prompt-side nudge; must stay consistent with allowedTools."""
    if dry_run:
        return (
            '【dry_run】本次演习：工具已全部禁用，不要调用任何工具，'
            '也不要假装查过。只根据已给上下文思考，并输出 THOUGHTS/ACTION/CONTENT。'
        )
    readonly = [
        n for n in (tool_names or CC_WAKE_READONLY_HINTS)
        if n in CC_WAKE_READONLY_HINTS
    ] or list(CC_WAKE_READONLY_HINTS)
    lines = [
        '【Wake 工具边界】与上方「Wake·Claude Code 工具面」一致：',
        '只读：' + '、'.join(readonly) + '、codebase 只读。',
        '可写：灯控（on/off/bedside）、add_todo、add_ledger。',
        '不可用：位置、设备、留言板、相册、截图、联网搜索——不要假装查过。',
    ]
    if float(t_hours or 0.0) >= 1.0:
        lines.append(
            '空闲已超过一小时：若心里不确定，可先调用一个只读工具了解现状，再决定 ACTION。'
            '不要无理由选 none，也不要编造工具结果。'
        )
    return '\n'.join(lines)
