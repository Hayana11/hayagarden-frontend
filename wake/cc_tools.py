"""Inspect/dry-run capability brochures for Claude Wake.

Canonical normal Wake is Unified / hot ``_CC_RESIDENT``.
morning/nightwatch/ritual/self_trigger are disabled. These strings remain
only because inspect_only / dry_run still assemble a brochure via
``build_system(capability_profile='cc_wake'|'wake_dry_run')``.
"""
from __future__ import annotations

# Inspect brochure: describe the current route, not a retired runner.
CC_WAKE_CAPABILITY_TEXT = (
    '（Wake·Claude inspect——仅此一份，以此为准。\n'
    'canonical normal Wake：现有 Unified 热 _CC_RESIDENT 自然聊天；'
    '可执行 action 由 B2 none 与 B3 message 的 route/executor 所有权决定，'
    '不是独立 Claude Wake runner。\n'
    'morning / nightwatch / ritual / self_trigger：disabled，'
    '没有 production Action，没有 provider 调用。\n'
    'inspect_only 只描述当前路线，不调用模型、不写库。\n'
    '灯：仅只读 get_light_status；不得调用 light_on/light_off 或床头灯模式切换。\n'
    '不可用：codebase patch/create_file、codebase explain_history、'
    '位置、留言板、联网搜索、GitHub、Playwright、Pocket、截图、相册写、'
    '发文件/选择器。\n'
    '若其它段落与本段冲突，以本段为准。）'
)

# dry_run: must NOT inject the normal tool brochure at all.
WAKE_DRY_RUN_CAPABILITY_TEXT = (
    '（Wake 演习模式 dry_run——仅此一份，以此为准。\n'
    '本轮无任何工具；所有外部动作均不可用。\n'
    '不要调用工具，不要假装查过。不写库、不改灯/待办/账本。\n'
    'disabled 模式仍然 disabled；canonical normal 仍是 Unified 热 resident 路线。）'
)

CC_WAKE_STABLE_NOTE = (
    '\n## Wake 说明\n'
    '这是 inspect/dry-run 路线说明，不是独立 Claude Wake runner。'
    'canonical normal 走 Unified 热 _CC_RESIDENT；'
    'morning/nightwatch/ritual/self_trigger 为 disabled。\n'
)

WAKE_DRY_RUN_STABLE_NOTE = (
    '\n## Wake 演习说明\n'
    '本轮是 dry_run：无工具、不写库、不改灯/待办/账本。'
    'disabled 模式仍然 disabled。\n'
)
