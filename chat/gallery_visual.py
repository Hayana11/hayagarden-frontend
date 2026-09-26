"""Neutral, image-only Gallery vision calls over the existing Relay adapter."""
from __future__ import annotations

from typing import Any, Callable, Optional


def describe_image_bytes(
    image_bytes: bytes,
    mime: str,
    *,
    question: Optional[str] = None,
    timeout: int = 15,
    relay_client: Any = None,
    extract_text_fn: Optional[Callable[[Any], str]] = None,
) -> Optional[str]:
    """Describe visible facts from supplied pixels, without persona or chat history."""
    if not image_bytes:
        return None
    from chat.cc_vision_bridge import build_image_content_block
    image_block = build_image_content_block(image_bytes, mime)
    system = (
        '你是中性视觉整理器。只根据这次提供的原图，报告可直接看见的事实：主体、动作、构图、'
        '颜色、光线和清晰可读的文字。不得推断人物身份、关系、心理、动机、故事或敏感属性。'
        '不确定就明确说看不清，不补全。只用简洁中文回答。'
    )
    if question:
        prompt = ('请只依据这张原图回答这个可见细节问题：%s\n'
                  '精确小字、数量、颜色、位置若无法确认，请说明无法确认，不要猜。') % str(question)[:500]
    else:
        prompt = '请用一到三句中文中性描述画面中可直接观察到的内容。'
    payload = {
        'max_tokens': 350,
        'system': system,
        'messages': [{'role': 'user', 'content': [
            image_block, {'type': 'text', 'text': prompt},
        ]}],
    }
    if relay_client is None:
        from relay.manager import relay as relay_client
    if extract_text_fn is None:
        from chat.response_parser import extract_text as extract_text_fn
    try:
        result = relay_client.call(payload, timeout=max(1, min(int(timeout), 30)))
        description = (extract_text_fn(result) or '').strip()
    except Exception:
        return None
    return description[:1000] or None
