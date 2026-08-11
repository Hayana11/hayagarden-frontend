"""Tests for chat.context_budget helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from chat.context_budget import (
    build_state_send_payload,
    file_content_sha256,
    file_ref_key,
    format_state_for_send,
    state_key_relevant,
)


class ContextBudgetTests(unittest.TestCase):
    def test_user_relevant_lights(self):
        self.assertTrue(state_key_relevant('lights', '床头灯还亮吗'))

    def test_unchanged_volatile_omitted_on_hot(self):
        raw = {'lights': '主灯关', 'emotion': '平静'}
        send = build_state_send_payload(raw, raw, user_text='你好', is_cold=False)
        self.assertNotIn('lights', send)

    def test_user_relevant_resends_lights(self):
        raw = {'lights': '主灯关'}
        send = build_state_send_payload(raw, raw, user_text='灯还亮吗', is_cold=False)
        self.assertIn('lights', send)
        text, mode = format_state_for_send({}, send, is_cold=False)
        self.assertIn('灯', text)
        self.assertEqual(mode, 'delta')

    def test_formatter_has_no_time_bucket_label(self):
        """H1A R2: no 当前时间段 label in state formatter."""
        text, mode = format_state_for_send(
            {},
            {'lights': '关', 'emotion': '平静'},
            is_cold=True,
        )
        self.assertEqual(mode, 'snapshot')
        self.assertNotIn('当前时间段', text)
        self.assertNotIn('time_bucket', text)

    def test_file_ref_key_uses_content_sha256(self):
        body = 'hello file'
        sha = file_content_sha256(body)
        self.assertEqual(file_ref_key('/static/a.txt', sha), '/static/a.txt|%s' % sha)


class NoTimeBucketRuntimeTests(unittest.TestCase):
    def test_collect_state_has_no_time_bucket(self):
        def get_db():
            raise AssertionError('db not needed')

        with (
            patch('urllib.request.urlopen', side_effect=OSError('no')),
            patch('config_store.get_bool', return_value=False),
        ):
            from chat.system_builder import _cc_collect_state
            import chat.system_builder as sb

            state = _cc_collect_state(get_db, lean=True)
        self.assertNotIn('time_bucket', state)
        self.assertFalse(hasattr(sb, 'build_time_bucket'))


if __name__ == '__main__':
    unittest.main()
