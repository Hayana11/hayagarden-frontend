"""Resolve an explicitly selected image from one authoritative chat turn."""
from __future__ import annotations

from typing import Any, Callable


class CurrentTurnImageError(ValueError):
    pass


def resolve_current_turn_image(
    message_id: Any,
    conversation_id: Any,
    *,
    get_db_fn: Callable[[], Any],
    image_index: Any = None,
) -> dict[str, Any]:
    """Return one image ref from exactly ``message_id``; never search globally."""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        raise CurrentTurnImageError('当前轮没有可绑定的用户消息')
    if mid <= 0:
        raise CurrentTurnImageError('当前轮没有可绑定的用户消息')

    conn = get_db_fn()
    try:
        row = conn.execute('SELECT * FROM chat_messages WHERE id=?', (mid,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise CurrentTurnImageError('当前用户消息不存在，无法安全收藏图片')
    record = dict(row)
    if str(record.get('author') or '').strip().lower() not in {'hayana', 'haya', 'user'}:
        raise CurrentTurnImageError('当前绑定消息不是用户消息，无法安全收藏图片')
    source_chat_id = str(conversation_id or '').strip()
    if not source_chat_id:
        raise CurrentTurnImageError('当前会话标识缺失，无法安全收藏图片')

    from chat.attachment_contract import (
        AttachmentValidationError,
        provider_current_turn_attachments,
    )
    try:
        attachments = provider_current_turn_attachments(
            record.get('attachments') or '[]',
            legacy_image_url=record.get('image_url') or '',
        )
    except AttachmentValidationError as exc:
        raise CurrentTurnImageError('当前用户消息附件无效，无法安全收藏图片') from exc
    images = [item for item in attachments if item.get('type') == 'image']
    if not images:
        raise CurrentTurnImageError('当前轮没有可收藏的用户图片')

    if image_index is None:
        if len(images) != 1:
            raise CurrentTurnImageError('当前轮有多张图片，请用 image_index 选择（从 0 开始）')
        index = 0
    else:
        if isinstance(image_index, bool):
            raise CurrentTurnImageError('image_index 必须是从 0 开始的整数')
        try:
            index = int(image_index)
        except (TypeError, ValueError):
            raise CurrentTurnImageError('image_index 必须是从 0 开始的整数')
        if index < 0 or index >= len(images):
            raise CurrentTurnImageError('image_index 超出当前轮图片范围')

    return {
        'ref': images[index]['url'],
        'image_index': index,
        'source_msg_id': int(record['id']),
        'source_chat_id': source_chat_id,
    }
