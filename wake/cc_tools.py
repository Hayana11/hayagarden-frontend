"""CC Wake tool surface: only MCP-backed wake tools are open.

Relay Wake keeps the full WAKE_TOOLS table. Claude Code Wake must not advertise
tools that have no MCP counterpart — and must never pretend it called them.

The capability text here is the single source of truth for CC Wake prompts;
``build_system(capability_profile='cc_wake')`` injects it and must not also
inject the generic Relay tool brochure.

IMPORTANT: never use bare ``mcp__codebase`` — that opens the whole server,
including patch/create_file. Enumerate read-only tool full names instead.
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

CODEBASE_PREFIX = 'codebase_'

# Per-tool MCP names (NOT bare mcp__codebase — that would include patch/create_file).
CODEBASE_READ_MCP = (
    'mcp__codebase__describe_project',
    'mcp__codebase__read_file',
    'mcp__codebase__list_directory',
    'mcp__codebase__search_code',
    'mcp__codebase__find_references',
    'mcp__codebase__git_view',
    'mcp__codebase__explain_history',
)

CODEBASE_WRITE_MCP = (
    'mcp__codebase__patch',
    'mcp__codebase__create_file',
)

CODEBASE_READ_LOGICAL = frozenset((
    'codebase_describe_project',
    'codebase_read_file',
    'codebase_list_directory',
    'codebase_search_code',
    'codebase_find_references',
    'codebase_git_view',
    'codebase_explain_history',
))

CC_WAKE_READONLY_LOGICAL = (
    'search_memories',
    'get_light_status',
    'get_todos',
    'get_countdowns',
    'get_ledger',
    'get_ledger_budget',
) + tuple(sorted(CODEBASE_READ_LOGICAL))

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
) + CODEBASE_READ_MCP

# Single brochure for CC Wake — must match --allowedTools exactly.
CC_WAKE_CAPABILITY_TEXT = (
    '（Wake·Claude Code 工具面——仅此一份，以此为准。\n'
    '只读可用：search_memories、get_light_status、get_todos、get_countdowns、'
    'get_ledger、get_ledger_budget、以及 codebase 只读工具'
    '（mcp__codebase__describe_project / read_file / list_directory / search_code / '
    'find_references / git_view / explain_history）。\n'
    '可写可用：light_on、light_off、light_bedside_warm、light_bedside_neutral、'
    'add_todo、add_ledger。\n'
    '不可用：codebase patch/create_file、位置、手机状态、留言板、联网搜索、GitHub、'
    'Playwright 读网页、Pocket、截图、相册、desire 工具、self_trigger、wake_settings、'
    '发文件/选择器。\n'
    '没有的工具不要假装调用过；若其它段落与本段冲突，以本段为准。）'
)

# dry_run: must NOT inject the normal tool brochure at all.
WAKE_DRY_RUN_CAPABILITY_TEXT = (
    '（Wake 演习模式 dry_run——仅此一份，以此为准。\n'
    '本轮无任何工具；所有外部动作均不可用。\n'
    '不要调用工具，不要假装查过。只根据已给上下文输出 THOUGHTS/ACTION/CONTENT。）'
)

CC_WAKE_STABLE_NOTE = (
    '\n## Wake 说明\n'
    '这是自主唤醒检查，不是日常聊天窗口。'
    '工具权限以上一段「Wake·Claude Code 工具面」为准；'
    '不要假设留言板、联网或截图可用。\n'
)

WAKE_DRY_RUN_STABLE_NOTE = (
    '\n## Wake 演习说明\n'
    '本轮是 dry_run：无工具、不写库、不改灯/待办/账本。'
    '只输出结构化决策。\n'
)

# Soft nudge candidates (readonly home tools — never demand write tools).
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
    if name in CODEBASE_READ_LOGICAL:
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

    Never includes bare ``mcp__codebase`` or patch/create_file.
    """
    names = set(CC_WAKE_READONLY_MCP) | set(CC_WAKE_WRITE_MCP)
    if tools is not None:
        allowed_logical = {
            str((tool or {}).get('name') or '')
            for tool in tools
            if isinstance(tool, dict)
        }
        restricted = set(CC_WAKE_WRITE_MCP)  # light switches stay (no logical twin)
        for logical, mcp in WAKE_TO_CC_MCP.items():
            if logical in allowed_logical:
                restricted.add(mcp)
        if allowed_logical & CODEBASE_READ_LOGICAL:
            restricted.update(CODEBASE_READ_MCP)
        for logical in (
            'search_memories', 'get_light_status', 'get_todos', 'get_countdowns',
            'get_ledger', 'get_ledger_budget',
        ):
            mcp = WAKE_TO_CC_MCP.get(logical)
            if mcp and logical in allowed_logical:
                restricted.add(mcp)
        names = restricted
    # Hard deny write tools even if a caller somehow asked.
    names -= set(CODEBASE_WRITE_MCP)
    names.discard('mcp__codebase')
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
        # Capability profile already says no tools — keep a short reminder only.
        return '【dry_run】无工具。只输出 THOUGHTS/ACTION/CONTENT。'
    readonly = [
        n for n in (tool_names or CC_WAKE_READONLY_HINTS)
        if n in CC_WAKE_READONLY_HINTS
    ] or list(CC_WAKE_READONLY_HINTS)
    lines = [
        '【Wake 工具边界】与上方「Wake·Claude Code 工具面」一致：',
        '只读：' + '、'.join(readonly) + '、codebase 只读（不含 patch/create_file）。',
        '可写：灯控（on/off/bedside）、add_todo、add_ledger。',
        '不可用：位置、设备、留言板、相册、截图、联网搜索——不要假装查过。',
    ]
    if float(t_hours or 0.0) >= 1.0:
        lines.append(
            '空闲已超过一小时：若心里不确定，可先调用一个只读工具了解现状，再决定 ACTION。'
            '不要无理由选 none，也不要编造工具结果。'
        )
    return '\n'.join(lines)
