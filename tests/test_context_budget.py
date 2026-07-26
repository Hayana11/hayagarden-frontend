"""Tests for chat.context_budget helpers."""

from __future__ import annotations

import unittest

from chat.context_budget import (
    filter_state_dict_for_turn,
    file_revisit_summary,
    trim_rows_to_token_budget,
    trim_tool_history_lines,
)
from tools.cc_usage_observability import estimate_tokens_heuristic_cjk1_ascii4_v1 as est


class ContextBudgetTests(unittest.TestCase):
    def test_filter_state_skips_unchanged_volatile_without_relevance(self):
        last = {
            'time_bucket': '当前时间段：上午 左右',
            'lights': '主灯关',
            'ledger': '支出100',
        }
        new = dict(last)
        new['time_bucket'] = '当前时间段：中午 左右'
        filtered = filter_state_dict_for_turn(
            new,
            user_text='你好呀',
            last_snapshot=last,
            is_cold=False,
        )
        self.assertNotIn('lights', filtered)
        self.assertNotIn('ledger', filtered)
        self.assertNotIn('time_bucket', filtered)

    def test_filter_state_keeps_volatile_when_user_relevant(self):
        last = {'lights': '主灯关'}
        new = {'lights': '主灯关', 'ledger': '支出100'}
        filtered = filter_state_dict_for_turn(
            new,
            user_text='床头灯还亮着吗',
            last_snapshot=last,
            is_cold=False,
        )
        self.assertIn('lights', filtered)

    def test_trim_rows_to_token_budget_keeps_newest(self):
        rows = [{'id': i, 'text': 'x' * 40} for i in range(10)]
        kept, total = trim_rows_to_token_budget(
            rows,
            budget=120,
            text_fn=lambda r: r['text'],
            estimate_tokens=est,
        )
        self.assertLessEqual(total, 120)
        self.assertEqual([r['id'] for r in kept], list(range(10 - len(kept), 10)))

    def test_trim_tool_history_lines_respects_total_budget(self):
        lines = [
            '[上一轮我调用的工具与结果]',
            '· tool1 {}\n  → {}'.format('{}', 'a' * 2000),
            '· tool2 {}\n  → {}'.format('{}', 'b' * 2000),
        ]
        out = trim_tool_history_lines(lines, total_budget=500, estimate_tokens=est)
        self.assertEqual(out[0], lines[0])
        self.assertLessEqual(est('\n'.join(out)), 500)

    def test_file_revisit_summary_truncates(self):
        body = 'A' * 1000
        summary = file_revisit_summary(body, preview_chars=100)
        self.assertIn('此前已全文注入', summary)
        self.assertLess(len(summary), len(body))


if __name__ == '__main__':
    unittest.main()
