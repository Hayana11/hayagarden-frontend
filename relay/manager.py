"""
RelayManager：统一的请求发送层。
gateway 所有对 API 的调用都经过这里。
"""
import json
import urllib.request
import urllib.error
from relay.capabilities import get_caps
from relay.adapter import adapt_request


class RelayManager:
    def __init__(self, env_path="/opt/frontend/.env"):
        self.env_path = env_path
        self._reload_env()

    def _reload_env(self):
        self.api_url = ""
        self.api_key = ""
        self.model = ""
        self.ws_model = ""
        try:
            for ln in open(self.env_path):
                ln = ln.strip()
                if ln.startswith("API_URL="):
                    self.api_url = ln.split("=", 1)[1]
                elif ln.startswith("ANTHROPIC_API_KEY="):
                    self.api_key = ln.split("=", 1)[1]
                elif ln.startswith("MODEL="):
                    self.model = ln.split("=", 1)[1]
                elif ln.startswith("WS_MODEL="):
                    self.ws_model = ln.split("=", 1)[1]
        except Exception:
            pass

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
