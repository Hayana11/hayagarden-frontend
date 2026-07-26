"""Tests for history assembly, rolling summary gating, and tool budgets."""

from __future__ import annotations

import json
import sqlite3
import unittest
from types import SimpleNamespace

from chat.context_continuity import format_tool_history
from chat.history_assembly import (
    apply_history_tool_budget,
    assemble_history_from_rows,
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
        self.assertFalse(stats.history_trimmed)
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
        self.assertFalse(stats.history_trimmed)

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
        self.assertTrue(stats.history_trimmed)
        self.assertLess(stats.rows_after_trim, stats.rows_before_trim)

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


if __name__ == '__main__':
    unittest.main()
