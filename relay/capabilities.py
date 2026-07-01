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
#   stream    - 是否支持 SSE stream
#   beta_header - 需要的 anthropic-beta header（None 表示不加）
#   max_system_len - system prompt 最大字符数（超出截断）

CAPABILITIES = {
    "official": {
        "thinking": True,
        "cache": True,
        "tools": True,
        "stream": True,
        "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "guagua": {
        "thinking": True,
        "cache": True,
        "tools": True,
        "stream": True,
        "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "68886868": {
        "thinking": False,
        "cache": False,
        "tools": True,
        "stream": True,
        "beta_header": None,
        "max_system_len": 8000,
    },
    "treegpt": {
        "thinking": False,
        "cache": False,
        "tools": True,
        "stream": True,
        "beta_header": None,
        "max_system_len": 10000,
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
