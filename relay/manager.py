"""
RelayManager：统一的请求发送层。
gateway 所有对 API 的调用都经过这里。

url/key 优先级：ACTIVE_RELAY（runtime_config，指向 relay_presets 一行）
> .env（部署期兜底，尚未选过 relay 时用这个）。
model/ws_model 完全交给 config_store（其内部自带 .env 迁移期兜底）。

每次调用都重新读一遍（不缓存），配置改了立即生效，不需要重启 gateway 进程。
"""
import json
import sqlite3
import urllib.request
import urllib.error
from relay.capabilities import get_caps
from relay.adapter import adapt_request

DB_PATH = '/opt/frontend/memories.db'


def _lookup_active_relay():
    """按 config_store 里的 ACTIVE_RELAY（relay_presets.id）查 url/key。
    没设置过 ACTIVE_RELAY，或那一行已被删除时返回 None（调用方回退 .env）。"""
    import config_store as _cfg
    active_id = _cfg.get('ACTIVE_RELAY', '')
    if not active_id:
        return None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        row = conn.execute('SELECT url, key FROM relay_presets WHERE id=?', (active_id,)).fetchone()
        conn.close()
        if row and row[0]:
            return {'url': row[0], 'key': row[1] or ''}
    except Exception:
        pass
    return None


class RelayManager:
    def __init__(self, env_path="/opt/frontend/.env"):
        self.env_path = env_path
        self._reload_env()

    def _reload_env(self):
        import config_store as _cfg

        # .env 部署期兜底
        self.api_url = ""
        self.api_key = ""
        try:
            for ln in open(self.env_path):
                ln = ln.strip()
                if ln.startswith("API_URL="):
                    self.api_url = ln.split("=", 1)[1]
                elif ln.startswith("ANTHROPIC_API_KEY="):
                    self.api_key = ln.split("=", 1)[1]
        except Exception:
            pass

        # ACTIVE_RELAY（runtime_config）覆盖 .env
        active = _lookup_active_relay()
        if active:
            self.api_url = active['url'] or self.api_url
            self.api_key = active['key'] or self.api_key

        self.model = _cfg.get('MODEL')
        self.ws_model = _cfg.get('WS_MODEL')

    @property
    def caps(self):
        return get_caps(self.api_url)

    def call(self, payload: dict, timeout=60, use_ws_model=False) -> dict:
        """同步调用 API，自动按 relay 能力裁剪 payload。"""
        self._reload_env()

        if use_ws_model and self.ws_model:
            payload["model"] = self.ws_model
        elif "model" not in payload or not payload["model"]:
            payload["model"] = self.model

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        caps = self.caps
        payload, headers = adapt_request(payload, headers, caps)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.api_url, data=data, headers=headers)

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def call_stream(self, payload: dict, timeout=120, use_ws_model=False):
        """流式调用 API，返回 response 对象（调用方自己逐行读取 SSE）。"""
        self._reload_env()

        if use_ws_model and self.ws_model:
            payload["model"] = self.ws_model
        elif "model" not in payload or not payload["model"]:
            payload["model"] = self.model

        payload["stream"] = True

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        caps = self.caps
        payload, headers = adapt_request(payload, headers, caps)

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.api_url, data=data, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def extract_text(self, response: dict) -> str:
        """从 API 响应中提取纯文本"""
        return "".join(
            b.get("text", "")
            for b in response.get("content", [])
            if b.get("type") == "text"
        ).strip()


# 全局单例
relay = RelayManager()
