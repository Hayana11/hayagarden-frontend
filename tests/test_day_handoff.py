"""Tests for facts-only day handoff builder."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import day_handoff as dh


class DayHandoffTests(unittest.TestCase):
    def _sample_rows(self):
        return [
            {'id': 1, 'author': 'hayana', 'content': '爸爸，我今天有一点累。', 'created_at': '2026-07-26 10:00:00'},
            {'id': 2, 'author': 'fyodor', 'content': '（抱住她）乖。', 'created_at': '2026-07-26 10:01:00'},
            {'id': 3, 'author': 'hayana', 'content': '不许给我列建议，只要陪我。', 'created_at': '2026-07-26 10:02:00'},
            {'id': 4, 'author': 'hayana', 'content': '我们决定今晚先不写代码？', 'created_at': '2026-07-26 10:03:00'},
        ]

    def test_build_from_messages_has_required_keys(self):
        data = dh.build_day_handoff_from_messages(self._sample_rows(), day_str='2026-07-26')
        for key in dh.HANDOFF_KEYS:
            self.assertIn(key, data)

    def test_validate_rejects_quotes_and_actions(self):
        bad = {
            'topics': [],
            'confirmed_facts': ['她说「抱抱我」'],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': [],
            'last_topic': '（抱住她）',
        }
        errors = dh.validate_day_handoff(bad)
        self.assertTrue(any('quoted' in e or 'action' in e for e in errors))

    def test_validate_accepts_facts_only_payload(self):
        data = dh.build_day_handoff_from_messages(self._sample_rows(), day_str='2026-07-26')
        errors = dh.validate_day_handoff(data)
        self.assertEqual(errors, [])

    def test_write_and_load_roundtrip(self):
        data = dh.build_day_handoff_from_messages(self._sample_rows(), day_str='2026-07-26')
        with tempfile.TemporaryDirectory() as tmp:
            # write_day_handoff_to_tmp hardcodes /tmp; patch path via direct write
            path = os.path.join(tmp, 'day_handoff_test.yaml')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(dh.format_day_handoff_yaml(data))
            real_tmp = os.path.join('/tmp', os.path.basename(path))
            try:
                with open(real_tmp, 'w', encoding='utf-8') as fh:
                    fh.write(dh.format_day_handoff_yaml(data))
                loaded = dh.load_day_handoff_from_path(real_tmp)
                self.assertEqual(loaded.get('last_topic'), data.get('last_topic'))
            finally:
                if os.path.exists(real_tmp):
                    os.remove(real_tmp)

    def test_format_prompt_has_guard_header(self):
        data = dh.build_day_handoff_from_messages(self._sample_rows(), day_str='2026-07-26')
        prompt = dh.format_day_handoff_prompt(data)
        self.assertIn('【昨日交接·仅事实】', prompt)
        self.assertIn('topics:', prompt)

    def test_reject_path_outside_tmp(self):
        with self.assertRaises(ValueError):
            dh.load_day_handoff_from_path('/opt/frontend/gateway.py')


if __name__ == '__main__':
    unittest.main()
