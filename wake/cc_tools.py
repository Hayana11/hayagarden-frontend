"""CC Wake prompt brochures for inspect_only / dry_run system assembly.

Live Claude Wake provider rounds for morning/nightwatch/ritual/self_trigger
are retired. Canonical normal Wake uses Unified/_CC_RESIDENT natural chat
output, not this tool-compat surface. These strings remain only because
``build_system(capability_profile='cc_wake'|'wake_dry_run')`` still injects
them for inspect_only and dry_run prompt assembly.
"""
from __future__ import annotations

# Single brochure for CC Wake inspect/dry-run prompts.
CC_WAKE_CAPABILITY_TEXT = (
    '（Wake·Claude Code 工具面——仅此一份，以此为准。\n'
    '只读可用：search_memories、get_light_status、get_todos、get_countdowns、'
    'get_ledger、get_ledger_budget、以及 codebase 只读工具'
    '（mcp__codebase__describe_project / read_file / list_directory / search_code / '
    'find_references / git_view）。\n'
    '可写可用：add_todo、add_ledger。\n'
    '灯：仅只读 get_light_status；Wake 不得调用 light_on/light_off 或床头灯模式切换。\n'
    '不可用：codebase patch/create_file、codebase explain_history（内部会打中转站）、'
    '位置、手机状态、留言板、联网搜索、GitHub、Playwright 读网页、Pocket、截图、相册、'
    'desire 工具、self_trigger、wake_settings、发文件/选择器。\n'
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
