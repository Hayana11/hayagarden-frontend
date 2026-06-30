"""
每个 relay 的能力声明。
新增 relay 只改这个文件。
"""

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
    for pattern, relay_id in URL_PATTERNS.items():
        if pattern in api_url:
            return relay_id
    return "official"

def get_caps(api_url: str) -> dict:
    relay_id = detect_relay(api_url)
    return CAPABILITIES.get(relay_id, CAPABILITIES["official"])
