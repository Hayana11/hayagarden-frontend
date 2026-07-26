"""Tests for history assembly, rolling summary gating, and tool budgets."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from chat.context_continuity import format_tool_history
from chat.history_assembly import (
    apply_history_tool_budget,
    assemble_history_from_rows,
    committed_file_refs_in_messages,
    inject_rolling_summary_and_enforce_budget,
    trim_messages_to_text_budget,
)


def _row(author, content, **kwargs):
    base = {
        'author': author,
        'content': content,
        'image_url': '',
        'created_at': '2026-07-26 12:00:00',
        'tool_calls': '',
        'file_url': '',
        'file_name': '',
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _estimate(text):
    return len(text or '')


class HistoryAssemblyTests(unittest.TestCase):
    def test_no_trim_no_rolling_flag_for_small_history(self):
        rows = [_row('hayana' if i % 2 == 0 else 'assistant', f'msg {i}') for i in range(10)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=10,
            history_token_budget=50000,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertGreaterEqual(len(msgs), 5)
        self.assertFalse(stats.conversation_content_trimmed)
        self.assertFalse(stats.tool_history_trimmed)
        self.assertEqual(stats.rows_before_trim, stats.rows_after_trim)

    def test_large_history_within_budget_not_trimmed(self):
        rows = [_row('hayana', '短消息') for _ in range(80)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=80,
            history_token_budget=50000,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertEqual(stats.rows_after_trim, 80)
        self.assertFalse(stats.conversation_content_trimmed)

    def test_token_budget_trims_old_rows(self):
        rows = [_row('hayana' if i % 2 == 0 else 'assistant', 'x' * 500) for i in range(80)]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=80,
            history_token_budget=3000,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
        )
        self.assertTrue(stats.conversation_content_trimmed)
        self.assertLess(stats.rows_after_trim, stats.rows_before_trim)

    def test_conversation_content_trimmed_when_history_exceeds_budget(self):
        rows = [
            _row('hayana', 'A' * 1200, created_at='2026-07-26 11:00:00'),
            _row('assistant', 'B' * 1200, created_at='2026-07-26 11:05:00'),
            _row('hayana', 'C' * 1200, created_at='2026-07-26 12:30:00'),
            _row('assistant', 'D' * 1200, created_at='2026-07-26 12:35:00'),
            _row('hayana', 'latest user', created_at='2026-07-26 13:00:00'),
        ]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=5,
            history_token_budget=1200,
            static_dir='/tmp',
            read_file_fn=lambda *_a, **_k: None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            estimate_tokens=_estimate,
        )
        self.assertTrue(stats.conversation_content_trimmed)
        self.assertFalse(stats.tool_history_trimmed)
        self.assertIn('latest user', json.dumps(msgs, ensure_ascii=False))

    def test_tool_only_trim_sets_tool_history_flag_in_assembly(self):
        calls = json.dumps([
            {'name': 'web_search', 'args': {}, 'result': 'a' * 20000, 'success': True},
            {'name': 'web_search', 'args': {}, 'result': 'b' * 20000, 'success': True},
        ])
        rows = [
            _row('assistant', 'reply', tool_calls=calls),
            _row('hayana', 'latest user'),
        ]
        with mock.patch('chat.history_assembly._tool_caps', return_value=(2000, 8000, 12000, 3000)):
            msgs, stats = assemble_history_from_rows(
                rows,
                available_count=2,
                history_token_budget=50000,
                static_dir='/tmp',
                read_file_fn=lambda *_a, **_k: None,
                img_block_fn=lambda *_a, **_k: None,
                is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            )
        self.assertTrue(stats.tool_history_trimmed)
        self.assertFalse(stats.conversation_content_trimmed)

    def test_tool_only_trim_does_not_set_conversation_content_trimmed(self):
        calls = json.dumps([
            {'name': 'web_search', 'args': {}, 'result': 'a' * 20000, 'success': True},
            {'name': 'web_search', 'args': {}, 'result': 'b' * 20000, 'success': True},
        ])
        text = format_tool_history(calls, cap_small=20000, cap_large=20000, cap_per_message=50000)
        msgs = [
            {'role': 'assistant', 'content': text},
            {'role': 'assistant', 'content': text},
            {'role': 'user', 'content': 'latest'},
        ]
        out, trimmed = apply_history_tool_budget(msgs, total_budget=3000, estimate_tokens=_estimate)
        self.assertTrue(trimmed)
        joined = json.dumps(out, ensure_ascii=False)
        self.assertIn('更早的工具结果已因上下文预算省略', joined)
        self.assertIn('latest', joined)

    def test_history_tool_total_budget_trims_oldest(self):
        calls = json.dumps([
            {'name': 'web_search', 'args': {}, 'result': 'a' * 20000, 'success': True},
            {'name': 'web_search', 'args': {}, 'result': 'b' * 20000, 'success': True},
        ])
        text = format_tool_history(calls, cap_small=20000, cap_large=20000, cap_per_message=50000)
        msgs = [
            {'role': 'assistant', 'content': text},
            {'role': 'assistant', 'content': text},
        ]
        out, trimmed = apply_history_tool_budget(msgs, total_budget=3000)
        self.assertTrue(trimmed)
        joined = json.dumps(out, ensure_ascii=False)
        self.assertIn('更早的工具结果已因上下文预算省略', joined)

    def test_rolling_summary_injected_and_within_budget(self):
        msgs = [
            {'role': 'user', 'content': 'old ' + 'x' * 2000},
            {'role': 'assistant', 'content': 'reply ' + 'y' * 2000},
            {'role': 'user', 'content': 'latest user'},
        ]
        summary = 'summary ' + 'z' * 500
        out, tokens, _ = inject_rolling_summary_and_enforce_budget(
            msgs, rolling_summary=summary, budget=3500, estimate_tokens=_estimate,
        )
        self.assertLessEqual(tokens, 3500)
        joined = json.dumps(out, ensure_ascii=False)
        self.assertIn('连续性摘要', joined)
        self.assertIn('latest user', joined)

    def test_last_user_message_always_kept_on_trim(self):
        msgs = [
            {'role': 'user', 'content': 'a' * 5000},
            {'role': 'assistant', 'content': 'b' * 5000},
            {'role': 'user', 'content': 'must keep'},
        ]
        out, trimmed = trim_messages_to_text_budget(msgs, budget=100, estimate_tokens=_estimate)
        self.assertTrue(trimmed)
        self.assertIn('must keep', json.dumps(out, ensure_ascii=False))

    def test_resident_known_file_uses_url_and_hash(self):
        body = 'full file body'
        from chat.context_budget import file_content_sha256, file_ref_key
        ref = file_ref_key('/static/a.txt', file_content_sha256(body))
        rows = [_row('hayana', 'see file', file_url='/static/a.txt', file_name='a.txt')]
        msgs, stats = assemble_history_from_rows(
            rows,
            available_count=1,
            history_token_budget=50000,
            static_dir='/tmp',
            read_file_fn=lambda _static, url: body if url == '/static/a.txt' else None,
            img_block_fn=lambda *_a, **_k: None,
            is_ai_author=lambda a: a in ('assistant', 'fyodor', 'claude'),
            resident_file_hashes={ref},
        )
        joined = json.dumps(msgs, ensure_ascii=False)
        self.assertIn('此前 resident 已全文注入', joined)
        self.assertEqual(stats.file_injections[0]['mode'], 'resident_summary')

    def test_committed_refs_only_for_full_files_in_output(self):
        injections = [
            {
                'url': '/static/a.txt',
                'mode': 'full',
                'ref_key': '/static/a.txt|abc',
                'content_sha256': 'abc',
            },
            {
                'url': '/static/b.txt',
                'mode': 'marker_only',
                'ref_key': '/static/b.txt|def',
                'content_sha256': 'def',
            },
        ]
        msgs = [{'role': 'user', 'content': '[用户发来文件: a]\n```\nhello\n``` /static/a.txt'}]
        refs = committed_file_refs_in_messages(msgs, injections)
        self.assertEqual(refs, {'/static/a.txt|abc'})


if __name__ == '__main__':
    unittest.main()
