"""
每个 relay 的能力声明。
新增 relay 只改这个文件的 CAPABILITIES/URL_PATTERNS。

get_caps() 优先读 relay_presets 表里针对该 URL 手动设置的 capabilities
（前端「添加预设」弹窗里的 3 个 toggle），没有手动设置时才回退到
静态 CAPABILITIES 字典按 URL 特征自动检测。
"""
import json
import sqlite3

DB_PATH = '/opt/frontend/memories.db'

# 能力字段说明：
#   thinking  - 是否支持 extended thinking (thinking parameter)
#   cache     - 是否支持 prompt caching (cache_control blocks)
#   tools     - 是否支持 tool_use
#   vision    - 是否支持看图片（image content block）。False 时 adapter
#               会把消息里的图片块换成占位文字，避免直接发过去报错或
#               被模型假装看懂瞎回答。
#   stream    - 是否支持 SSE stream
#   beta_header - 需要的 anthropic-beta header（None 表示不加）
#   max_system_len - system prompt 最大字符数（超出截断）

CAPABILITIES = {
    "official": {
        "thinking": True,
        "cache": True,
        "cache_1h": True,
        "tools": True,
        "vision": True,   # 官方原生模型自带视觉能力
        "stream": True,
        "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "guagua": {
        "thinking": True,
        "cache": True,
        "cache_1h": False, # 未复测 1h；先按 5m，避免把余额打在高价写入上
        "tools": True,
        "vision": True,   # 未单独实测，按"能力较全的中转站代理官方模型"推测
        "stream": True,
        "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "68886868": {
        "thinking": True,   # 实测：显式传 thinking 参数会返回针对性内容，不是空 block
        "cache": False,     # 未验证过带 cache_control 是否安全，维持原判
        "cache_1h": False,
        "tools": True,
        "vision": True,     # 实测：发真实图片能准确描述出内容，不是瞎猜
        "stream": True,
        "beta_header": None,
        "max_system_len": 30000,  # 实测 20800 字符 system 正常返回 200，留出安全余量
    },
    "treegpt": {
        "thinking": True,   # 实测：1024+ budget 可返回原生 thinking block
        "cache": True,      # 实测：cache_control + metadata.user_id 可读回 cache_read_input_tokens
        "cache_1h": False,  # 实测 ttl=1h 仍写入 ephemeral_5m，tree 当前不支持真 1h
        "tools": True,
        "vision": False,  # 未实测图片；先保守关闭，避免假装看图
        "stream": True,
        "beta_header": None,
        "max_system_len": 30000,  # 实测 2.1 万字符 system + 工具可正常返回
    },
}

URL_PATTERNS = {
    "anthropic.com": "official",
    "guagua.uk":     "guagua",
    "68886868.xyz":  "68886868",
    "treegpt.cc":    "treegpt",
}


def detect_relay(api_url: str) -> str:
    """从 API URL 自动检测 relay 类型"""
    for pattern, relay_id in URL_PATTERNS.items():
        if pattern in api_url:
            return relay_id
    return "official"


def _db_override(api_url: str) -> dict:
    """从 relay_presets 表读该 URL 手动设置的 capabilities（thinking/cache/tools 三项）。
    没有匹配行、没有设置过、或 DB 不可读时返回空 dict。"""
    if not api_url:
        return {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        row = conn.execute(
            'SELECT capabilities FROM relay_presets WHERE url=? AND capabilities IS NOT NULL '
            "AND capabilities != '' ORDER BY created_at DESC LIMIT 1",
            (api_url,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def get_caps(api_url: str) -> dict:
    """获取当前 relay 的能力配置：DB 手动覆盖 > 静态检测表"""
    relay_id = detect_relay(api_url)
    base = dict(CAPABILITIES.get(relay_id, CAPABILITIES["official"]))
    override = _db_override(api_url)
    for k in ('thinking', 'cache', 'tools'):
        if k in override:
            base[k] = bool(override[k])
    return base
