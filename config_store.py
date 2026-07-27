"""
config_store.py
运行时配置的统一读写层，backed by runtime_config 表（memories.db）。

原则：
  - .env 只保留部署配置（API_URL/ANTHROPIC_API_KEY/CLAUDE_CODE_OAUTH_TOKEN/
    BOARD_TOKEN_FYODOR/CONTEXT_USAGE_REPORT_TOKEN/MOMENTS_OWNER_TOKEN/DISCORD_*/SECRET_KEY/PORT）
  - 运行时配置（MODEL/WS_MODEL/provider 路由/DESIRE_DRIVEN/LONGING_ENABLED/
    ACTIVE_RELAY）全部走这里，改了立即生效，不依赖进程重启
  - get() 不做进程内缓存，每次直接查 DB
  - 迁移期兜底：DB 里还没设置过的 key，回退读一次 .env 里的同名旧值，
    确保刚上线时不会读到空值；一旦有人调用过 set()，DB 优先，.env 从此失效
"""
import os
import sqlite3

DB_PATH = os.getenv('HAYAGARDEN_CONFIG_DB_PATH', '/opt/frontend/memories.db')
ENV_PATH = os.getenv('HAYAGARDEN_ENV_PATH', '/opt/frontend/.env')

# key -> 迁移期兜底读取的 .env 变量名（None 表示没有旧值可兜底）
_ENV_FALLBACK_KEYS = {
    'MODEL':           'MODEL',
    'WS_MODEL':        'WS_MODEL',
    'GW_PROVIDER':     'GW_PROVIDER',
    'CHAT_PROVIDER':   'CHAT_PROVIDER',
    'WAKE_PROVIDER':   'WAKE_PROVIDER',
    'BACKGROUND_PROVIDER': 'BACKGROUND_PROVIDER',
    'FALLBACK_PROVIDER': 'FALLBACK_PROVIDER',
    'DESIRE_DRIVEN':   'DESIRE_DRIVEN',
    'LONGING_ENABLED': 'LONGING_ENABLED',
    'DESIRE_LEDGER_ENABLED': 'DESIRE_LEDGER_ENABLED',
    'MIRROR_ENABLED': 'MIRROR_ENABLED',
    'IDENTITY_GOVERNANCE_ENABLED': 'IDENTITY_GOVERNANCE_ENABLED',
    'RELATIONSHIP_CONTEXT_ENABLED': 'RELATIONSHIP_CONTEXT_ENABLED',
    'RELATIONSHIP_BANDS_CALIBRATED': 'RELATIONSHIP_BANDS_CALIBRATED',
    'RELATIONSHIP_IDLE_REFRESH_SECONDS': 'RELATIONSHIP_IDLE_REFRESH_SECONDS',
    'WAKE_MIN_IDLE_MINUTES': 'WAKE_MIN_IDLE_MINUTES',
    'WAKE_RELATIONSHIP_CONTEXT_ENABLED': 'WAKE_RELATIONSHIP_CONTEXT_ENABLED',
}

_DEFAULTS = {
    'GW_PROVIDER':     'api_relay',
    # CHAT_PROVIDER 留空时兼容读取 GW_PROVIDER；wake 在 B1 前应在线上显式设为 api_relay。
    'CHAT_PROVIDER':   '',
    'WAKE_PROVIDER':   'inherit',
    'BACKGROUND_PROVIDER': 'api_relay',
    'FALLBACK_PROVIDER': 'none',
    'DESIRE_DRIVEN':   '0',
    'LONGING_ENABLED': '1',
    'DESIRE_LEDGER_ENABLED': '0',
    'MIRROR_ENABLED': '0',
    'IDENTITY_GOVERNANCE_ENABLED': '0',
    # A1 代码先安全落地；部署观察窗口由运行时配置显式开启。
    'RELATIONSHIP_CONTEXT_ENABLED': '0',
    'RELATIONSHIP_BANDS_CALIBRATED': '0',
    'RELATIONSHIP_IDLE_REFRESH_SECONDS': '3600',
    # A1.1：普通 Wake 至少空闲这么久才允许掷骰 / 调模型。
    'WAKE_MIN_IDLE_MINUTES': '30',
    # A1.1：Wake 注入 A1 关系上下文的独立回滚开关（不影响互动时钟修复）。
    'WAKE_RELATIONSHIP_CONTEXT_ENABLED': '1',
    # P-CONTEXT-CLEAN-WINDOW-SHADOW：诊断用干净窗，默认关闭。
    'CC_CLEAN_WINDOW_SHADOW_ENABLED': '0',
}


def _init_table():
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.execute('''CREATE TABLE IF NOT EXISTS runtime_config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at DATETIME DEFAULT (datetime('now','+8 hours'))
    )''')
    conn.commit()
    conn.close()


_init_table()


def _read_env_fallback(key):
    env_key = _ENV_FALLBACK_KEYS.get(key)
    if not env_key:
        return None
    result = None
    try:
        for ln in open(ENV_PATH):
            ln = ln.strip()
            if ln.startswith(env_key + '='):
                result = ln.split('=', 1)[1].strip()  # 后出现的行覆盖前面的，与其余 .env 解析处一致
    except Exception:
        pass
    return result


def get(key, default=None):
    """读运行时配置。DB 有值 > .env 迁移期兜底 > default 参数 > 内置默认值。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        row = conn.execute('SELECT value FROM runtime_config WHERE key=?', (key,)).fetchone()
        conn.close()
        if row is not None:
            return row[0]
    except Exception:
        pass
    env_val = _read_env_fallback(key)
    if env_val:
        return env_val
    if default is not None:
        return default
    return _DEFAULTS.get(key, '')


def set(key, value):
    """写运行时配置。立即生效，其他进程下次 get() 就能读到（同一个 sqlite 文件）。"""
    conn = sqlite3.connect(DB_PATH, timeout=3)
    conn.execute(
        "INSERT INTO runtime_config (key, value, updated_at) VALUES (?,?,datetime('now','+8 hours')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def mutate(key, default, mutator):
    """在同一连接里 BEGIN IMMEDIATE 后读-改-写，避免并发丢更新。"""
    conn = sqlite3.connect(DB_PATH, timeout=3)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT value FROM runtime_config WHERE key=?', (key,)).fetchone()
        current = row[0] if row is not None else default
        new_value = mutator(current)
        conn.execute(
            "INSERT INTO runtime_config (key, value, updated_at) VALUES (?,?,datetime('now','+8 hours')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(new_value)),
        )
        conn.commit()
        return new_value
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_bool(key, default=False):
    v = get(key, '1' if default else '0')
    return str(v).strip() == '1'


def get_int(key, default=0):
    try:
        return int(get(key, str(default)))
    except Exception:
        return default


def get_float(key, default=0.0):
    try:
        return float(get(key, str(default)))
    except Exception:
        return default


def exists(key):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        row = conn.execute('SELECT 1 FROM runtime_config WHERE key=?', (key,)).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False
