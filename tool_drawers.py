"""
tool_drawers.py — 工具抽屉路由（架构参考 mingyue3677/agent-tool-drawers）

把 gateway 的全量 TOOLS 按能力域分成「抽屉」，每轮对话只把命中的抽屉
开给模型，减少不相关 tool schema 占用的上下文，并降低误调用。

v1 策略（保守，宁可多给不误伤）：
  - 只做确定性 Force Rules（关键词正则），不做 LLM router
  - memory 抽屉常开（save/search 记忆是高频兜底能力）
  - 没有命中任何规则时回退全量工具 —— 行为与关闭时完全一致
  - 命中后 = 常开抽屉 + 命中抽屉的并集

运行时开关（runtime_config，改了立即生效不用重启）：
  - TOOL_DRAWERS_ENABLED: '1' 启用，默认 '0'（完全不改变现有行为）
  - TOOL_DRAWERS_LOG:     '1' 时每轮在 stdout 打一行选择结果（默认开）
"""
import json
import re

import config_store

# ── 抽屉定义 ────────────────────────────────────────────────
# id -> {label, tools}；工具名必须与 gateway.TOOLS 里的 name 一致。
# gateway 启动时会做一次校验，抽屉里引用了不存在的工具会打警告。
DRAWERS = {
    'memory': {
        'label': '记忆',
        'tools': ['save_memory', 'search_memories'],
    },
    'web': {
        'label': '联网',
        'tools': ['web_search', 'browse_github', 'read_webpage'],
    },
    'pocket': {
        'label': '手机浏览器',
        'tools': ['pocket_status', 'pocket_goto', 'pocket_js', 'pocket_html', 'pocket_screenshot'],
    },
    'light': {
        'label': '灯控',
        'tools': ['light_on', 'light_off', 'light_warm', 'light_neutral',
                  'set_brightness', 'set_color_temp', 'get_light_status'],
    },
    'shopping': {
        'label': '购物',
        'tools': ['shop_browse', 'shop_act', 'shop_checkout',
                  'shop_login_start', 'shop_login_status'],
    },
    'gallery': {
        'label': '相册/截图',
        'tools': ['save_to_gallery', 'recall_photo', 'screenshot_chat'],
    },
    'code': {
        'label': '代码/文件',
        'tools': ['read_backend_file', 'search_files', 'read_frontend_file',
                  'write_frontend_file', 'str_replace_frontend_file', 'check_page_render',
                  'shell_exec', 'ws_job', 'ws_ls', 'ws_read', 'ws_write', 'ws_edit', 'ws_patch', 'ws_diff',
                  'mcp_search', 'mcp_load', 'mcp_call', 'workspace_app'],
    },
    'workspace': {
        'label': '沙箱工作区',
        'tools': ['shell_exec', 'ws_job', 'ws_ls', 'ws_read', 'ws_write', 'ws_edit', 'ws_patch', 'ws_diff',
                  'mcp_search', 'mcp_load', 'mcp_call', 'workspace_app'],
    },
    'self_config': {
        'label': '自我配置',
        'tools': ['read_bot_config', 'edit_bot_config',
                  'get_wake_settings', 'set_wake_settings'],
    },
    'board': {
        'label': '留言板',
        'tools': ['read_board', 'post_to_board', 'reply_to_board', 'block_user'],
    },
    'life': {
        'label': '生活状态',
        'tools': ['get_activity_summary', 'log_period_event', 'get_location',
                  'get_device_status', 'request_phone_screenshot'],
    },
    'calendar': {
        'label': '生活 / 日程',
        'tools': ['get_todos', 'add_todo', 'get_countdowns',
                  'get_ledger', 'add_ledger', 'get_ledger_budget'],
    },
    'desire': {
        'label': '欲望账本',
        'tools': ['desire_add', 'desire_list', 'desire_act', 'desire_reflect', 'desire_history'],
    },
    'triggers': {
        'label': 'Wake',
        'tools': ['set_self_trigger', 'cancel_self_trigger'],
    },
    'artifacts': {
        'label': '产物生成',
        'tools': ['create_html', 'create_markdown', 'create_document'],
    },
    'phone': {
        'label': '行动',
        'tools': ['issue_command'],
    },
}

# 常开抽屉：不管命中什么，这些始终对模型可见
CORE_DRAWERS = ['memory']

DISABLED_TOOLS_KEY = 'TOOL_DISABLED'
DISABLED_DRAWERS_KEY = 'TOOL_DRAWER_DISABLED'

# ── Force Rules ─────────────────────────────────────────────
# (正则, [抽屉id])。按序全部匹配（不短路），命中即并入。
FORCE_RULES = [
    (re.compile(r'开灯|关灯|暖光|暖灯|中性光|亮度|色温|台灯|灯还?[开关亮]'), ['light']),
    (re.compile(r'搜一?下|搜索|查一?[下查]|最新|新闻|github|仓库|开源', re.I), ['web']),
    (re.compile(r'小红书|微博|收藏|pocket|手机浏览器|她的账号|登录态', re.I), ['pocket']),
    (re.compile(r'购物|淘宝|下单|购物车|结[账帐]|买.{0,6}(东西|个|件|点)|店里'), ['shopping']),
    (re.compile(r'照片|图片|相册|截图|拍的|存图'), ['gallery']),
    (re.compile(r'代码|文件|前端|后端|页面|部署|修(一下|个|复)|bug|报错', re.I), ['code', 'workspace']),
    (re.compile(r'沙箱|工作区|workspace|跑脚本|写脚本|执行命令|终端|ws_|mcp_search|自定义工具|workspace_app|实时应用', re.I), ['workspace']),
    (re.compile(r'待办|todo|记账|账本|预算|倒计时|calendar|ledger', re.I), ['calendar']),
    (re.compile(r'欲望|desire|想要|心愿', re.I), ['desire']),
    (re.compile(r'留言板|板子上|发(个)?帖|board', re.I), ['board']),
    (re.compile(r'在干嘛|在做什么|活动|屏幕|例假|月经|经期|姨妈|位置|在哪'), ['life']),
    (re.compile(r'提醒我|闹钟|定时|叫我|到点'), ['triggers']),
    (re.compile(r'写(个|份)?(文档|报告)|html|markdown|word|导出|做(个|张)(页面|卡片)', re.I), ['artifacts', 'code']),
    (re.compile(r'手机|发?指令|deep\s?link', re.I), ['phone']),
    (re.compile(r'记(住|一下|下来)|别忘|想起|回忆|之前说过'), ['memory']),
]


def validate(all_tools):
    """启动时校验抽屉引用的工具名是否都存在；返回警告列表（不抛异常）。"""
    known = {t.get('name') for t in all_tools}
    warnings = []
    for did, d in DRAWERS.items():
        for name in d['tools']:
            if name not in known:
                warnings.append(f'[drawers] {did} 引用了不存在的工具: {name}')
    covered = set()
    for d in DRAWERS.values():
        covered.update(d['tools'])
    for name in known - covered:
        warnings.append(f'[drawers] 工具未归入任何抽屉（启用后将不可见）: {name}')
    return warnings


def enabled():
    return config_store.get_bool('TOOL_DRAWERS_ENABLED', False)


def _load_name_set(key: str) -> set[str]:
    raw = (config_store.get(key, '') or '').strip()
    return _parse_name_set(raw)


def _parse_name_set(raw: str) -> set[str]:
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(name).strip() for name in parsed if str(name).strip()}
    except Exception:
        pass
    return {part.strip() for part in raw.split(',') if part.strip()}


def _mutate_name_set(key: str, mutator) -> set[str]:
    def apply(raw: str) -> str:
        updated = mutator(set(_parse_name_set(raw)))
        return json.dumps(sorted(updated))

    new_raw = config_store.mutate(key, '', apply)
    return _parse_name_set(new_raw)


def get_disabled_tools() -> set[str]:
    return _load_name_set(DISABLED_TOOLS_KEY)


def get_disabled_drawers() -> set[str]:
    return _load_name_set(DISABLED_DRAWERS_KEY)


def set_tool_enabled(tool_name: str, enabled_flag: bool) -> set[str]:
    clean = (tool_name or '').strip()
    if not clean:
        raise ValueError('tool required')

    def mutate(names: set[str]) -> set[str]:
        if enabled_flag:
            names.discard(clean)
        else:
            names.add(clean)
        return names

    return _mutate_name_set(DISABLED_TOOLS_KEY, mutate)


def set_drawer_enabled(drawer_id: str, enabled_flag: bool) -> set[str]:
    clean = (drawer_id or '').strip()
    if clean not in DRAWERS:
        raise ValueError('unknown drawer')

    def mutate(names: set[str]) -> set[str]:
        if enabled_flag:
            names.discard(clean)
        else:
            names.add(clean)
        return names

    return _mutate_name_set(DISABLED_DRAWERS_KEY, mutate)


def is_tool_enabled_in_drawer(
    tool_name: str,
    drawer_id: str,
    *,
    disabled_tools: set[str] | None = None,
    disabled_drawers: set[str] | None = None,
) -> bool:
    clean = (tool_name or '').strip()
    if not clean:
        return False
    tools = disabled_tools if disabled_tools is not None else get_disabled_tools()
    drawers = disabled_drawers if disabled_drawers is not None else get_disabled_drawers()
    if clean in tools:
        return False
    return drawer_id not in drawers


def drawer_enabled(drawer_id: str, *, disabled_drawers: set[str] | None = None) -> bool:
    drawers = disabled_drawers if disabled_drawers is not None else get_disabled_drawers()
    return drawer_id not in drawers


def _apply_disabled_tools(all_tools, allowed_names: set[str] | None = None):
    disabled_tools = get_disabled_tools()
    selected = []
    for tool in all_tools:
        name = (tool.get('name') or '').strip()
        if not name or name in disabled_tools:
            continue
        if allowed_names is not None and name not in allowed_names:
            continue
        selected.append(tool)
    return selected


def serialize_drawers() -> list[dict]:
    disabled_tools = get_disabled_tools()
    disabled_drawers = get_disabled_drawers()
    payload = []
    for drawer_id, info in DRAWERS.items():
        drawer_on = drawer_enabled(drawer_id, disabled_drawers=disabled_drawers)
        payload.append({
            'id': drawer_id,
            'label': info['label'],
            'enabled': drawer_on,
            'tools': [
                {
                    'name': name,
                    'enabled': is_tool_enabled_in_drawer(
                        name,
                        drawer_id,
                        disabled_tools=disabled_tools,
                        disabled_drawers=disabled_drawers,
                    ),
                }
                for name in info['tools']
            ],
        })
    return payload


def extract_user_text(messages):
    """从 build_messages() 的结果里取最后一条 user 文本，用于路由。"""
    for m in reversed(messages or []):
        if m.get('role') != 'user':
            continue
        c = m.get('content')
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = [b.get('text', '') for b in c
                     if isinstance(b, dict) and b.get('type') == 'text']
            if parts:
                return ' '.join(parts)
    return ''


def match_drawers(user_text):
    """返回命中的抽屉 id 列表（不含常开抽屉；未命中返回空列表）。"""
    if not user_text:
        return []
    hit = []
    for pattern, drawer_ids in FORCE_RULES:
        if pattern.search(user_text):
            for did in drawer_ids:
                if did not in hit and did in DRAWERS:
                    hit.append(did)
    return hit


def select_tools(user_text, all_tools):
    """
    返回 (tools, info)。
    个体工具禁用始终生效；抽屉级禁用只在路由命中对应抽屉时生效。
    """
    total = len(all_tools)
    if not enabled():
        selected = _apply_disabled_tools(all_tools)
        return selected, {
            'enabled': False,
            'mode': 'off',
            'tools': len(selected),
            'total': total,
        }

    hit = match_drawers(user_text)
    if not hit:
        selected = _apply_disabled_tools(all_tools)
        return selected, {
            'enabled': True,
            'mode': 'fallback_all',
            'drawers': [],
            'tools': len(selected),
            'total': total,
        }

    disabled_drawers = get_disabled_drawers()
    opened = list(CORE_DRAWERS)
    for did in hit:
        if did not in opened:
            opened.append(did)
    allowed = set()
    for did in opened:
        if did in disabled_drawers:
            continue
        allowed.update(DRAWERS[did]['tools'])
    selected = _apply_disabled_tools(all_tools, allowed)
    info = {
        'enabled': True,
        'mode': 'routed',
        'drawers': opened,
        'tools': len(selected),
        'total': total,
    }
    if config_store.get_bool('TOOL_DRAWERS_LOG', True):
        print(f"[drawers] {info['mode']} drawers={opened} "
              f"tools={info['tools']}/{info['total']} text={user_text[:40]!r}", flush=True)
    return selected, info


def select_tools_from_messages(messages, all_tools):
    return select_tools(extract_user_text(messages), all_tools)
