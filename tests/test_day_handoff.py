"""Tests for facts-only day handoff builder."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from chat import day_handoff as dh


class DayHandoffBoundaryTests(unittest.TestCase):
    def test_chat_day_window_uses_four_am_boundary(self):
        day, start, end = dh.chat_day_window('2026-07-26')
        self.assertEqual(day, '2026-07-26')
        self.assertEqual(start, '2026-07-26 04:00:00')
        self.assertEqual(end, '2026-07-27 03:59:59')

    def test_0359_belongs_to_previous_chat_day(self):
        ts = __import__('datetime').datetime(2026, 7, 26, 3, 59, 59)
        self.assertEqual(dh.chat_day_for_timestamp(ts), '2026-07-25')

    def test_0400_belongs_to_new_chat_day(self):
        ts = __import__('datetime').datetime(2026, 7, 26, 4, 0, 0)
        self.assertEqual(dh.chat_day_for_timestamp(ts), '2026-07-26')

    def test_reject_invalid_day_format(self):
        with self.assertRaises(ValueError):
            dh.validate_day_string('2026/07/26')


class DayHandoffExtractionTests(unittest.TestCase):
    def _rows(self):
        return [
            {'id': 1, 'author': 'hayana', 'content': '咪咪喵喵地跑来跑去', 'created_at': '2026-07-26 10:00:00'},
            {'id': 2, 'author': 'hayana', 'content': 'PR #142 已经合并并部署了吗？', 'created_at': '2026-07-26 10:01:00'},
            {'id': 3, 'author': 'fyodor', 'content': '还没有，仍是 Draft。', 'created_at': '2026-07-26 10:02:00'},
            {'id': 4, 'author': 'hayana', 'content': '不要给我列建议，只要陪我。', 'created_at': '2026-07-26 10:03:00'},
            {'id': 5, 'author': 'hayana', 'content': '我们决定今晚先不写代码。', 'created_at': '2026-07-26 10:04:00'},
            {'id': 6, 'author': 'fyodor', 'content': '好，先休息。', 'created_at': '2026-07-26 10:05:00'},
        ]

    def test_casual_message_not_confirmed_fact(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        joined = '\n'.join(data.get('confirmed_facts') or [])
        self.assertNotIn('咪咪喵喵', joined)

    def test_answered_question_not_open_loop(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        joined = '\n'.join(data.get('open_loops') or [])
        self.assertNotIn('PR #142', joined)

    def test_request_with不要_passes_validation(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        joined = '\n'.join(data.get('explicit_user_requests') or [])
        self.assertIn('不要', joined)
        self.assertEqual(dh.validate_day_handoff(data), [])

    def test_metadata_flags(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        self.assertEqual(data.get('extraction_mode'), 'conservative_rules')
        self.assertTrue(data.get('requires_human_review'))


class DayHandoffParserTests(unittest.TestCase):
    def _valid_yaml(self) -> str:
        return (
            'day: "2026-07-26"\n'
            'source_day: "2026-07-26"\n'
            'source_start_at: "2026-07-26 04:00:00"\n'
            'source_end_at: "2026-07-27 03:59:59"\n'
            'source_first_message_id: "1"\n'
            'source_last_message_id: "2"\n'
            'source_message_count: "2"\n'
            'source_sha256: "abc"\n'
            'extraction_mode: "conservative_rules"\n'
            'requires_human_review: true\n'
            'topics:\n'
            '  - "疲劳"\n'
            'confirmed_facts:\n'
            '  -\n'
            'decisions:\n'
            '  -\n'
            'open_loops:\n'
            '  -\n'
            'explicit_user_requests:\n'
            '  - "不要给我列建议，只要陪我"\n'
            'last_topic: "用户谈及休息"\n'
        )

    def test_parse_rejects_unknown_key(self):
        text = self._valid_yaml() + 'evil_key: "x"\n'
        with self.assertRaises(ValueError):
            dh.parse_day_handoff_yaml(text)

    def test_parse_rejects_duplicate_key(self):
        text = self._valid_yaml() + 'topics:\n  - "b"\n'
        with self.assertRaises(ValueError):
            dh.parse_day_handoff_yaml(text)

    def test_parse_rejects_missing_required_key(self):
        text = self._valid_yaml().replace('last_topic: "用户谈及休息"\n', '')
        with self.assertRaises(ValueError):
            dh.parse_day_handoff_yaml(text)

    def test_parse_rejects_oversized_file(self):
        text = 'x' * (dh.MAX_FILE_BYTES + 1)
        with self.assertRaises(ValueError):
            dh.parse_day_handoff_yaml(text)


class DayHandoffSecurePathTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patch = mock.patch.object(dh, 'SHADOW_HANDOFF_DIR', self._tmpdir)
        self._patch.start()
        dh.ensure_shadow_handoff_dir()

    def tearDown(self):
        self._patch.stop()

    def _write_valid_file(self, name: str = 'day_handoff_20260726.yaml') -> str:
        data = {
            'day': '2026-07-26',
            'source_day': '2026-07-26',
            'source_start_at': '2026-07-26 04:00:00',
            'source_end_at': '2026-07-27 03:59:59',
            'source_first_message_id': 1,
            'source_last_message_id': 2,
            'source_message_count': 2,
            'source_sha256': 'abc',
            'extraction_mode': 'conservative_rules',
            'requires_human_review': True,
            'topics': ['疲劳'],
            'confirmed_facts': [],
            'decisions': [],
            'open_loops': [],
            'explicit_user_requests': ['不要给我列建议，只要陪我'],
            'last_topic': '用户谈及休息',
        }
        path = dh.write_day_handoff_to_tmp(data)
        return path

    def test_write_uses_secure_directory_and_permissions(self):
        path = self._write_valid_file()
        self.assertTrue(path.startswith(self._tmpdir))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode, stat.S_IRUSR | stat.S_IWUSR)

    def test_reject_path_outside_shadow_dir(self):
        with self.assertRaises(ValueError):
            dh.resolve_shadow_handoff_path('/etc/passwd')

    def test_reject_symlink(self):
        real = self._write_valid_file()
        link = os.path.join(self._tmpdir, 'day_handoff_link.yaml')
        os.symlink(real, link)
        with self.assertRaises(ValueError):
            dh.resolve_shadow_handoff_path(link)

    def test_refuse_overwrite_existing_file(self):
        self._write_valid_file()
        with self.assertRaises(FileExistsError):
            self._write_valid_file()


if __name__ == '__main__':
    unittest.main()
