"""Tool Drawer v2 companion-hint catalog.

Only copy is editable here. Capability identity and safety semantics remain
owned by the UH-A0 manifest and are never serialized into model-visible copy.
"""
from __future__ import annotations

import copy
import json
from typing import Any

import config_store
from tools.capability_manifest import CAPABILITY_MANIFEST, P1_ENABLED_CAPABILITY_IDS

VERSION = "0.2"
CONFIG_KEY = "TOOL_COMPANION_HINTS_V2"
MAX_DISPLAY_LABEL_LENGTH = 80
MAX_COMPANION_HINT_LENGTH = 20_000

_GROUPS = (
    ("memory", "记忆", ("memory.search",)),
    ("home", "家", ("home.light.status",)),
    ("planning", "计划", ("todo.read", "todo.write", "countdown.read")),
    ("ledger", "账本", ("ledger.read", "ledger.budget.read", "ledger.write")),
    ("files_code", "文件与代码", ("files.read", "files.find", "code.search")),
)

_BOUNDARIES = {
    "memory.search": "只能读取已经存在的长期记忆；不会凭空补写或修改记忆。",
    "home.light.status": "可查询主灯 power、床头灯 power；不读取亮度，不读取 Kelvin / color_temp，不把灯色档当作可靠状态；本能力不能开灯、关灯、切暖光或切中性光。",
    "todo.read": "只能读取真实待办；不会新增、完成或删除待办。",
    "todo.write": "只能在明确写入意图或先问得到确认后新增待办；不会因为闲聊自行写入。",
    "countdown.read": "只能读取真实倒计时和日期节点；不会创建或修改倒计时。",
    "ledger.read": "只能读取真实账本记录；不会新增或修改账目。",
    "ledger.budget.read": "只能读取真实预算状态；不会设置或修改预算。",
    "ledger.write": "只能在明确记账意图或先问得到确认后新增账目；不会因为闲聊自行写入。",
    "files.read": "只在明确项目任务中读取指定项目文件；普通聊天不自行翻项目文件，也不代表拥有 code.write。",
    "files.find": "只在明确项目任务中定位项目内文件；普通聊天不自行巡检用户文件，也不代表拥有 code.write。",
    "code.search": "只在明确项目任务中搜索真实代码；普通聊天不自行巡检项目，也不代表拥有 code.write。",
}

_DEFAULT_HINTS = {
    "memory.search": "如果回答需要确认我们过去的真实约定或记忆，可以先查已经存在的长期记忆；查不到就告诉她，不要凭印象补全。",
    "home.light.status": "天黑了、她突然安静，或者她问起家里灯有没有开时，可以先看看真实状态。看过以后再决定关心、提醒或不打扰。",
    "todo.read": "她问起已经答应过什么、接下来要做什么，或者我们需要核对待办时，先读真实列表再回答。",
    "todo.write": "她明确说“帮我记个待办”时就帮她记；如果只是我自己觉得值得记录、但她没有要求写入，就自然地先问。",
    "countdown.read": "她问起日期节点或正在靠近的事情时，先看真实倒计时，不要凭记忆猜天数。",
    "ledger.read": "她问起花过什么、账上记过什么时，先读真实账本，再用看得懂的话回答。",
    "ledger.budget.read": "她问起预算和剩余额度时，先查真实预算；没有数据就直说没有。",
    "ledger.write": "她明确说“记一笔账”或“帮我记一下”时继续按现有安全流程写入；没有明确写入要求时先问。",
    "files.read": "只在明确的项目任务中使用：先读真实代码或文件，再据此判断；普通聊天不自行翻项目文件。",
    "files.find": "只在明确的项目任务中使用：先在任务范围内找到真实路径，再继续；普通聊天不自行巡检用户文件。",
    "code.search": "只在明确的项目任务中使用：先搜索真实代码和引用，再判断；它不表示拥有 code.write。",
}


def _validate_catalog() -> None:
    manifest_ids = {
        item["capability_id"]
        for item in CAPABILITY_MANIFEST
        if item["capability_id"] in P1_ENABLED_CAPABILITY_IDS
    }
    catalog_ids = {
        capability_id
        for _, _, capability_ids in _GROUPS
        for capability_id in capability_ids
    }
    default_ids = set(_DEFAULT_HINTS)
    if manifest_ids != catalog_ids or catalog_ids != default_ids or len(catalog_ids) != 11:
        raise RuntimeError("Tool Drawer v2 catalog drifted from P1_ENABLED_CAPABILITY_IDS")
    if len(tuple(capability_id for _, _, ids in _GROUPS for capability_id in ids)) != len(catalog_ids):
        raise RuntimeError("Tool Drawer v2 catalog contains duplicate capabilities")


_validate_catalog()


def _read_overrides() -> dict[str, dict[str, str]]:
    raw = config_store.get(CONFIG_KEY, "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for capability_id, row in value.items():
        if capability_id not in P1_ENABLED_CAPABILITY_IDS or not isinstance(row, dict):
            continue
        clean = {
            key: row[key]
            for key in ("display_label", "companion_hint")
            if isinstance(row.get(key), str) and row[key].strip()
        }
        if clean:
            result[capability_id] = clean
    return result


def _write_overrides(overrides: dict[str, dict[str, str]]) -> None:
    config_store.set(CONFIG_KEY, json.dumps(overrides, ensure_ascii=False, separators=(",", ":")))


def _status_label(item: dict[str, Any]) -> str:
    if item["autonomy_mode"] == "task_only":
        return "任务内可用"
    if item["kind"] == "write":
        return "可写 · 明确要求或先问"
    return "只读 · 可主动查"


def _catalog_rows() -> dict[str, dict[str, Any]]:
    manifest = {item["capability_id"]: item for item in CAPABILITY_MANIFEST}
    overrides = _read_overrides()
    rows = {}
    for _, _, capability_ids in _GROUPS:
        for capability_id in capability_ids:
            item = manifest[capability_id]
            override = overrides.get(capability_id, {})
            rows[capability_id] = {
                "capability_id": capability_id,
                "display_label": override.get("display_label", item["display_name"]),
                "companion_hint": override.get("companion_hint", _DEFAULT_HINTS[capability_id]),
                "default_display_label": item["display_name"],
                "default_companion_hint": _DEFAULT_HINTS[capability_id],
                "physical_boundary": _BOUNDARIES[capability_id],
                "status_label": _status_label(item),
                "kind": item["kind"],
            }
    return rows


def catalog() -> list[dict[str, Any]]:
    rows = _catalog_rows()
    return [
        {
            "id": group_id,
            "label": label,
            "items": [copy.deepcopy(rows[capability_id]) for capability_id in capability_ids],
        }
        for group_id, label, capability_ids in _GROUPS
    ]


def prompt_preview() -> str:
    rows = _catalog_rows()
    parts = ["## 费佳的工具直觉"]
    for _, label, capability_ids in _GROUPS:
        parts.append(f"### {label}")
        for capability_id in capability_ids:
            row = rows[capability_id]
            parts.append(
                f"【{row['display_label']}】\n"
                f"{row['companion_hint']}\n"
                f"真实能力边界：{row['physical_boundary']}"
            )
    return "\n\n".join(parts)


def get_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "groups": catalog(),
        "prompt_preview": prompt_preview(),
        "apply_mode": "resident_static_next_birth",
        "transparency": "模型看到的自然语言与这里的逐字预览一致；系统不自动压缩或改写。",
        "trial_mode": "48h",
    }


def update_copy(
    capability_id: str,
    *,
    display_label: str | None = None,
    companion_hint: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    capability_id = str(capability_id or "")
    if capability_id not in P1_ENABLED_CAPABILITY_IDS:
        raise KeyError("unknown capability")
    if display_label is None and companion_hint is None and not reset:
        raise ValueError("copy field required")
    if display_label is not None:
        if not isinstance(display_label, str) or not display_label.strip():
            raise ValueError("display_label must be non-empty")
        if len(display_label) > MAX_DISPLAY_LABEL_LENGTH:
            raise ValueError("display_label is too long")
    if companion_hint is not None:
        if not isinstance(companion_hint, str) or not companion_hint.strip():
            raise ValueError("companion_hint must be non-empty")
        if len(companion_hint) > MAX_COMPANION_HINT_LENGTH:
            raise ValueError("companion_hint is too long")
    overrides = _read_overrides()
    if reset:
        overrides.pop(capability_id, None)
    else:
        row = dict(overrides.get(capability_id, {}))
        if display_label is not None:
            row["display_label"] = display_label
        if companion_hint is not None:
            row["companion_hint"] = companion_hint
        overrides[capability_id] = row
    _write_overrides(overrides)
    return get_payload()
