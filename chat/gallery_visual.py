"""Neutral, image-only Gallery vision calls over the existing Relay adapter."""
from __future__ import annotations

import json
import logging
import re
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



def annotate_image_bytes(
    image_bytes: bytes, mime: str, *, timeout: int = 15,
    relay_client: Any = None, extract_text_fn: Optional[Callable[[Any], str]] = None,
) -> dict[str, Any]:
    """Return neutral structured observations through Gallery's existing Relay vision path."""
    empty = {"caption": "", "ocr_text": "", "entities": [], "model_identity": ""}
    if not image_bytes:
        return empty
    from chat.cc_vision_bridge import build_image_content_block
    from relay.manager import resolve_active_relay_model_identity

    model = ""
    try:
        model = str(resolve_active_relay_model_identity() or "")
        if relay_client is None:
            from relay.manager import relay as relay_client
        if extract_text_fn is None:
            from chat.response_parser import extract_text as extract_text_fn
        payload = {
            "max_tokens": 500,
            "system": (
                "只看这次提供的像素，不推断身份、关系、心理、动机、故事或敏感属性；看不清就留空。只返回 JSON："
                '{"caption":"可见画面","ocr_text":"清晰可读原文","entities":["可见物体"]}'
            ),
            "messages": [{"role": "user", "content": [
                build_image_content_block(image_bytes, mime),
                {"type": "text", "text": "分别填写 caption、ocr_text 和可见实体；无法确认就留空或返回空数组。"},
            ]}],
        }
        result = relay_client.call(payload, timeout=max(1, min(int(timeout), 30)))
        match = re.search(r"\{.*\}", str(extract_text_fn(result) or ""), re.S)
        if not match:
            raise ValueError("structured_vision_json_missing")
        parsed = json.loads(match.group(0))
        entities = parsed.get("entities")
        return {
            "caption": str(parsed.get("caption") or "").strip()[:1000],
            "ocr_text": str(parsed.get("ocr_text") or "").strip()[:2000],
            "entities": [str(x).strip()[:100] for x in entities if str(x).strip()][:32]
            if isinstance(entities, list) else [],
            "model_identity": model,
        }
    except Exception as exc:
        logging.getLogger(__name__).warning("gallery vision annotation unavailable: %s", type(exc).__name__)
        return {**empty, "model_identity": model}
