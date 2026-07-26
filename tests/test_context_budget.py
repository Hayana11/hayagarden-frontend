"""Tests for chat.context_budget helpers."""

from __future__ import annotations

import unittest

from chat.context_budget import (
    build_state_send_payload,
    file_content_sha256,
    file_ref_key,
    format_state_for_send,
    merge_cumulative_state_send,
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

    def test_time_only_change_not_sent_for_continue(self):
        before = {'time_bucket': '当前时间段：12:00 左右', 'lights': '关'}
        after = {'time_bucket': '当前时间段：12:30 左右', 'lights': '关'}
        send = build_state_send_payload(before, after, user_text='继续', is_cold=False)
        text, mode = format_state_for_send(
            merge_cumulative_state_send({}, before),
            send,
            is_cold=False,
        )
        self.assertEqual(send, {})
        self.assertEqual(text, '')
        self.assertEqual(mode, 'none')

    def test_time_only_change_sent_when_user_asks_time(self):
        before = {'time_bucket': '当前时间段：12:00 左右'}
        after = {'time_bucket': '当前时间段：12:30 左右'}
        send = build_state_send_payload(before, after, user_text='现在几点了', is_cold=False)
        text, mode = format_state_for_send(
            merge_cumulative_state_send({}, before),
            send,
            is_cold=False,
        )
        self.assertIn('time_bucket', send)
        self.assertIn('12:30', text)
        self.assertEqual(mode, 'delta')

    def test_lights_change_includes_latest_time_bucket(self):
        before = {'time_bucket': '当前时间段：12:00 左右', 'lights': '关'}
        after = {'time_bucket': '当前时间段：12:30 左右', 'lights': '开'}
        send = build_state_send_payload(before, after, user_text='继续', is_cold=False)
        self.assertIn('lights', send)
        self.assertIn('time_bucket', send)

    def test_file_ref_key_uses_content_sha256(self):
        body = 'hello file'
        sha = file_content_sha256(body)
        self.assertEqual(file_ref_key('/static/a.txt', sha), '/static/a.txt|%s' % sha)


if __name__ == '__main__':
    unittest.main()
