"""Golden parity: legacy path (all CONTEXT_LEAN_* off) vs frozen base 3ffed9ba builder."""
from __future__ import annotations

import datetime
import json
import unittest
from types import SimpleNamespace

from chat.context_continuity import format_tool_history_legacy
from chat.history_boundary import legacy_block_limit
from chat.history_legacy import assemble_legacy_history


def _row(
    mid,
    *,
    author='hayana',
    content='msg',
    image_url='',
    created_at='2026-07-26 12:00:00',
    tool_calls='',
    file_url='',
    file_name='',
):
    return SimpleNamespace(
        id=mid,
        author=author,
        content=content,
        image_url=image_url,
        created_at=created_at,
        tool_calls=tool_calls,
        file_url=file_url,
        file_name=file_name,
    )


def _baseline_assemble(
    rows,
    available_count,
    *,
    static_dir='/tmp',
    read_file_fn=None,
    img_block_fn=None,
    rolling_summary='',
):
    """Frozen provider-visible messages from base 3ffed9ba build_messages."""
    window_base, window_block = 60, 20
    limit = available_count
    if available_count > window_base:
        limit = window_base + ((available_count - window_base) % window_block)
    if limit <= 0:
        limit = window_base
    if len(rows) > limit:
        rows = rows[-limit:]
    total = len(rows)
    img_indices = [i for i, r in enumerate(rows) if r.image_url]
    keep_img_indices = set(img_indices[-2:])
    msgs = []
    prev_dt = None
    for ri, r in enumerate(rows):
        is_ai = r.author in ('fyodor', 'claude', 'assistant')
        role = 'assistant' if is_ai else 'user'
        note = ''
        cur_dt = None
        try:
            cur_dt = datetime.datetime.strptime(r.created_at, '%Y-%m-%d %H:%M:%S')
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
        if r.image_url:
            if ri in keep_img_indices:
                blk = img_block_fn(r.image_url) if img_block_fn else None
                if blk:
                    blocks.append(blk)
            else:
                blocks.append({'type': 'text', 'text': '[一张较早发送的图片，内容已不在上下文中]'})
        if r.content:
            blocks.append({'type': 'text', 'text': note + r.content})
        fu = r.file_url or ''
        if fu and not is_ai and ri >= total - 6 and str(fu).startswith('/static/'):
            body = read_file_fn(static_dir, fu) if read_file_fn else None
            if body is not None:
                if len(body) > 30000:
                    body = body[:30000] + '\n...(文件过长已截断)'
                blocks.append({
                    'type': 'text',
                    'text': '[用户发来文件: %s]\n```\n%s\n```' % (r.file_name or '附件', body),
                })
        if is_ai and r.tool_calls:
            th = format_tool_history_legacy(r.tool_calls)
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
    if available_count > limit and (rolling_summary or '').strip():
        pre = '[更早对话的连续性摘要（滞出当前窗口的部分）]\n' + rolling_summary.strip()
        if msgs and msgs[0]['role'] == 'user':
            c0 = msgs[0]['content']
            if isinstance(c0, str):
                msgs[0]['content'] = pre + '\n\n' + c0
            else:
                msgs[0]['content'] = [{'type': 'text', 'text': pre}] + c0
        else:
            msgs.insert(0, {'role': 'user', 'content': pre})
    if not msgs or msgs[0]['role'] == 'assistant':
        msgs.insert(0, {'role': 'user', 'content': '...'})
    return msgs


def _legacy_assemble(rows, available_count, *, rolling_summary='', files=None):
    limit = legacy_block_limit(available_count)
    fetch_rows = rows[-limit:] if len(rows) > limit else list(rows)
    files = files or {}
    msgs, stats = assemble_legacy_history(
        fetch_rows,
        available_count=available_count,
        static_dir='/tmp',
        read_file_fn=lambda _sd, url: files.get(url),
        img_block_fn=lambda url: {'type': 'image', 'source': {'type': 'base64', 'data': url}},
        format_tool_history_fn=format_tool_history_legacy,
        is_ai_author=lambda a: a in ('fyodor', 'claude', 'assistant'),
    )
    if available_count > legacy_block_limit(available_count) and (rolling_summary or '').strip():
        pre = '[更早对话的连续性摘要（滞出当前窗口的部分）]\n' + rolling_summary.strip()
        if msgs and msgs[0]['role'] == 'user':
            c0 = msgs[0]['content']
            if isinstance(c0, str):
                msgs[0]['content'] = pre + '\n\n' + c0
            else:
                msgs[0]['content'] = [{'type': 'text', 'text': pre}] + c0
        else:
            msgs.insert(0, {'role': 'user', 'content': pre})
    if not msgs or msgs[0]['role'] == 'assistant':
        msgs.insert(0, {'role': 'user', 'content': '...'})
    _ = stats
    return msgs


class ContextLeanGoldenTests(unittest.TestCase):
    def _assert_parity(self, rows, available_count, **kwargs):
        files = kwargs.pop('files', None)
        if files and 'read_file_fn' not in kwargs:
            kwargs['read_file_fn'] = lambda _sd, url: files.get(url)
        if 'img_block_fn' not in kwargs:
            kwargs['img_block_fn'] = lambda url: {'type': 'image', 'source': {'type': 'base64', 'data': url}}
        expected = _baseline_assemble(rows, available_count, **kwargs)
        actual = _legacy_assemble(
            rows, available_count,
            rolling_summary=kwargs.get('rolling_summary', ''),
            files=files or {},
        )
        self.assertEqual(expected, actual)

    def test_under_sixty_messages(self):
        rows = [_row(i, content='m%d' % i) for i in range(1, 51)]
        self._assert_parity(rows, 50)

    def test_sixty_one_to_seventy_nine_growth(self):
        rows = [_row(i, content='g%d' % i) for i in range(1, 72)]
        self._assert_parity(rows, 71)

    def test_eighty_with_rolling_summary(self):
        rows = [_row(i, content='b%d' % i) for i in range(1, 81)]
        self._assert_parity(rows, 80, rolling_summary='早前聊过天气和晚饭。')

    def test_hundred_with_rolling_summary(self):
        rows = [_row(i, content='h%d' % i) for i in range(1, 101)]
        self._assert_parity(rows, 100, rolling_summary='长对话摘要。')

    def test_images_keep_last_two(self):
        rows = [
            _row(1, image_url='/static/a.jpg', content=''),
            _row(2, content='text'),
            _row(3, image_url='/static/b.jpg', content=''),
            _row(4, image_url='/static/c.jpg', content=''),
        ]
        self._assert_parity(rows, 4)

    def test_recent_six_files(self):
        rows = [
            _row(i, file_url='/static/f%d.txt' % i, file_name='f%d.txt' % i, content='[文件]')
            for i in range(1, 9)
        ]
        files = {'/static/f%d.txt' % i: 'body-%d' % i for i in range(1, 9)}
        self._assert_parity(rows, 8, files=files)

    def test_tool_history_over_twelve_thousand_without_per_message_trim(self):
        big = 'x' * 7000
        calls = json.dumps([
            {'name': 'search', 'args': {}, 'result': big, 'success': True},
            {'name': 'search', 'args': {}, 'result': big, 'success': True},
        ])
        rows = [_row(1, author='assistant', content='', tool_calls=calls)]
        self._assert_parity(rows, 1)

    def test_consecutive_same_role_merge(self):
        rows = [
            _row(1, author='hayana', content='a'),
            _row(2, author='hayana', content='b'),
            _row(3, author='assistant', content='c'),
            _row(4, author='assistant', content='d'),
        ]
        self._assert_parity(rows, 4)

    def test_time_gap_prefix(self):
        rows = [
            _row(1, created_at='2026-07-26 10:00:00', content='早'),
            _row(2, created_at='2026-07-26 11:00:00', content='晚'),
        ]
        self._assert_parity(rows, 2)


if __name__ == '__main__':
    unittest.main()
