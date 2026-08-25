"""Legacy build_messages history assembly (main-branch semantics)."""
from __future__ import annotations

import datetime
import os
from typing import Any, Callable, Optional

from chat.attachment_contract import image_attachment_urls, text_file_attachments
from chat.history_boundary import compute_boundary_ids, legacy_block_limit, persist_history_boundary


def _file_marker(filename: str) -> str:
    return '[文件: %s]' % (filename or '附件')


def _has_file_marker(blocks: list[dict[str, Any]], filename: str) -> bool:
    name = filename or '附件'
    markers = ('[文件: %s]' % name, '[文件:%s]' % name)
    return any(
        any(marker in str(block.get('text') or '') for marker in markers)
        for block in blocks if block.get('type') == 'text'
    )



def _row_text_files(row: Any) -> list[dict[str, str]]:
    return text_file_attachments(
        _legacy_row_get(row, 'attachments'),
        legacy_file_url=_legacy_row_get(row, 'file_url'),
        legacy_file_name=_legacy_row_get(row, 'file_name'),
    )


def _row_image_urls(row: Any) -> list[str]:
    return image_attachment_urls(
        _legacy_row_get(row, 'attachments'),
        legacy_image_url=_legacy_row_get(row, 'image_url'),
    )


def assemble_legacy_history(
    rows: list[Any],
    *,
    available_count: int,
    static_dir: str,
    read_file_fn: Callable[[str, str], Optional[str]],
    img_block_fn: Callable[[str], Optional[dict]],
    format_tool_history_fn: Callable[[str], str],
    is_ai_author: Callable[[str], bool],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_ids = [_legacy_row_id(r) for r in rows]
    original_count = len(rows)
    limit = legacy_block_limit(available_count)
    trimmed = available_count > limit
    if len(rows) > limit:
        rows = rows[-limit:]

    total = len(rows)
    row_image_urls = [_row_image_urls(r) for r in rows]
    image_count = sum(len(urls) for urls in row_image_urls)
    keep_image_positions = set(range(max(0, image_count - 2), image_count))

    msgs: list[dict[str, Any]] = []
    image_position = 0
    prev_dt = None
    retained_ids: list[int] = []

    for ri, r in enumerate(rows):
        row_id = _legacy_row_id(r)
        if row_id:
            retained_ids.append(row_id)
        author = _legacy_row_get(r, 'author')
        is_ai = is_ai_author(author)
        role = 'assistant' if is_ai else 'user'

        note = ''
        cur_dt = None
        try:
            cur_dt = datetime.datetime.strptime(_legacy_row_get(r, 'created_at'), '%Y-%m-%d %H:%M:%S')
        except Exception:
            pass
        if cur_dt and prev_dt and not is_ai:
            gap = cur_dt - prev_dt
            if gap >= datetime.timedelta(minutes=30):
                hrs, rem = divmod(int(gap.total_seconds()), 3600)
                mins = rem // 60
                gap_str = ('%d小时%d分' % (hrs, mins)) if hrs else ('%d分钟' % mins)
                note = '[%s · 距上一条消息隔了%s] ' % (cur_dt.strftime('%m月%d日 %H:%M'), gap_str)
        if cur_dt:
            prev_dt = cur_dt

        blocks = []
        for image_url in row_image_urls[ri]:
            if image_position in keep_image_positions:
                blk = img_block_fn(image_url)
                if blk:
                    blocks.append(blk)
            else:
                blocks.append({'type': 'text', 'text': '[一张较早发送的图片，内容已不在上下文中]'})
            image_position += 1

        if _legacy_row_get(r, 'content'):
            blocks.append({'type': 'text', 'text': note + _legacy_row_get(r, 'content')})

        if not is_ai:
            for file_ref in _row_text_files(r):
                fu = file_ref['url']
                fname = file_ref['name'] or '附件'
                body = read_file_fn(static_dir, fu) if ri >= total - 6 else None
                if body is not None:
                    if len(body) > 30000:
                        body = body[:30000] + '\n...(文件过长已截断)'
                    blocks.append({
                        'type': 'text',
                        'text': '[用户发来文件: %s]\n```\n%s\n```' % (fname, body),
                    })
                elif not _has_file_marker(blocks, fname):
                    blocks.append({'type': 'text', 'text': _file_marker(fname)})

        if is_ai and _legacy_row_get(r, 'tool_calls'):
            th = format_tool_history_fn(_legacy_row_get(r, 'tool_calls'))
            if th:
                blocks.append({'type': 'text', 'text': th})

        if not blocks:
            continue

        content = blocks[0]['text'] if len(blocks) == 1 and blocks[0]['type'] == 'text' else blocks
        if msgs and msgs[-1]['role'] == role:
            prev = msgs[-1]['content']
            if isinstance(prev, str) and isinstance(content, str):
                msgs[-1]['content'] = prev + '\n' + content
            else:
                if isinstance(prev, str):
                    prev = [{'type': 'text', 'text': prev}]
                if isinstance(content, str):
                    content = [{'type': 'text', 'text': content}]
                msgs[-1]['content'] = prev + content
        else:
            msgs.append({'role': role, 'content': content})

    trimmed_up_to, oldest = compute_boundary_ids(all_ids, retained_ids)
    stats = {
        'rows_before_trim': original_count,
        'rows_after_trim': len(rows),
        'conversation_content_trimmed': trimmed,
        'trimmed_up_to_id': trimmed_up_to,
        'oldest_retained_message_id': oldest,
        'history_trimmed': trimmed,
        'block_count_trimmed': trimmed,
    }
    persist_history_boundary(trimmed_up_to_id=trimmed_up_to, oldest_retained_message_id=oldest)
    return msgs, stats


def _legacy_row_get(row: Any, key: str, default: Any = '') -> Any:
    if hasattr(row, 'keys') and key in row.keys():
        return row[key]
    return getattr(row, key, default)


def _legacy_row_id(row: Any) -> int:
    return int(_legacy_row_get(row, 'id', 0) or 0)
