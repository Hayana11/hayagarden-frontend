"""Assemble chat history messages with token budgets and tool caps.

Text-token budget covers rendered text blocks only (content, time-gap notes,
file bodies, tool history, image placeholders). Image binary payloads are not
included in HISTORY_TOKEN_BUDGET; callers should keep the last-N image hard cap
separately and record image counts/bytes in observability.
"""
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
from chat.context_continuity import format_tool_history

EstimateFn = Callable[[Optional[str]], int]
ImgBlockFn = Callable[[str], Optional[dict]]
ReadFileFn = Callable[[str, str], Optional[str]]

_TOOL_HISTORY_HEADER = '[上一轮我调用的工具与结果]'
_HISTORY_OMITTED_MARKER = '[更早的工具结果已因上下文预算省略]'
_ROLLING_SUMMARY_MARKER = '[更早对话的连续性摘要（滞出当前窗口的部分）]'
_RESIDENT_FILE_SUMMARY_SUFFIX = '...(此前 resident 已全文注入，以上为摘要)'


def _row_get(row: Any, key: str, default: Any = '') -> Any:
    if hasattr(row, 'keys') and key in row.keys():
        return row[key]
    return getattr(row, key, default)


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


def apply_history_tool_budget(
    messages: list[dict[str, Any]],
    *,
    total_budget: Optional[int] = None,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], bool]:
    """Trim oldest tool-history blocks across the whole transcript."""
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
) -> tuple[list[dict[str, Any]], bool]:
    if budget <= 0 or not messages:
        return messages, False

    n = len(messages)
    protected = {int(i) for i in (protect_indices or ())}
    last_user = _last_user_index(messages)
    if last_user is not None:
        protected.add(last_user)

    costs = [estimate_message_text_tokens(messages[i].get('content'), estimate_tokens) for i in range(n)]
    kept = set(protected)
    total = sum(costs[i] for i in kept)

    for i in range(n - 1, -1, -1):
        if i in kept:
            continue
        if kept - protected and total + costs[i] > budget:
            continue
        if not (kept - protected) and total + costs[i] > budget:
            kept.add(i)
            total += costs[i]
            continue
        if total + costs[i] > budget:
            break
        kept.add(i)
        total += costs[i]

    if not kept:
        kept.add(n - 1)
    if len(kept) == n:
        return messages, False
    return [messages[i] for i in sorted(kept)], True


def inject_rolling_summary_and_enforce_budget(
    messages: list[dict[str, Any]],
    *,
    rolling_summary: str,
    budget: int,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], int, bool]:
    """Inject rolling summary prefix and trim while protecting summary + last user."""
    summary = (rolling_summary or '').strip()
    if not summary or budget <= 0:
        tokens = sum(estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in messages)
        return messages, tokens, False

    msgs = [dict(m) for m in messages]
    last_user = _last_user_index(msgs)
    last_user_msg = msgs[last_user] if last_user is not None else None
    middle = [msgs[i] for i in range(len(msgs)) if i != last_user]

    marker = _ROLLING_SUMMARY_MARKER + '\n'
    summary_text = summary
    trimmed = False

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
            return assembled, tokens, trimmed
        if len(middle) > 0:
            middle, dropped = trim_messages_to_text_budget(middle, budget=max(1, budget // 2), estimate_tokens=estimate_tokens)
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
                return assembled, tokens, trimmed
            continue
        return assembled, tokens, trimmed


def committed_file_refs_in_messages(
    messages: Sequence[dict[str, Any]],
    file_injections: Sequence[Mapping[str, Any]],
) -> set[str]:
    """Return url|sha256 refs for full file bodies still present in rendered messages."""
    joined = '\n'.join(flatten_message_content(m.get('content')) for m in messages)
    refs: set[str] = set()
    for fi in file_injections or ():
        if fi.get('mode') != 'full':
            continue
        url = str(fi.get('url') or '')
        ref_key = str(fi.get('ref_key') or '')
        if not url or not ref_key:
            continue
        if '[用户发来文件:' not in joined or url not in joined:
            continue
        idx = joined.find(url)
        if idx >= 0:
            snippet = joined[max(0, idx - 40): idx + 500]
            if _RESIDENT_FILE_SUMMARY_SUFFIX in snippet:
                continue
        refs.add(ref_key)
    return refs


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


def assemble_history_from_rows(
    rows: Sequence[Any],
    *,
    available_count: int,
    history_token_budget: int,
    window_base: int = 60,
    window_block: int = 20,
    static_dir: str,
    read_file_fn: ReadFileFn,
    img_block_fn: ImgBlockFn,
    is_ai_author: Callable[[str], bool],
    resident_file_hashes: Optional[set[str]] = None,
    estimate_tokens: EstimateFn = default_estimate_tokens,
) -> tuple[list[dict[str, Any]], HistoryBuildStats]:
    """Build API-relay/CC-bootstrap history messages from DB rows."""
    stats = HistoryBuildStats(rows_before_trim=len(rows))
    rows = list(rows)
    resident_known_files = set(resident_file_hashes or ())

    if history_token_budget <= 0:
        limit = available_count
        if available_count > window_base:
            limit = window_base + ((available_count - window_base) % window_block)
        if limit <= 0:
            limit = window_base
        if len(rows) > limit:
            stats.block_count_trimmed = True
            stats.conversation_content_trimmed = True
            rows = rows[-limit:]
    else:
        small, large, per_message, _history_total = _tool_caps()
        prev_preview_dt: Optional[datetime.datetime] = None

        def _preview_row(row):
            nonlocal prev_preview_dt
            author = _row_get(row, 'author')
            tool_calls = _row_get(row, 'tool_calls')
            content = _row_get(row, 'content')
            file_url = _row_get(row, 'file_url')
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
            if is_ai_author(author) and tool_calls:
                tool_text = format_tool_history(
                    tool_calls,
                    cap_small=small,
                    cap_large=large,
                    cap_per_message=per_message,
                )
            fu = file_url
            if fu and not is_ai_author(author) and str(fu).startswith('/static/'):
                file_body = read_file_fn(static_dir, str(fu)) or ''
                if file_body and _resident_knows_file(str(fu), file_body, resident_known_files):
                    file_body = file_body[:400] + '\n' + _RESIDENT_FILE_SUMMARY_SUFFIX
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

    stats.rows_after_trim = len(rows)
    stats.history_trimmed = stats.conversation_content_trimmed
    total = len(rows)
    img_indices = [i for i, r in enumerate(rows) if _row_get(r, 'image_url')]
    keep_img_indices = set(img_indices[-2:])

    msgs: list[dict[str, Any]] = []
    prev_dt = None
    seen_files: set[str] = set()

    for ri, r in enumerate(rows):
        author = _row_get(r, 'author')
        is_ai = is_ai_author(author)
        role = 'assistant' if is_ai else 'user'

        note = ''
        cur_dt = None
        try:
            cur_dt = datetime.datetime.strptime(r['created_at'], '%Y-%m-%d %H:%M:%S')
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
        if _row_get(r, 'image_url'):
            if ri in keep_img_indices:
                blk = img_block_fn(_row_get(r, 'image_url'))
                if blk:
                    blocks.append(blk)
                    stats.image_block_count += 1
            else:
                blocks.append({'type': 'text', 'text': '[一张较早发送的图片，内容已不在上下文中]'})
                stats.image_placeholder_count += 1

        if _row_get(r, 'content'):
            blocks.append({'type': 'text', 'text': note + _row_get(r, 'content')})

        fu = _row_get(r, 'file_url')
        if fu and not is_ai and str(fu).startswith('/static/'):
            body = read_file_fn(static_dir, str(fu))
            if body is not None:
                fname = _row_get(r, 'file_name') or '附件'
                full_sha = file_content_sha256(body)
                mode = 'full'
                if _resident_knows_file(str(fu), body, resident_known_files):
                    body = body[:400] + '\n' + _RESIDENT_FILE_SUMMARY_SUFFIX
                    mode = 'resident_summary'
                elif ri >= total - 6:
                    if len(body) > 30000:
                        body = body[:30000] + '\n...(文件过长已截断)'
                    seen_files.add(str(fu))
                else:
                    mode = 'marker_only'
                    body = ''
                if body:
                    text = '[用户发来文件: %s]\n```\n%s\n```' % (fname, body)
                    blocks.append({'type': 'text', 'text': text})
                    stats.file_injections.append({
                        'url': str(fu),
                        'mode': mode,
                        'content_sha256': full_sha,
                        'ref_key': file_ref_key(str(fu), full_sha),
                        'tokens_estimate': estimate_tokens(text),
                    })

        if is_ai and _row_get(r, 'tool_calls'):
            small, large, per_message, _ = _tool_caps()
            th = format_tool_history(
                _row_get(r, 'tool_calls'),
                cap_small=small,
                cap_large=large,
                cap_per_message=per_message,
            )
            if th:
                blocks.append({'type': 'text', 'text': th})

        if not blocks:
            continue

        content: Any = blocks[0]['text'] if len(blocks) == 1 and blocks[0]['type'] == 'text' else blocks
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

    if history_token_budget > 0:
        msgs, msg_trimmed = trim_messages_to_text_budget(
            msgs, budget=history_token_budget, estimate_tokens=estimate_tokens,
        )
        if msg_trimmed:
            stats.conversation_content_trimmed = True

    msgs, _tool_trimmed = apply_history_tool_budget(msgs, estimate_tokens=estimate_tokens)
    if _tool_trimmed:
        stats.tool_history_trimmed = True

    stats.history_trimmed = stats.conversation_content_trimmed or stats.tool_history_trimmed
    stats.rendered_text_tokens_estimate = sum(
        estimate_message_text_tokens(m.get('content'), estimate_tokens) for m in msgs
    )
    return msgs, stats
