"""Tests for chat.context_budget helpers."""

from __future__ import annotations

import unittest

from chat.context_budget import (
    build_state_send_payload,
    format_state_for_send,
    state_key_relevant,
)


class ContextBudgetTests(unittest.TestCase):
    def test_user_relevant_lights(self):
        self.assertTrue(state_key_relevant('lights', '床头灯还亮吗'))

    def test_unchanged_volatile_omitted_on_hot(self):
        raw = {'lights': '主灯关', 'time_bucket': '当前时间段：上午 左右'}
        send = build_state_send_payload(raw, raw, user_text='你好', is_cold=False)
        self.assertNotIn('lights', send)
        self.assertNotIn('time_bucket', send)

    def test_user_relevant_resends_lights(self):
        raw = {'lights': '主灯关'}
        send = build_state_send_payload(raw, raw, user_text='灯还亮吗', is_cold=False)
        self.assertIn('lights', send)
        text, mode = format_state_for_send({}, send, is_cold=False)
        self.assertIn('灯', text)
        self.assertEqual(mode, 'delta')


if __name__ == '__main__':
    unittest.main()
