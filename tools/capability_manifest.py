"""UH-A0 v1.0 provider-neutral capability manifest.

This module is the P1 product-semantics source of truth for tool capabilities.
It intentionally does *not* issue leases, expose tools to Claude Code, enforce
permissions, or implement provider adapters.  Those are later UH-A0 slices.

Frozen contract (2026-08-12):
- capability / intent live together in one manifest entry;
- capability_id is provider-neutral;
- loading_policy is visibility metadata, never authorization;
- P1 enabled vs reserved membership is kept outside entry fields so the
  manifest field names remain exactly the frozen contract fields.
"""
from __future__ import annotations

from typing import Any


CAPABILITY_FIELDS = (
    "capability_id",
    "display_name",
    "kind",
    "side_effect",
    "autonomy_mode",
    "trigger",
    "purpose",
    "deny_when",
    "failure_behavior",
    "loading_policy",
    "provider_bindings",
)

CAPABILITY_KINDS = frozenset({"read", "write", "execute"})
CAPABILITY_SIDE_EFFECTS = frozenset({"none", "external_state", "code_or_process"})
CAPABILITY_AUTONOMY_MODES = frozenset(
    {"read_auto", "explicit_or_ask", "task_only", "never_auto"}
)
CAPABILITY_LOADING_POLICIES = frozenset({"always_load", "deferred", "task_scoped"})


CAPABILITY_MANIFEST: tuple[dict[str, Any], ...] = (
    {
        "capability_id": "memory.search",
        "display_name": "搜索长期记忆",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖过去事实，且当前上下文不足以可靠确认时。",
        "purpose": "取得已经存在的真实记忆，避免凭印象补全。",
        "deny_when": "当前对话已经有充分事实，或只是为了展示工具能力时。",
        "failure_behavior": "明确说明未能查询或未找到，不得假装记得或查到。",
        "loading_policy": "always_load",
        "provider_bindings": {"claude_code": "mcp__home__search_memories"},
    },
    {
        "capability_id": "home.light.status",
        "display_name": "查看灯光状态",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖当前灯光真实状态，且状态未被可靠确认时。",
        "purpose": "先取得家庭设备真实状态，再决定如何回应。",
        "deny_when": "当前状态已明确，或与当前话题无关时。",
        "failure_behavior": "说明状态读取失败，不得猜测灯光当前状态。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__get_light_status"},
    },
    {
        "capability_id": "todo.read",
        "display_name": "查看待办",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖用户现有待办或承诺，且当前上下文不足时。",
        "purpose": "读取真实待办，避免遗漏或凭空补写事项。",
        "deny_when": "当前话题不需要待办事实，或已有充分上下文时。",
        "failure_behavior": "说明待办读取失败，不得声称看过未取得的数据。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__get_todos"},
    },
    {
        "capability_id": "todo.write",
        "display_name": "新增待办",
        "kind": "write",
        "side_effect": "external_state",
        "autonomy_mode": "explicit_or_ask",
        "trigger": "用户明确要求记录待办，或一个具体待办明显有帮助但尚未获授权时。",
        "purpose": "把用户确认的具体事项写入待办，而不是只口头承诺。",
        "deny_when": "只是讨论计划、没有明确写入意图且未取得 ASK 确认时。",
        "failure_behavior": "明确说明没有写入；不得说“记好了”或伪造成功。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__add_todo"},
    },
    {
        "capability_id": "countdown.read",
        "display_name": "查看倒计时",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖现有倒计时、日期节点或临近事件事实时。",
        "purpose": "取得真实倒计时状态，避免把时间节点说错。",
        "deny_when": "当前回答与倒计时无关，或事实已经明确时。",
        "failure_behavior": "说明倒计时读取失败，不得猜测存在的倒计时。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__get_countdowns"},
    },
    {
        "capability_id": "ledger.read",
        "display_name": "查看账本",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖已有账本记录、消费事实或历史金额时。",
        "purpose": "读取真实账本数据，避免凭记忆推测财务记录。",
        "deny_when": "当前问题不依赖账本事实，或用户已经给出足够数据时。",
        "failure_behavior": "说明账本读取失败，不得编造金额或记录。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__get_ledger"},
    },
    {
        "capability_id": "ledger.budget.read",
        "display_name": "查看预算",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖当前预算额度、预算状态或剩余额度时。",
        "purpose": "读取真实预算事实，而不是根据账目自行假设预算。",
        "deny_when": "当前话题不需要预算事实，或预算已经明确时。",
        "failure_behavior": "说明预算读取失败，不得伪造预算额度。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__get_ledger_budget"},
    },
    {
        "capability_id": "ledger.write",
        "display_name": "新增账目",
        "kind": "write",
        "side_effect": "external_state",
        "autonomy_mode": "explicit_or_ask",
        "trigger": "用户明确要求记账，或一个具体账目明显需要记录但尚未获授权时。",
        "purpose": "把用户确认的具体账目写入账本，而不是只在对话中复述。",
        "deny_when": "金额、用途或写入意图不明确，且没有完成 ASK 确认时。",
        "failure_behavior": "明确说明没有写入；不得说“已经记账”或伪造结果。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "mcp__home__add_ledger"},
    },
    {
        "capability_id": "files.read",
        "display_name": "读取文件",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "task_only",
        "trigger": "明确任务需要读取项目文件内容时。",
        "purpose": "直接查看任务所需文件，而不是根据文件名或记忆猜测。",
        "deny_when": "普通 Chat/Wake 没有明确 task_contract 时。",
        "failure_behavior": "报告无法读取的路径或原因，不得假装看过文件。",
        "loading_policy": "task_scoped",
        "provider_bindings": {"claude_code": "Read"},
    },
    {
        "capability_id": "files.find",
        "display_name": "查找文件",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "task_only",
        "trigger": "明确任务需要定位项目内文件或路径时。",
        "purpose": "在任务范围内找到真实文件位置，避免猜路径。",
        "deny_when": "普通 Chat/Wake 没有明确 task_contract 时。",
        "failure_behavior": "说明未找到或无法搜索，不得捏造路径。",
        "loading_policy": "task_scoped",
        "provider_bindings": {"claude_code": "Glob"},
    },
    {
        "capability_id": "code.search",
        "display_name": "搜索代码",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "task_only",
        "trigger": "明确任务需要查找代码符号、文本或引用位置时。",
        "purpose": "基于真实代码搜索结果定位实现，而不是凭架构印象推断。",
        "deny_when": "普通 Chat/Wake 没有明确 task_contract 时。",
        "failure_behavior": "说明搜索失败或无结果，不得声称代码中存在未验证内容。",
        "loading_policy": "task_scoped",
        "provider_bindings": {"claude_code": "Grep"},
    },
    {
        "capability_id": "web.search",
        "display_name": "网络搜索",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖当前外部事实，且本地上下文无法可靠确认时。",
        "purpose": "取得可核验的当前信息，而不是依赖可能过时的记忆。",
        "deny_when": "当前回答不需要外部实时事实时。",
        "failure_behavior": "说明搜索不可用或失败，不得假装已联网核验。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "WebSearch"},
    },
    {
        "capability_id": "web.read",
        "display_name": "读取网页",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖某个已知网页或在线文档的具体内容时。",
        "purpose": "读取来源原文，避免只根据搜索摘要或记忆回答。",
        "deny_when": "没有需要读取的具体在线来源时。",
        "failure_behavior": "说明网页读取失败，不得声称看过未取得的页面。",
        "loading_policy": "deferred",
        "provider_bindings": {"claude_code": "WebFetch"},
    },
    {
        "capability_id": "github.read",
        "display_name": "读取 GitHub",
        "kind": "read",
        "side_effect": "none",
        "autonomy_mode": "read_auto",
        "trigger": "回答依赖仓库、PR、Issue 或提交的真实状态时。",
        "purpose": "读取真实 GitHub 证据，而不是依据旧回报推断。",
        "deny_when": "P1 尚未启用该能力，或当前任务不依赖 GitHub 状态时。",
        "failure_behavior": "说明 GitHub 读取不可用或失败，不得伪造仓库状态。",
        "loading_policy": "deferred",
        "provider_bindings": {},
    },
    {
        "capability_id": "home.light.control",
        "display_name": "控制灯光",
        "kind": "write",
        "side_effect": "external_state",
        "autonomy_mode": "explicit_or_ask",
        "trigger": "用户明确要求改变灯光，或具体控制动作明显有帮助但尚未获授权时。",
        "purpose": "在得到当前动作授权后改变家庭灯光状态。",
        "deny_when": "P1 尚未启用该能力，或当前具体控制动作未获授权时。",
        "failure_behavior": "明确说明没有改变灯光，不得声称控制成功。",
        "loading_policy": "deferred",
        "provider_bindings": {},
    },
    {
        "capability_id": "code.write",
        "display_name": "修改代码",
        "kind": "write",
        "side_effect": "code_or_process",
        "autonomy_mode": "task_only",
        "trigger": "明确 task_contract 授权修改代码时。",
        "purpose": "在任务范围内执行受控代码修改。",
        "deny_when": "普通 Chat/Wake，或 task_contract 未显式授予 code.write 时。",
        "failure_behavior": "停止修改并报告失败；不得声称未实际写入的改动已完成。",
        "loading_policy": "task_scoped",
        "provider_bindings": {"claude_code": ("Edit", "Write")},
    },
    {
        "capability_id": "workspace.execute",
        "display_name": "Workspace 沙箱执行",
        "kind": "execute",
        "side_effect": "code_or_process",
        "autonomy_mode": "task_only",
        "trigger": "明确 task_contract 需要在隔离 Workspace 中执行程序或长任务时。",
        "purpose": "把需要执行的任务限制在沙箱边界内，而不是裸开放系统 shell。",
        "deny_when": "普通 Chat/Wake，或 task_contract 未显式授予 workspace.execute 时。",
        "failure_behavior": "停止执行并报告环境/执行错误，不得伪造运行结果。",
        "loading_policy": "task_scoped",
        "provider_bindings": {},
    },
)


P1_ENABLED_CAPABILITY_IDS = frozenset(
    {
        "memory.search",
        "home.light.status",
        "todo.read",
        "todo.write",
        "countdown.read",
        "ledger.read",
        "ledger.budget.read",
        "ledger.write",
        "files.read",
        "files.find",
        "code.search",
        "web.search",
        "web.read",
    }
)

P1_RESERVED_CAPABILITY_IDS = frozenset(
    {
        "github.read",
        "home.light.control",
        "code.write",
        "workspace.execute",
    }
)

_CAPABILITIES_BY_ID = {item["capability_id"]: item for item in CAPABILITY_MANIFEST}


def get_capability(capability_id: str) -> dict[str, Any] | None:
    """Return one manifest entry by provider-neutral capability_id."""
    return _CAPABILITIES_BY_ID.get(str(capability_id or ""))


def p1_enabled_capabilities() -> tuple[dict[str, Any], ...]:
    """Return P1-enabled entries in canonical manifest order."""
    return tuple(
        item for item in CAPABILITY_MANIFEST
        if item["capability_id"] in P1_ENABLED_CAPABILITY_IDS
    )

