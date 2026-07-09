"""
Feature Adapter：根据 relay 能力自动裁剪请求。
gateway 不需要关心 relay 差异，全部交给 adapter。
"""
import copy


def _strip_msg_blocks(messages: list, drop_types: set) -> list:
    """去掉 messages 里指定类型的 content blocks，空内容的消息整条跳过。"""
    clean = []
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, list):
            c2 = [b for b in c if not (isinstance(b, dict) and b.get("type") in drop_types)]
            if not c2:
                continue
            clean.append({**msg, "content": c2})
        else:
            clean.append(msg)
    return clean


def _replace_image_blocks(messages: list) -> list:
    """vision=False 时，把图片块换成一句占位文字，而不是像 _strip_msg_blocks
    那样整条删掉——直接删掉的话，哈娅发过图这件事在对话历史里会完全消失，
    模型没法合理回应（比如接不上"你看这张图"这句话）。"""
    out = []
    for msg in messages:
        c = msg.get("content")
        if isinstance(c, list):
            c2 = []
            for b in c:
                if isinstance(b, dict) and b.get("type") == "image":
                    c2.append({
                        "type": "text",
                        "text": "[图片：当前对话模型看不了图，内容已省略。"
                                "如果哈娅提到图里的东西，直接问她图里是什么。]",
                    })
                else:
                    c2.append(b)
            out.append({**msg, "content": c2})
        else:
            out.append(msg)
    return out


def _upgrade_system_cache_ttl(payload: dict, ttl: str) -> None:
    """Upgrade explicit system cache breakpoints to a longer TTL.

    Mixed TTL rule: longer-lived breakpoints must appear before shorter ones.
    Our system blocks precede message rolling BP4, so system=1h + messages=5m
    is valid on providers that truly support 1h cache writes.
    """
    sys = payload.get("system")
    if not isinstance(sys, list):
        return
    for block in sys:
        if not isinstance(block, dict):
            continue
        cc = block.get("cache_control")
        if isinstance(cc, dict) and cc.get("type") == "ephemeral":
            cc["ttl"] = ttl


def adapt_request(payload: dict, headers: dict, caps: dict) -> tuple:
    """
    输入：完整的请求 payload 和 headers
    输出：根据 relay 能力裁剪后的 payload 和 headers
    """
    payload = copy.deepcopy(payload)
    headers = dict(headers)

    # ── thinking ──
    if not caps.get("thinking", True):
        payload.pop("thinking", None)
        # messages 里的 thinking blocks 也要去掉（assistant turn 里会有）
        if "messages" in payload:
            payload["messages"] = _strip_msg_blocks(
                payload["messages"], {"thinking"}
            )

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

    elif caps.get("cache_1h"):
        _upgrade_system_cache_ttl(payload, "1h")

    # ── tools ──
    if not caps.get("tools", True):
        payload.pop("tools", None)
        payload.pop("tool_choice", None)
        if "messages" in payload:
            payload["messages"] = _strip_msg_blocks(
                payload["messages"], {"tool_use", "tool_result"}
            )

    # ── vision ──
    if not caps.get("vision", True):
        if "messages" in payload:
            payload["messages"] = _replace_image_blocks(payload["messages"])

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
