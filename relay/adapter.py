"""
Feature Adapter：根据 relay 能力自动裁剪请求。
gateway 不需要关心 relay 差异，全部交给 adapter。
"""
import copy

def adapt_request(payload: dict, headers: dict, caps: dict) -> tuple:
    payload = copy.deepcopy(payload)
    headers = dict(headers)

    # ── thinking ──
    if not caps.get("thinking", True):
        payload.pop("thinking", None)

    # ── cache ──
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

    # ── tools ──
    if not caps.get("tools", True):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)

    # ── beta header ──
    beta = caps.get("beta_header")
    if beta:
        headers["anthropic-beta"] = beta
    else:
        headers.pop("anthropic-beta", None)

    # ── system 长度限制 ──
    max_len = caps.get("max_system_len", 50000)
    sys = payload.get("system")
    if isinstance(sys, str) and len(sys) > max_len:
        payload["system"] = sys[:max_len]

    return payload, headers
