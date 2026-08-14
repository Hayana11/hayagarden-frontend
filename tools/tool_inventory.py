"""Read-only historical Gateway tool inventory; never imports gateway.py."""
from __future__ import annotations
from typing import Any
GROUP_DEFS=[["memory","记忆",["save_memory","search_memories"]],["web","联网",["web_search","browse_github","read_webpage"]],["pocket","Pocket 手机浏览器",["pocket_status","pocket_goto","pocket_js","pocket_html","pocket_screenshot"]],["light","灯",["light_on","light_off","light_warm","light_neutral","set_brightness","set_color_temp","get_light_status"]],["shopping","购物",["shop_browse","shop_act","shop_checkout","shop_login_start","shop_login_status"]],["gallery","相册/截图",["save_to_gallery","recall_photo","screenshot_chat"]],["code_files","代码/文件",["read_backend_file","search_files","read_frontend_file","write_frontend_file","str_replace_frontend_file","check_page_render","codebase_describe_project","codebase_read_file","codebase_list_directory","codebase_search_code","codebase_find_references","codebase_patch","codebase_create_file","codebase_git_view","codebase_explain_history"]],["workspace","Workspace",["shell_exec","ws_job","ws_ls","ws_read","ws_write","ws_edit","ws_patch","ws_diff","mcp_search","mcp_load","mcp_call","workspace_app"]],["self_config","自我配置",["read_bot_config","edit_bot_config","get_wake_settings","set_wake_settings"]],["board","留言板",["read_board","post_to_board","reply_to_board","block_user"]],["life","生活状态",["get_activity_summary","log_period_event","get_location","get_device_status","request_phone_screenshot"]],["plans_ledger","计划与账本",["get_todos","add_todo","get_countdowns","get_ledger","add_ledger","get_ledger_budget"]],["desire","欲望账本",["desire_add","desire_list","desire_act","desire_reflect","desire_history"]],["triggers","自主触发",["set_self_trigger","cancel_self_trigger"]],["artifacts","产物生成",["create_html","create_markdown","create_document"]],["phone","手机指令",["issue_command"]],["moments","朋友圈",["collect_chat_moment"]]]
def _tool(tool_name: str, display_label: str, *, available: bool=False, reason_code: str="legacy_only", provider: str|None=None)->dict[str,Any]:
    return {"tool_name":tool_name,"display_label":display_label,"available":available,"status_label":"当前可用" if available else "当前不可用","reason_code":reason_code,"provider":provider}
_ACTIVE={
"search_memories":_tool("search_memories","搜索长期记忆",available=True,reason_code="active",provider="mcp__home__search_memories"),
"get_light_status":_tool("get_light_status","查看灯光状态",available=True,reason_code="active",provider="mcp__home__get_light_status"),
"get_todos":_tool("get_todos","查看待办",available=True,reason_code="active",provider="mcp__home__get_todos"),
"get_countdowns":_tool("get_countdowns","查看倒计时",available=True,reason_code="active",provider="mcp__home__get_countdowns"),
"get_ledger":_tool("get_ledger","查看账本",available=True,reason_code="active",provider="mcp__home__get_ledger"),
"get_ledger_budget":_tool("get_ledger_budget","查看预算",available=True,reason_code="active",provider="mcp__home__get_ledger_budget"),
"codebase_describe_project":_tool("codebase_describe_project","读取项目架构",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_read_file":_tool("codebase_read_file","读取项目文件",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_list_directory":_tool("codebase_list_directory","查找项目文件",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_search_code":_tool("codebase_search_code","搜索代码",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_find_references":_tool("codebase_find_references","查找代码引用",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_git_view":_tool("codebase_git_view","查看 Git 历史",available=True,reason_code="active",provider="mcp__codebase"),
"codebase_explain_history":_tool("codebase_explain_history","解释代码历史",available=True,reason_code="active",provider="mcp__codebase")}
_GRAY={
"add_todo":_tool("add_todo","记录待办",reason_code="safety_gap",provider="mcp__home__add_todo"),
"add_ledger":_tool("add_ledger","记一笔账",reason_code="safety_gap",provider="mcp__home__add_ledger"),
"collect_chat_moment":_tool("collect_chat_moment","收藏聊天到朋友圈",reason_code="prerequisite_unproven",provider="mcp__home__collect_chat_moment"),
"light_on":_tool("light_on","开灯",reason_code="contract_disabled",provider="mcp__home__light_on"),
"light_off":_tool("light_off","关灯",reason_code="contract_disabled",provider="mcp__home__light_off"),
"light_warm":_tool("light_warm","暖光",reason_code="contract_disabled",provider="mcp__home"),
"light_neutral":_tool("light_neutral","中性光",reason_code="contract_disabled",provider="mcp__home"),
"set_brightness":_tool("set_brightness","设置亮度",reason_code="retired"),
"set_color_temp":_tool("set_color_temp","设置色温",reason_code="retired"),
"codebase_patch":_tool("codebase_patch","修改项目文件",reason_code="safety_gap",provider="mcp__codebase"),
"codebase_create_file":_tool("codebase_create_file","创建项目文件",reason_code="safety_gap",provider="mcp__codebase")}
def _row(name): return _ACTIVE.get(name) or _GRAY.get(name) or _tool(name,name)
def _build_groups():
    out=[]
    for gid,label,names in GROUP_DEFS:
        ts=[_row(n) for n in names]
        out.append({"id":gid,"label":label,"total":len(ts),"available":sum(t["available"] for t in ts),"tools":ts})
    return out
def payload():
    groups=_build_groups(); ts=[t for g in groups for t in g["tools"]]; a=sum(t["available"] for t in ts)
    return {"ok":True,"version":"0.1","total":len(ts),"available_count":a,"unavailable_count":len(ts)-a,"groups":groups}
def inventory_names(): return [t["tool_name"] for g in _build_groups() for t in g["tools"]]
