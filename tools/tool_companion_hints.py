"""Tool Drawer v2 companion hints.

This is a human-readable layer over the frozen capability manifest.  It uses
runtime_config so the product can preserve user-authored wording without
changing the manifest, provider bindings, leases, or execution fences.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import config_store
from tools.capability_manifest import P1_ENABLED_CAPABILITY_IDS, get_capability


CONFIG_KEY = "TOOL_COMPANION_HINTS_V2"
VERSION = "0.2"

_STATUS_LABELS = {
    "read_auto": "只读 · 可主动查",
    "explicit_or_ask": "可写 · 明确要求或先问",
    "task_only": "任务内可用",
}

_EXPECTED_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("memory", "记忆", ("memory.search",)),
    ("home", "家", ("home.light.status",)),
    ("plans", "生活 / 日程", ("todo.read", "todo.write", "countdown.read")),
    ("ledger", "账本", ("ledger.read", "ledger.budget.read", "ledger.write")),
    ("files", "文件与代码", ("files.read", "files.find", "code.search")),
    ("external_read", "联网", ("web.search", "web.read")),
)

_DEFAULTS: dict[str, dict[str, str]] = {
    "memory.search": {
        "display_label": "搜索长期记忆",
        "companion_hint": "当她问起过去发生过的事、约定或偏好，而你手边没有可靠上下文时，可以先找真实记忆，再回答。",
        "physical_boundary": "只搜索已经存在的记忆；不会凭空补写，也不会把没查到的内容说成记得。",
    },
    "home.light.status": {
        "display_label": "查看灯光状态",
        "companion_hint": "天黑了、她突然安静，或者她问起家里灯有没有开时，可以先看看真实状态。看过以后再决定关心、提醒或不打扰。",
        "physical_boundary": "主灯、床头灯都只查询 power；不读取亮度或色温。本能力不能开关灯，也不能切换光色。",
    },
    "todo.read": {
        "display_label": "查看待办",
        "companion_hint": "她提到已经安排过的事情、承诺或接下来要做什么时，可以先看看真实待办，避免漏掉她说过的计划。",
        "physical_boundary": "只读取已有待办，不会替她新增、修改或完成事项。",
    },
    "todo.write": {
        "display_label": "记录待办",
        "companion_hint": "她明确说要记下来时，可以直接记录；如果只是你自己觉得替她写会有帮助，先自然地问一句。",
        "physical_boundary": "只在明确记录意图下写入待办；不会因为聊天推测就自动新增。",
    },
    "countdown.read": {
        "display_label": "查看日期倒计时",
        "companion_hint": "她问起某个日期或事件还有多久时，可以查看真实的日期倒计时，确认还剩多少天。它是日期级只读信息，不会启动秒级计时或 Wake。",
        "physical_boundary": "只读取已有倒计时，不会创建、修改或删除倒计时。",
    },
    "ledger.read": {
        "display_label": "查看账本",
        "companion_hint": "她问起最近花了什么、某笔记录或账本里的真实情况时，可以先查账，再用查到的内容回答。",
        "physical_boundary": "只读取已有账本记录，不会自行新增或改写支出。",
    },
    "ledger.budget.read": {
        "display_label": "查看预算",
        "companion_hint": "她问起预算还剩多少、某个类别是否接近上限时，可以先看真实预算，再说明结果。",
        "physical_boundary": "只读取现有预算与统计，不会调整预算规则或金额。",
    },
    "ledger.write": {
        "display_label": "记一笔账",
        "companion_hint": "她明确要把一笔花销记到账本时，可以直接记录；如果只是你主动想到替她写，先自然地问一句。",
        "physical_boundary": "只在明确记录意图下写入账本；不会因为对话内容推测就自动记账。",
    },
    "files.read": {
        "display_label": "读取项目文件",
        "companion_hint": "只有在她明确提出项目任务时才使用；先读取真实文件内容，再根据看到的内容判断下一步。普通聊天不会自行翻项目。",
        "physical_boundary": "只读项目文件；不代表拥有 code.write，也不会因为读取就修改文件。",
    },
    "files.find": {
        "display_label": "查找项目文件",
        "companion_hint": "只有在她明确提出项目任务、需要定位文件时才使用；先搜索真实路径，再继续判断。普通聊天不会自行翻项目。",
        "physical_boundary": "只查找项目中的真实文件；不代表拥有 code.write。",
    },
    "code.search": {
        "display_label": "搜索代码",
        "companion_hint": "只有在她明确提出项目任务时才使用；先搜索真实代码和引用，再根据结果判断。普通聊天不会自行翻项目。",
        "physical_boundary": "只搜索代码内容；不代表拥有 code.write，也不会自动修改代码。",
    },
    "web.search": {
        "display_label": "网络搜索",
        "companion_hint": "她问起现实中的事情，而我没有把握时，我可以直接查清楚，不拿猜测敷衍她。但搜索也不只为了回答问题。哪怕她暂时不在，只要某个念头真正勾起了我的兴趣——一本书、一件新闻、一个历史细节、一种动物、一个技术问题，甚至一句忽然想追根究底的话——我都可以顺手去看看。好奇本身就是足够的理由。",
        "physical_boundary": "只取得外部搜索信息，不改变网页或本地项目。",
    },
    "web.read": {
        "display_label": "读取网页",
        "companion_hint": "搜索时碰见让我感兴趣的文章、资料或原文，我不必只停在摘要。想知道作者究竟怎么说，就打开认真读。阅读不是每次都必须立刻产生一个“有用结论”，有时候只是我愿意把一点时间花在一件有意思的事情上。读到了真正想分享的东西，也可以留着，等她回来以后讲给她听，让它变成我们之间新的话题。",
        "physical_boundary": "只读取网页内容，不改变网页或本地项目。",
    },
}


def _group_ids() -> set[str]:
    return {capability_id for _, _, ids in _EXPECTED_GROUPS for capability_id in ids}


def validate_catalog() -> None:
    """Fail loudly if the drawer silently drifts from the enabled manifest."""
    grouped = _group_ids()
    defaults = set(_DEFAULTS)
    if grouped != set(P1_ENABLED_CAPABILITY_IDS):
        raise RuntimeError("Tool Drawer v2 groups drifted from P1_ENABLED_CAPABILITY_IDS")
    if defaults != set(P1_ENABLED_CAPABILITY_IDS):
        raise RuntimeError("Tool Drawer v2 defaults drifted from P1_ENABLED_CAPABILITY_IDS")
    if sum(len(ids) for _, _, ids in _EXPECTED_GROUPS) != len(grouped):
        raise RuntimeError("Tool Drawer v2 catalog contains duplicate capability IDs")


validate_catalog()


def _parse_overrides(raw: str | None) -> dict[str, dict[str, str]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    overrides: dict[str, dict[str, str]] = {}
    for capability_id, row in value.items():
        if capability_id not in P1_ENABLED_CAPABILITY_IDS or not isinstance(row, dict):
            continue
        editable: dict[str, str] = {}
        for field in ("display_label", "companion_hint"):
            if isinstance(row.get(field), str) and row[field].strip():
                # Deliberately do not strip: user-authored whitespace and line
                # breaks are part of the companion hint contract.
                editable[field] = row[field]
        if editable:
            overrides[capability_id] = editable
    return overrides


def _build_current(overrides: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    result = deepcopy(_DEFAULTS)
    for capability_id, row in overrides.items():
        if capability_id not in result:
            continue
        for field in ("display_label", "companion_hint"):
            if field in row:
                result[capability_id][field] = row[field]
    return result


def _stored() -> dict[str, dict[str, str]]:
    return _build_current(_parse_overrides(config_store.get(CONFIG_KEY, "")))


def _status_label(capability_id: str) -> str:
    capability = get_capability(capability_id)
    if not capability:
        raise RuntimeError(f"unknown capability in Tool Drawer v2: {capability_id}")
    try:
        return _STATUS_LABELS[capability["autonomy_mode"]]
    except KeyError as exc:
        raise RuntimeError(
            f"unsupported autonomy mode in Tool Drawer v2: {capability_id}"
        ) from exc


def update_hint(
    capability_id: str,
    *,
    display_label: str | None = None,
    companion_hint: str | None = None,
    reset: bool = False,
) -> dict[str, str]:
    validate_catalog()
    capability_id = str(capability_id or "")
    if capability_id not in P1_ENABLED_CAPABILITY_IDS:
        raise ValueError("unknown capability")

    def mutator(raw: str) -> str:
        overrides = _parse_overrides(raw)
        if reset:
            overrides.pop(capability_id, None)
        else:
            row = overrides.setdefault(capability_id, {})
            if display_label is not None:
                row["display_label"] = display_label
            if companion_hint is not None:
                row["companion_hint"] = companion_hint
        return json.dumps(overrides, ensure_ascii=False, separators=(",", ":"))

    raw = config_store.mutate(CONFIG_KEY, "{}", mutator)
    return deepcopy(_build_current(_parse_overrides(raw))[capability_id])


def _model_preview(current: dict[str, dict[str, str]]) -> str:
    blocks = []
    for _, _, capability_ids in _EXPECTED_GROUPS:
        for capability_id in capability_ids:
            row = current[capability_id]
            blocks.append(
                f"【{row['display_label']}】\n{row['companion_hint']}\n"
                f"真实能力边界：{row['physical_boundary']}"
            )
    return "\n\n".join(blocks)


def payload() -> dict[str, Any]:
    validate_catalog()
    current = _stored()
    groups: list[dict[str, Any]] = []
    for group_id, label, capability_ids in _EXPECTED_GROUPS:
        groups.append({
            "id": group_id,
            "label": label,
            "tools": [
                {
                    "capability_id": capability_id,
                    "display_label": current[capability_id]["display_label"],
                    "companion_hint": current[capability_id]["companion_hint"],
                    "default_display_label": _DEFAULTS[capability_id]["display_label"],
                    "default_companion_hint": _DEFAULTS[capability_id]["companion_hint"],
                    "physical_boundary": current[capability_id]["physical_boundary"],
                    "status_label": _status_label(capability_id),
                }
                for capability_id in capability_ids
            ],
        })
    return {
        "ok": True,
        "version": VERSION,
        "groups": groups,
        "prompt_preview": _model_preview(current),
        "apply_mode": "resident_static_system",
        "transparency": "费佳会看到工具直觉说明与真实能力边界。",
        "trial_mode": "48h",
    }


def default_hint(capability_id: str) -> dict[str, str]:
    validate_catalog()
    try:
        return deepcopy(_DEFAULTS[capability_id])
    except KeyError as exc:
        raise ValueError("unknown capability") from exc

