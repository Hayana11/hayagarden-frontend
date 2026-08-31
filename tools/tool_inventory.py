"""Read-only historical Gateway tool inventory; never imports gateway.py."""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any
GROUP_DEFS=[["memory","记忆",["save_memory","search_memories","write_diary"]],["web","联网",["web_search","browse_github","read_webpage"]],["pocket","Pocket 手机浏览器",["pocket_status","pocket_goto","pocket_js","pocket_html","pocket_screenshot"]],["light","灯",["light_on","light_off","light_warm","light_neutral","set_brightness","set_color_temp","get_light_status"]],["shopping","购物",["shop_browse","shop_act","shop_checkout","shop_login_start","shop_login_status"]],["gallery","相册/截图",["save_to_gallery","recall_photo","screenshot_chat"]],["code_files","代码/文件",["read_backend_file","search_files","read_frontend_file","write_frontend_file","str_replace_frontend_file","check_page_render","codebase_describe_project","codebase_read_file","codebase_list_directory","codebase_search_code","codebase_find_references","codebase_patch","codebase_create_file","codebase_git_view","codebase_explain_history"]],["workspace","Workspace",["shell_exec","ws_job","ws_ls","ws_read","ws_write","ws_edit","ws_patch","ws_diff","mcp_search","mcp_load","mcp_call","workspace_app"]],["self_config","自我配置",["read_bot_config","edit_bot_config","get_wake_settings","set_wake_settings"]],["board","留言板",["read_board","post_to_board","reply_to_board","block_user"]],["life","生活状态",["get_activity_summary","log_period_event","get_location","get_device_status","request_phone_screenshot"]],["plans_ledger","计划与账本",["get_todos","add_todo","get_countdowns","get_ledger","add_ledger","get_ledger_budget"]],["desire","欲望账本",["desire_add","desire_list","desire_act","desire_reflect","desire_history"]],["triggers","自主触发",["set_self_trigger","cancel_self_trigger"]],["artifacts","产物生成",["create_html","create_markdown","create_document"]],["phone","手机指令",["issue_command"]],["moments","朋友圈",["collect_chat_moment"]]]

DISPLAY_LABELS = {
  "save_memory": "保存长期记忆",
  "search_memories": "搜索长期记忆",
  "write_diary": "记日记",
  "web_search": "搜索网页",
  "browse_github": "浏览 GitHub",
  "read_webpage": "读取网页",
  "pocket_status": "查看 Pocket 状态",
  "pocket_goto": "打开 Pocket 页面",
  "pocket_js": "执行 Pocket 脚本",
  "pocket_html": "读取 Pocket 页面",
  "pocket_screenshot": "截取 Pocket 页面",
  "light_on": "开灯",
  "light_off": "关灯",
  "light_warm": "切换暖光",
  "light_neutral": "切换中性光",
  "set_brightness": "设置亮度",
  "set_color_temp": "设置色温",
  "get_light_status": "查看灯光状态",
  "shop_browse": "浏览商品",
  "shop_act": "执行购物操作",
  "shop_checkout": "结算购物车",
  "shop_login_start": "开始购物登录",
  "shop_login_status": "查看购物登录状态",
  "save_to_gallery": "保存到相册",
  "recall_photo": "查找相册照片",
  "screenshot_chat": "截取聊天画面",
  "read_backend_file": "读取后端文件",
  "search_files": "搜索项目文件",
  "read_frontend_file": "读取前端文件",
  "write_frontend_file": "写入前端文件",
  "str_replace_frontend_file": "替换前端代码",
  "check_page_render": "检查页面渲染",
  "codebase_describe_project": "读取项目架构",
  "codebase_read_file": "读取项目文件",
  "codebase_list_directory": "查找项目文件",
  "codebase_search_code": "搜索代码",
  "codebase_find_references": "查找代码引用",
  "codebase_patch": "修改项目文件",
  "codebase_create_file": "创建项目文件",
  "codebase_git_view": "查看 Git 历史",
  "codebase_explain_history": "解释代码历史",
  "shell_exec": "执行工作区命令",
  "ws_job": "管理工作区任务",
  "ws_ls": "列出工作区文件",
  "ws_read": "读取工作区文件",
  "ws_write": "写入工作区文件",
  "ws_edit": "编辑工作区文件",
  "ws_patch": "修改工作区文件",
  "ws_diff": "查看工作区差异",
  "mcp_search": "搜索 MCP 工具",
  "mcp_load": "加载 MCP 工具",
  "mcp_call": "调用 MCP 工具",
  "workspace_app": "管理工作区应用",
  "read_bot_config": "查看机器人配置",
  "edit_bot_config": "编辑机器人配置",
  "get_wake_settings": "查看 Wake 设置",
  "set_wake_settings": "修改 Wake 设置",
  "read_board": "查看留言板",
  "post_to_board": "发布留言",
  "reply_to_board": "回复留言",
  "block_user": "屏蔽用户",
  "get_activity_summary": "查看活动状态",
  "log_period_event": "记录生理周期",
  "get_location": "查看位置",
  "get_device_status": "查看设备状态",
  "request_phone_screenshot": "请求手机截图",
  "get_todos": "查看待办",
  "add_todo": "记录待办",
  "get_countdowns": "查看倒计时",
  "get_ledger": "查看账本",
  "add_ledger": "记一笔账",
  "get_ledger_budget": "查看预算",
  "desire_add": "记录欲望",
  "desire_list": "查看欲望",
  "desire_act": "执行欲望",
  "desire_reflect": "反思欲望",
  "desire_history": "查看欲望历史",
  "set_self_trigger": "设置自主触发",
  "cancel_self_trigger": "取消自主触发",
  "create_html": "创建 HTML",
  "create_markdown": "创建 Markdown",
  "create_document": "创建文档",
  "issue_command": "发出手机指令",
  "collect_chat_moment": "收藏聊天到朋友圈"
}

_TOOL_NAMES = {name for _, _, names in GROUP_DEFS for name in names}
if set(DISPLAY_LABELS) != _TOOL_NAMES or any(not label.strip() or label == name for name, label in DISPLAY_LABELS.items()):
    raise RuntimeError("tool inventory display labels must cover every tool with readable Chinese text")

def _tool(tool_name: str, display_label: str, *, available: bool=False, reason_code: str="legacy_only", provider: str|None=None)->dict[str,Any]:
    return {"tool_name":tool_name,"display_label":display_label,"available":available,"status_label":"当前可用" if available else "当前不可用","reason_code":reason_code,"provider":provider}


_GRAY = {
  "browse_github": _tool("browse_github", "浏览 GitHub", reason_code="provider_blocked", provider="gateway._github_browse"),
  "collect_chat_moment": _tool("collect_chat_moment", "收藏聊天到朋友圈", reason_code="prerequisite_unproven", provider="mcp__home__collect_chat_moment"),
  "light_on": _tool("light_on", "开灯", reason_code="contract_disabled", provider="mcp__home__light_on"),
  "light_off": _tool("light_off", "关灯", reason_code="contract_disabled", provider="mcp__home__light_off"),
  "light_warm": _tool("light_warm", "暖光", reason_code="contract_disabled", provider="mcp__home"),
  "light_neutral": _tool("light_neutral", "中性光", reason_code="contract_disabled", provider="mcp__home"),
  "set_brightness": _tool("set_brightness", "设置亮度", reason_code="retired"),
  "set_color_temp": _tool("set_color_temp", "设置色温", reason_code="retired"),
  "codebase_patch": _tool("codebase_patch", "修改项目文件", reason_code="safety_gap", provider="mcp__codebase"),
  "codebase_create_file": _tool("codebase_create_file", "创建项目文件", reason_code="safety_gap", provider="mcp__codebase"),
}
from tools import tool_companion_hints
from tools.capability_manifest import get_capability
from tools.cc_capability_adapter import physical_surface_names

_LEGACY_CAPABILITY_ALIASES = {
  "memory.search": "search_memories",
  "memory.write": "save_memory",
  "diary.write": "write_diary",
  "todo.write": "add_todo",
  "ledger.write": "add_ledger",
  "web.search": "web_search",
  "web.read": "read_webpage",
  "home.light.status": "get_light_status",
  "todo.read": "get_todos",
  "countdown.read": "get_countdowns",
  "ledger.read": "get_ledger",
  "ledger.budget.read": "get_ledger_budget",
  "files.read": "codebase_read_file",
  "files.find": "codebase_list_directory",
  "code.search": "codebase_search_code",
}
_LEGACY_CURRENT_NAMES = frozenset(_LEGACY_CAPABILITY_ALIASES.values())


def _manifest_binding(capability_id: str) -> str | None:
    entry = get_capability(capability_id) or {}
    binding = (entry.get("provider_bindings") or {}).get("claude_code")
    return binding if isinstance(binding, str) and binding.strip() else None


def _current_capability_groups() -> list[dict[str, Any]]:
    hints = tool_companion_hints.payload()
    physical = set(physical_surface_names())
    groups: list[dict[str, Any]] = []
    for source_group in hints["groups"]:
        tools: list[dict[str, Any]] = []
        for hint in source_group["tools"]:
            capability_id = str(hint["capability_id"])
            binding = _manifest_binding(capability_id)
            if not binding:
                continue
            available = binding in physical
            tools.append(_tool(
                capability_id,
                str(hint["display_label"]),
                available=available,
                reason_code="active" if available else "runtime_disabled",
                provider=binding,
            ))
        groups.append({
            "id": str(source_group["id"]),
            "label": str(source_group["label"]),
            "total": len(tools),
            "available": sum(tool["available"] for tool in tools),
            "tools": tools,
        })
    return groups


def _legacy_row(name: str) -> dict[str, Any]:
    return _GRAY.get(name) or _tool(name, DISPLAY_LABELS[name])


def _legacy_groups() -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for group_id, label, names in GROUP_DEFS:
        historical_names = [name for name in names if name not in _LEGACY_CURRENT_NAMES]
        tools = [_legacy_row(name) for name in historical_names]
        groups.append({
            "id": "legacy:" + group_id,
            "label": "历史 · " + label,
            "total": len(tools),
            "available": sum(tool["available"] for tool in tools),
            "tools": tools,
        })
    return groups


def _external_groups() -> list[dict[str, Any]]:
    from tools.external_mcp_surface import list_external_surface
    groups: list[dict[str, Any]] = []
    for server in list_external_surface():
        connected = server["lifecycle_state"] == "CONNECTED"
        tools = [
            {
                "tool_name": tool["remote_tool_name"],
                "display_label": tool["description"] or tool["remote_tool_name"],
                "available": tool["available"] is True,
                "status_label": (
                    "当前可用"
                    if tool["available"] is True
                    else "未连接"
                    if not connected
                    else "当前不可用"
                ),
                "reason_code": (
                    "active"
                    if tool["available"] is True
                    else "external_surface_stale"
                ),
                "provider": "External MCP · Streamable HTTP",
            }
            for tool in server.get("tools", ())
        ]
        groups.append({
            "id": "external_mcp:" + str(server["server_id"]),
            "label": str(server["display_name"]),
            "total": len(tools),
            "available": sum(tool["available"] for tool in tools),
            "tools": tools,
            "lifecycle_state": server["lifecycle_state"],
            "transport": server["transport"],
        })
    return groups


def payload() -> dict[str, Any]:
    groups = _current_capability_groups() + _legacy_groups() + _external_groups()
    tools = [tool for group in groups for tool in group["tools"]]
    available = sum(tool["available"] for tool in tools)
    return {
        "ok": True,
        "version": "0.2",
        "total": len(tools),
        "available_count": available,
        "unavailable_count": len(tools) - available,
        "groups": groups,
    }


def inventory_names() -> list[str]:
    return [
        tool["tool_name"]
        for group in _current_capability_groups() + _legacy_groups() + _external_groups()
        for tool in group["tools"]
    ]
