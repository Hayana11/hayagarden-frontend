# Gateway 重构方案 v1.0

> **目标**：把 gateway.py（2666行）拆成配置驱动的模块化架构
> **执行者**：费奥多尔·cc
> **审核者**：哈娅 + 费奥多尔·web

---

## 一、最终文件结构

```
/opt/frontend/
├── gateway.py              ← 瘦身后只做路由分发（~400行）
├── relay/
│   ├── __init__.py
│   ├── capabilities.py     ← 每个relay支持什么feature
│   ├── adapter.py          ← 根据relay能力自动裁剪请求
│   ├── manager.py          ← RelayManager：选relay、读key、发请求
│   └── registry.py         ← relay预设注册（从DB读）
├── wake/
│   ├── __init__.py          ← 暴露 wake_decide() 入口
│   ├── parser.py            ← THOUGHTS/ACTION/CONTENT 解析
│   ├── executor.py          ← action 执行器
│   ├── builder.py           ← system prompt 组装
│   └── modes/
│       ├── __init__.py
│       ├── base.py           ← WakeMode 基类
│       ├── normal.py
│       ├── dream.py
│       ├── nightwatch.py
│       └── summarize.py
├── chat/
│   ├── __init__.py
│   ├── system_builder.py   ← build_system()
│   ├── message_builder.py  ← build_messages()
│   ├── stream_handler.py   ← SSE stream
│   └── tool_runner.py      ← run_tool()
└── .env
```

---

## 二、Phase 1：RelayManager

### 核心思想
gateway 不再关心 relay 差异。所有 API 调用经过 RelayManager → adapter 自动降级。

### relay/capabilities.py

```python
CAPABILITIES = {
    "official": {
        "thinking": True, "cache": True, "tools": True,
        "stream": True, "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "guagua": {
        "thinking": True, "cache": True, "tools": True,
        "stream": True, "beta_header": "prompt-caching-2024-07-31",
        "max_system_len": 50000,
    },
    "68886868": {
        "thinking": False, "cache": False, "tools": True,
        "stream": True, "beta_header": None,
        "max_system_len": 8000,
    },
    "treegpt": {
        "thinking": False, "cache": False, "tools": True,
        "stream": True, "beta_header": None,
        "max_system_len": 10000,
    },
}

URL_PATTERNS = {
    "anthropic.com": "official",
    "guagua.uk": "guagua",
    "68886868.xyz": "68886868",
    "treegpt.cc": "treegpt",
}

def detect_relay(api_url: str) -> str:
    for pattern, relay_id in URL_PATTERNS.items():
        if pattern in api_url:
            return relay_id
    return "official"

def get_caps(api_url: str) -> dict:
    relay_id = detect_relay(api_url)
    return CAPABILITIES.get(relay_id, CAPABILITIES["official"])
```

### relay/adapter.py

```python
import copy

def adapt_request(payload: dict, headers: dict, caps: dict) -> tuple:
    payload = copy.deepcopy(payload)
    headers = dict(headers)

    # thinking
    if not caps.get("thinking", True):
        payload.pop("thinking", None)

    # cache - 展平 system list，去 cache_control
    if not caps.get("cache", True):
        sys = payload.get("system")
        if isinstance(sys, list):
            texts = []
            for block in sys:
                if isinstance(block, dict):
                    b = dict(block)
                    b.pop("cache_control", None)
                    texts.append(b.get("text", ""))
                else:
                    texts.append(str(block))
            payload["system"] = "\n".join(texts)
        for msg in payload.get("messages", []):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)

    # tools
    if not caps.get("tools", True):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    # beta header
    beta = caps.get("beta_header")
    if beta:
        headers["anthropic-beta"] = beta
    else:
        headers.pop("anthropic-beta", None)

    # system 长度
    max_len = caps.get("max_system_len", 50000)
    sys = payload.get("system")
    if isinstance(sys, str) and len(sys) > max_len:
        payload["system"] = sys[:max_len]

    return payload, headers
```

### relay/manager.py

```python
import json, urllib.request, urllib.error
from relay.capabilities import get_caps
from relay.adapter import adapt_request

class RelayManager:
    def __init__(self, env_path="/opt/frontend/.env"):
        self.env_path = env_path
        self._reload_env()

    def _reload_env(self):
        self.api_url = self.api_key = self.model = self.ws_model = ""
        try:
            for ln in open(self.env_path):
                ln = ln.strip()
                if ln.startswith("API_URL="): self.api_url = ln.split("=",1)[1]
                elif ln.startswith("ANTHROPIC_API_KEY="): self.api_key = ln.split("=",1)[1]
                elif ln.startswith("MODEL="): self.model = ln.split("=",1)[1]
                elif ln.startswith("WS_MODEL="): self.ws_model = ln.split("=",1)[1]
        except Exception: pass

    @property
    def caps(self):
        return get_caps(self.api_url)

    def call(self, payload: dict, timeout=60, use_ws_model=False) -> dict:
        self._reload_env()
        if use_ws_model and self.ws_model:
            payload["model"] = self.ws_model
        elif not payload.get("model"):
            payload["model"] = self.model

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload, headers = adapt_request(payload, headers, self.caps)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.api_url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def call_stream(self, payload: dict, timeout=120, use_ws_model=False):
        self._reload_env()
        if use_ws_model and self.ws_model:
            payload["model"] = self.ws_model
        elif not payload.get("model"):
            payload["model"] = self.model
        payload["stream"] = True
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload, headers = adapt_request(payload, headers, self.caps)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.api_url, data=data, headers=headers)
        return urllib.request.urlopen(req, timeout=timeout)

    def extract_text(self, response: dict) -> str:
        return "".join(
            b.get("text","") for b in response.get("content",[])
            if b.get("type") == "text"
        ).strip()

relay = RelayManager()
```

### 迁移方式

所有直接调 API 的地方改成：
```python
# 改之前
payload = json.dumps({...}).encode()
req = urllib.request.Request(API_URL, data=payload, headers={...})
with urllib.request.urlopen(req, timeout=60) as resp: ...

# 改之后
from relay.manager import relay
rd = relay.call({"model": MODEL, "system": system, "messages": messages,
    "thinking": {"type": "enabled", "budget_tokens": 10000}, "tools": tools})
```

---

## 三、Phase 2：Wake 模式拆分

### 现状
`wake_decide()` 200行 if/elif，4种模式共用正则解析。

### wake/modes/base.py
```python
from abc import ABC, abstractmethod

class WakeMode(ABC):
    name: str = "base"
    available_actions: list = ["none"]

    @abstractmethod
    def build_prompt(self, context: dict) -> str: ...

    @abstractmethod
    def post_process(self, parsed: dict, context: dict) -> dict: ...

    def get_system_suffix(self, context: dict) -> str:
        return ""
```

### 每个模式文件（normal/dream/nightwatch/summarize）继承 WakeMode。

### wake/__init__.py
```python
from wake.modes.normal import NormalMode
from wake.modes.dream import DreamMode
from wake.modes.nightwatch import NightwatchMode
from wake.modes.summarize import SummarizeMode
from wake.parser import parse_response
from wake.executor import execute_action

MODE_REGISTRY = {
    "normal": NormalMode(), "dream": DreamMode(),
    "nightwatch": NightwatchMode(), "summarize": SummarizeMode(),
}

def wake_decide(mode_name: str, context: dict) -> dict:
    mode = MODE_REGISTRY.get(mode_name, MODE_REGISTRY["normal"])
    prompt = mode.build_prompt(context)
    from relay.manager import relay
    response = relay.call({"system": system, "messages": [{"role":"user","content":prompt}], "max_tokens": 500})
    parsed = parse_response(relay.extract_text(response))
    result = mode.post_process(parsed, context)
    execute_action(result, context)
    return result
```

### gateway.py 改成
```python
@app.route('/wake', methods=['POST'])
def wake_endpoint():
    data = request.get_json() or {}
    context = _build_wake_context(data)
    from wake import wake_decide
    result = wake_decide(data.get('mode','normal'), context)
    return jsonify(result)
```

---

## 四、执行规则

### 分阶段提交
1. Phase 1 先做 relay/ 目录（纯新增）
2. 替换第一个调用点（workspace_chat）验证
3. 逐步替换其余调用点
4. Phase 2 做 wake/ 目录（纯新增）
5. 逐个模式搬迁，每搬一个验证一次

### 绝对不能碰的文件
- static/chat.html
- static/workspace.html
- workspace_server.py
- .env

### 每次提交前验证
```bash
python3 -c "import ast; ast.parse(open('gateway.py').read())"
cd /opt/frontend && python3 -c "from relay.manager import relay; print('OK')"
systemctl restart frontend-gw && sleep 3 && systemctl is-active frontend-gw
# 功能测试
curl -s -X POST http://127.0.0.1:5051/workspace/chat -H "Content-Type: application/json" -d '{"message":"hi","history":[]}' | python3 -c "import sys,json;print(json.load(sys.stdin).get('reply','')[:50])"
```

### 回滚
```bash
git tag pre-relay-refactor  # 开始前打
git tag pre-wake-refactor
# 出问题
git checkout pre-relay-refactor -- gateway.py
systemctl restart frontend-gw
```
