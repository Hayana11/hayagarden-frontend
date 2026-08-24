"""Assemble chat history messages with token budgets and tool caps."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from chat.context_budget import (
    default_estimate_tokens,
    file_content_sha256,
    file_ref_key,
    trim_rows_to_token_budget,
)
from chat.attachment_contract import image_attachment_urls, text_file_attachments
from chat.context_continuity import format_tool_history
from chat.history_boundary import (
    compute_boundary_ids,
    set_relay_history_head_id,
    set_relay_history_trimmed_up_to_id,
)

EstimateFn = Callable[[Optional[str]], int]
ImgBlockFn = Callable[[str], Optional[dict]]
ReadFileFn = Callable[[str, str], Optional[str]]

_TOOL_HISTORY_HEADER = '[上一轮我调用的工具与结果]'
_HISTORY_OMITTED_MARKER = '[更早的工具结果已因上下文预算省略]'
_ROLLING_SUMMARY_MARKER = '[更早对话的连续性摘要（滞出当前窗口的部分）]'
_RESIDENT_FILE_SUMMARY_SUFFIX = '...(此前 resident 已全文注入，以上为摘要)'
_FILE_WRAPPER_PREFIX = '[用户发来文件:'


def _row_get(row: Any, key: str, default: Any = '') -> Any:
    if hasattr(row, 'keys') and key in row.keys():
        return row[key]
    return getattr(row, key, default)


def _row_id(row: Any) -> int:
    return int(_row_get(row, 'id', 0) or 0)


@dataclass
class HistoryBuildStats:
    rows_before_trim: int = 0
    rows_after_trim: int = 0
    image_block_count: int = 0
    image_placeholder_count: int = 0
    rendered_text_tokens_estimate: int = 0
    file_injections: list[dict[str, Any]] = field(default_factory=list)
    history_trimmed: bool = False
    block_count_trimmed: bool = False
    conversation_content_trimmed: bool = False
    tool_history_trimmed: bool = False
    oldest_retained_message_id: int = 0
    trimmed_up_to_id: int = 0
    committed_full_file_refs: list[str] = field(default_factory=list)
    budget_overflow: bool = False
    overflow_tokens: int = 0


def flatten_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                parts.append(str(block.get('text') or ''))
        return '\n'.join(parts)
    return str(content or '')


def estimate_message_text_tokens(content: Any, estimate_tokens: EstimateFn = default_estimate_tokens) -> int:
    return estimate_tokens(flatten_message_content(content))


def _tool_caps():
    try:
        import config_store
        small = config_store.get_int('TOOL_INJECT_MAX', 2000)
        large = config_store.get_int('TOOL_INJECT_MAX_MCP', 8000)
        per_message = config_store.get_int('TOOL_INJECT_PER_MESSAGE_MAX', 12000)
        history_total = config_store.get_int('TOOL_INJECT_HISTORY_TOTAL_MAX', 12000)
    except Exception:
        small, large, per_message, history_total = 2000, 8000, 12000, 12000
    return small, large, per_message, history_total


def _text_block(text: str, *, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    block: dict[str, Any] = {'type': 'text', 'text': text}
    if meta:
        block['_hg_meta'] = meta
    return block


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
        _row_get(row, 'attachments'),
        legacy_file_url=_row_get(row, 'file_url'),
        legacy_file_name=_row_get(row, 'file_name'),
    )


def _row_image_urls(row: Any) -> list[str]:
    return image_attachment_urls(
        _row_get(row, 'attachments'),
        legacy_image_url=_row_get(row, 'image_url'),
    )


def strip_internal_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        msg = dict(msg)
        msg.pop('_hg_row_ids', None)
        content = msg.get('content')
        if isinstance(content, list):
            blocks = []
            for block in content:
                if not isinstance(block, dict):
                    blocks.append(block)
                    continue
                clean = {k: v for k, v in block.items() if k != '_hg_meta'}
                blocks.append(clean)
            msg['content'] = blocks
        out.append(msg)
    return out


def collect_committed_full_file_refs(messages: Sequence[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            meta = block.get('_hg_meta') or {}
            if meta.get('kind') == 'file' and meta.get('mode') == 'full' and meta.get('ref_key'):
                refs.add(str(meta['ref_key']))
    return refs


def apply_history_tool_budget(
    messages: list[dict[str, Any]],
    *,
    total_budget: Optional[int] = None,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], bool]:
    if total_budget is None:
        _, _, _, total_budget = _tool_caps()
    if total_budget <= 0 or not messages:
        return messages, False

    refs: list[dict[str, Any]] = []
    for mi, msg in enumerate(messages):
        content = msg.get('content')
        if isinstance(content, str) and _TOOL_HISTORY_HEADER in content:
            refs.append({'mi': mi, 'kind': 'str', 'text': content})
        elif isinstance(content, list):
            for bi, block in enumerate(content):
                if isinstance(block, dict) and block.get('type') == 'text':
                    text = str(block.get('text') or '')
                    if _TOOL_HISTORY_HEADER in text:
                        refs.append({'mi': mi, 'kind': 'list', 'bi': bi, 'text': text})

    if not refs:
        return messages, False

    refs_work = list(refs)
    while len(refs_work) > 0 and sum(estimate_tokens(r['text']) for r in refs_work) > total_budget:
        refs_work.pop(0)
    if len(refs_work) == len(refs):
        return messages, False
    refs = refs_work

    surviving = {(r['mi'], r.get('kind'), r.get('bi')): r['text'] for r in refs}
    out: list[dict[str, Any]] = []
    omitted_once = False
    for mi, msg in enumerate(messages):
        msg = dict(msg)
        content = msg.get('content')
        if isinstance(content, str) and _TOOL_HISTORY_HEADER in content:
            key = (mi, 'str', None)
            if key in surviving:
                msg['content'] = surviving[key]
            else:
                msg['content'] = _HISTORY_OMITTED_MARKER if not omitted_once else ''
                omitted_once = True
        elif isinstance(content, list):
            blocks = []
            for bi, block in enumerate(content):
                if (
                    isinstance(block, dict)
                    and block.get('type') == 'text'
                    and _TOOL_HISTORY_HEADER in str(block.get('text') or '')
                ):
                    key = (mi, 'list', bi)
                    if key in surviving:
                        blocks.append({**block, 'text': surviving[key]})
                    elif not omitted_once:
                        blocks.append({'type': 'text', 'text': _HISTORY_OMITTED_MARKER})
                        omitted_once = True
                else:
                    blocks.append(block)
            msg['content'] = blocks
        out.append(msg)
    return out, True


def _last_user_index(messages: Sequence[dict[str, Any]]) -> Optional[int]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            return i
    return None


def trim_messages_to_text_budget(
    messages: list[dict[str, Any]],
    *,
    budget: int,
    estimate_tokens: EstimateFn = default_estimate_tokens,
    protect_indices: Optional[Iterable[int]] = None,
) -> tuple[list[dict[str, Any]], bool, bool, int]:
    """Keep a contiguous newest suffix; always retain the latest user message."""
    if budget <= 0 or not messages:
        return messages, False, False, 0

    n = len(messages)
    costs = [estimate_message_text_tokens(messages[i].get('content'), estimate_tokens) for i in range(n)]
    last_user = _last_user_index(messages)
    if last_user is None:
        last_user = n - 1

    start = last_user
    total = costs[last_user]
    while start > 0:
        prev = start - 1
        if total + costs[prev] <= budget:
            start -= 1
            total += costs[prev]
        else:
            break

    suffix = [dict(messages[i]) for i in range(start, n)]
    trimmed = start > 0
    overflow = total > budget
    overflow_tokens = max(0, total - budget) if overflow else 0
    return suffix, trimmed, overflow, overflow_tokens


def _merge_message_content(prev: Any, content: Any) -> Any:
    if isinstance(prev, str) and isinstance(content, str):
        return prev + '\n' + content
    if isinstance(prev, str):
        prev = [{'type': 'text', 'text': prev}]
    if isinstance(content, str):
        content = [{'type': 'text', 'text': content}]
    return list(prev) + list(content)


def _merge_row_ids(msg: dict[str, Any], row_id: int) -> None:
    ids = list(msg.get('_hg_row_ids') or [])
    if row_id and row_id not in ids:
        ids.append(row_id)
    msg['_hg_row_ids'] = ids


def inject_rolling_summary_and_enforce_budget(
    messages: list[dict[str, Any]],
    *,
    rolling_summary: str,
    budget: int,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], int, bool, bool, int]:
    summary = (rolling_summary or '').strip()
    if not summary or budget <= 0:
        tokens = sum(estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in messages)
        return messages, tokens, False, False, 0

    msgs = [dict(m) for m in messages]
    last_user = _last_user_index(msgs)
    last_user_msg = msgs[last_user] if last_user is not None else None
    middle = [msgs[i] for i in range(len(msgs)) if i != last_user]

    marker = _ROLLING_SUMMARY_MARKER + '\n'
    summary_text = summary
    trimmed = False
    overflow = False
    overflow_tokens = 0

    def _assemble(summary_body: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if summary_body:
            out.append({'role': 'user', 'content': marker + summary_body})
        out.extend(middle)
        if last_user_msg is not None:
            out.append(dict(last_user_msg))
        return out

    while True:
        assembled = _assemble(summary_text)
        tokens = sum(estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in assembled)
        if tokens <= budget:
            return assembled, tokens, trimmed, overflow, overflow_tokens
        if len(middle) > 0:
            middle, dropped, ov, ov_tok = trim_messages_to_text_budget(
                middle, budget=max(1, budget // 2), estimate_tokens=estimate_tokens,
            )
            overflow = overflow or ov
            overflow_tokens = max(overflow_tokens, ov_tok)
            if dropped:
                trimmed = True
                continue
            middle = middle[1:]
            trimmed = True
            continue
        if summary_text:
            summary_text = summary_text[: max(0, len(summary_text) - 200)]
            trimmed = True
            if not summary_text:
                assembled = _assemble('')
                tokens = sum(estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in assembled)
                return assembled, tokens, trimmed, overflow, overflow_tokens
            continue
        overflow = True
        overflow_tokens = max(overflow_tokens, tokens - budget)
        return assembled, tokens, trimmed, overflow, overflow_tokens


def estimate_row_rendered_text(
    row: Mapping[str, Any],
    *,
    time_gap_prefix: str = '',
    file_body: str = '',
    tool_history_text: str = '',
    image_placeholder: bool = False,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> int:
    parts = [time_gap_prefix + str(row.get('content') or '')]
    if file_body:
        parts.append(file_body)
    if tool_history_text:
        parts.append(tool_history_text)
    if image_placeholder:
        parts.append('[一张较早发送的图片，内容已不在上下文中]')
    return estimate_tokens('\n'.join(p for p in parts if p))


def _resident_knows_file(url: str, body: str, resident_known_files: set[str]) -> bool:
    if not body:
        return False
    return file_ref_key(str(url), file_content_sha256(body)) in resident_known_files


def _finalize_boundary_stats(
    stats: HistoryBuildStats,
    all_row_ids: list[int],
    messages: list[dict[str, Any]],
) -> None:
    retained_ids: list[int] = []
    for msg in messages:
        for rid in msg.get('_hg_row_ids') or []:
            if rid not in retained_ids:
                retained_ids.append(int(rid))
    trimmed_up_to, oldest = compute_boundary_ids(all_row_ids, retained_ids)
    stats.trimmed_up_to_id = trimmed_up_to
    stats.oldest_retained_message_id = oldest or (min(retained_ids) if retained_ids else 0)
    stats.committed_full_file_refs = sorted(collect_committed_full_file_refs(messages))


def assemble_history_from_rows(
    rows: Sequence[Any],
    *,
    available_count: int,
    history_token_budget: int = 0,
    history_mode: str = 'legacy_block',
    relay_high_water: int = 0,
    relay_low_water: int = 0,
    relay_head_id: int = 0,
    window_base: int = 60,
    window_block: int = 20,
    static_dir: str,
    read_file_fn: ReadFileFn,
    img_block_fn: ImgBlockFn,
    is_ai_author: Callable[[str], bool],
    resident_file_hashes: Optional[set[str]] = None,
    apply_tool_budget: bool = True,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], HistoryBuildStats]:
    stats = HistoryBuildStats(rows_before_trim=len(rows))
    rows = list(rows)
    all_row_ids = [_row_id(r) for r in rows if _row_id(r)]
    resident_known_files = set(resident_file_hashes or ())

    if history_mode == 'legacy_block':
        limit = available_count
        if available_count > window_base:
            limit = window_base + ((available_count - window_base) % window_block)
        if limit <= 0:
            limit = window_base
        if len(rows) > limit:
            stats.block_count_trimmed = True
            stats.conversation_content_trimmed = True
            rows = rows[-limit:]
    elif history_mode == 'cc_token_budget' and history_token_budget > 0:
        small, large, per_message, _history_total = _tool_caps()
        prev_preview_dt: Optional[datetime.datetime] = None

        def _preview_row(row):
            nonlocal prev_preview_dt
            author = _row_get(row, 'author')
            tool_calls = _row_get(row, 'tool_calls')
            content = _row_get(row, 'content')
            file_refs = _row_text_files(row)
            gap = ''
            cur_dt = None
            try:
                cur_dt = datetime.datetime.strptime(_row_get(row, 'created_at'), '%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
            if cur_dt and prev_preview_dt and not is_ai_author(author):
                gap_delta = cur_dt - prev_preview_dt
                if gap_delta >= datetime.timedelta(minutes=30):
                    hrs, rem = divmod(int(gap_delta.total_seconds()), 3600)
                    mins = rem // 60
                    gap_str = ('%d小时%d分' % (hrs, mins)) if hrs else ('%d分钟' % mins)
                    gap = '[%s · 距上一条消息隔了%s] ' % (cur_dt.strftime('%m月%d日 %H:%M'), gap_str)
            if cur_dt:
                prev_preview_dt = cur_dt
            file_body = ''
            tool_text = ''
            if is_ai_author(author) and tool_calls and apply_tool_budget:
                tool_text = format_tool_history(
                    tool_calls,
                    cap_small=small,
                    cap_large=large,
                    cap_per_message=per_message,
                )
            if not is_ai_author(author):
                file_bodies: list[str] = []
                for file_ref in file_refs:
                    fu = file_ref['url']
                    body = read_file_fn(static_dir, fu) or ''
                    if body and _resident_knows_file(fu, body, resident_known_files):
                        body = body[:400] + '\n' + _RESIDENT_FILE_SUMMARY_SUFFIX
                    if body:
                        file_bodies.append(body)
                file_body = '\n'.join(file_bodies)
            return estimate_row_rendered_text(
                {'content': content},
                time_gap_prefix=gap,
                file_body=file_body,
                tool_history_text=tool_text,
                estimate_tokens=estimate_tokens,
            )

        rows, _ = trim_rows_to_token_budget(
            rows,
            budget=history_token_budget,
            text_fn=_preview_row,
            estimate_tokens=estimate_tokens,
        )
        if stats.rows_before_trim > len(rows):
            stats.conversation_content_trimmed = True
    elif history_mode == 'relay_hysteresis' and relay_head_id > 0:
        rows = [r for r in rows if _row_id(r) >= relay_head_id]

    stats.rows_after_trim = len(rows)
    total = len(rows)
    row_image_urls = [_row_image_urls(r) for r in rows]
    image_count = sum(len(urls) for urls in row_image_urls)
    keep_image_positions = set(range(max(0, image_count - 2), image_count))

    msgs: list[dict[str, Any]] = []
    image_position = 0
    prev_dt = None

    for ri, r in enumerate(rows):
        author = _row_get(r, 'author')
        row_id = _row_id(r)
        is_ai = is_ai_author(author)
        role = 'assistant' if is_ai else 'user'

        note = ''
        cur_dt = None
        try:
            cur_dt = datetime.datetime.strptime(_row_get(r, 'created_at'), '%Y-%m-%d %H:%M:%S')
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

        blocks: list[dict[str, Any]] = []
        for image_url in row_image_urls[ri]:
            if image_position in keep_image_positions:
                blk = img_block_fn(image_url)
                if blk:
                    blocks.append(blk)
                    stats.image_block_count += 1
            else:
                blocks.append(_text_block('[一张较早发送的图片，内容已不在上下文中]'))
                stats.image_placeholder_count += 1
            image_position += 1

        if _row_get(r, 'content'):
            blocks.append(_text_block(note + _row_get(r, 'content')))

        if not is_ai:
            for file_ref in _row_text_files(r):
                fu = file_ref['url']
                fname = file_ref['name'] or '附件'
                body = read_file_fn(static_dir, fu)
                if body is not None:
                    full_sha = file_content_sha256(body)
                    ref_key = file_ref_key(fu, full_sha)
                    mode = 'full'
                    if _resident_knows_file(fu, body, resident_known_files):
                        body = body[:400] + '\n' + _RESIDENT_FILE_SUMMARY_SUFFIX
                        mode = 'resident_summary'
                    elif ri >= total - 6:
                        if len(body) > 30000:
                            body = body[:30000] + '\n...(文件过长已截断)'
                    else:
                        mode = 'marker_only'
                        body = ''
                    if body:
                        text = '[用户发来文件: %s]\n```\n%s\n```' % (fname, body)
                        blocks.append(_text_block(text, meta={
                            'kind': 'file',
                            'mode': mode,
                            'url': fu,
                            'ref_key': ref_key,
                            'content_sha256': full_sha,
                        }))
                        stats.file_injections.append({
                            'url': fu,
                            'mode': mode,
                            'content_sha256': full_sha,
                            'ref_key': ref_key,
                            'tokens_estimate': estimate_tokens(text),
                        })
                    elif not _has_file_marker(blocks, fname):
                        marker = _file_marker(fname)
                        blocks.append(_text_block(marker, meta={
                            'kind': 'file',
                            'mode': 'marker_only',
                            'url': fu,
                            'ref_key': ref_key,
                            'content_sha256': full_sha,
                        }))
                        stats.file_injections.append({
                            'url': fu,
                            'mode': 'marker_only',
                            'content_sha256': full_sha,
                            'ref_key': ref_key,
                            'tokens_estimate': estimate_tokens(marker),
                        })
                elif not _has_file_marker(blocks, fname):
                    blocks.append(_text_block(_file_marker(fname), meta={
                        'kind': 'file', 'mode': 'marker_only', 'url': fu,
                    }))

        if is_ai and _row_get(r, 'tool_calls'):
            small, large, per_message, _ = _tool_caps()
            th = format_tool_history(
                _row_get(r, 'tool_calls'),
                cap_small=small,
                cap_large=large,
                cap_per_message=per_message if apply_tool_budget else None,
            )
            if th:
                blocks.append(_text_block(th))

        if not blocks:
            continue

        content: Any = blocks[0]['text'] if len(blocks) == 1 and blocks[0]['type'] == 'text' and '_hg_meta' not in blocks[0] else blocks
        if msgs and msgs[-1]['role'] == role:
            prev = msgs[-1]['content']
            msgs[-1]['content'] = _merge_message_content(prev, content)
            _merge_row_ids(msgs[-1], row_id)
        else:
            msgs.append({'role': role, 'content': content, '_hg_row_ids': [row_id] if row_id else []})

    if history_mode == 'cc_token_budget' and history_token_budget > 0:
        msgs, msg_trimmed, overflow, overflow_tokens = trim_messages_to_text_budget(
            msgs, budget=history_token_budget, estimate_tokens=estimate_tokens,
        )
        if msg_trimmed:
            stats.conversation_content_trimmed = True
        if overflow:
            stats.budget_overflow = True
            stats.overflow_tokens = overflow_tokens
    elif history_mode == 'relay_hysteresis' and relay_high_water > 0 and relay_low_water > 0:
        current_tokens = sum(estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in msgs)
        if current_tokens > relay_high_water:
            msgs, msg_trimmed, overflow, overflow_tokens = trim_messages_to_text_budget(
                msgs, budget=relay_low_water, estimate_tokens=estimate_tokens,
            )
            if msg_trimmed:
                stats.conversation_content_trimmed = True
            if overflow:
                stats.budget_overflow = True
                stats.overflow_tokens = overflow_tokens
            retained_ids = []
            for msg in msgs:
                retained_ids.extend(int(x) for x in (msg.get('_hg_row_ids') or []))
            if retained_ids:
                set_relay_history_head_id(min(retained_ids))
            trimmed_up_to, _oldest = compute_boundary_ids(all_row_ids, retained_ids)
            if trimmed_up_to > 0:
                set_relay_history_trimmed_up_to_id(trimmed_up_to)

    if apply_tool_budget:
        msgs, tool_trimmed = apply_history_tool_budget(msgs, estimate_tokens=estimate_tokens)
        if tool_trimmed:
            stats.tool_history_trimmed = True

    stats.history_trimmed = stats.conversation_content_trimmed or stats.tool_history_trimmed
    stats.rendered_text_tokens_estimate = sum(
        estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in msgs
    )
    _finalize_boundary_stats(stats, all_row_ids, msgs)
    return msgs, stats
