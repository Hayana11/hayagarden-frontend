"""Tests for facts-only day handoff builder."""
from __future__ import annotations

import hashlib
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

VALID_SHA256 = hashlib.sha256(b'fixture').hexdigest()
ASSISTANT_VOICE = '我轻轻把她揽进怀里，喵喵地蹭着她的发丝，低声说今晚不写代码了'


def _valid_handoff_data(**overrides):
    data = {
        'day': '2026-07-26',
        'source_day': '2026-07-26',
        'source_start_at': '2026-07-26 04:00:00',
        'source_end_at': '2026-07-27 03:59:59',
        'source_first_message_id': 1,
        'source_last_message_id': 6,
        'source_message_count': 6,
        'source_sha256': VALID_SHA256,
        'extraction_mode': 'conservative_rules',
        'requires_human_review': True,
        'topics': [],
        'confirmed_facts': [],
        'decisions': [],
        'open_loops': [],
        'explicit_user_requests': [],
        'last_topic': '',
    }
    data.update(overrides)
    return data


def _yaml_contains_no_assistant_voice(yaml_text: str) -> bool:
    lowered = yaml_text.lower()
    for marker in ('助手回应', '助手：', '助手:', ASSISTANT_VOICE.lower()):
        if marker in lowered:
            return False
    return True


class DayHandoffBoundaryTests(unittest.TestCase):
    def test_chat_day_window_uses_four_am_boundary(self):
        day, start, end, next_start = dh.chat_day_window('2026-07-26')
        self.assertEqual(day, '2026-07-26')
        self.assertEqual(start, '2026-07-26 04:00:00')
        self.assertEqual(end, '2026-07-27 03:59:59')
        self.assertEqual(next_start, '2026-07-27 04:00:00')

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
            {'id': 3, 'author': 'fyodor', 'content': ASSISTANT_VOICE, 'created_at': '2026-07-26 10:02:00'},
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

    def test_assistant_voice_not_in_decisions_or_last_topic(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        yaml_text = dh.format_day_handoff_yaml(data)
        self.assertTrue(_yaml_contains_no_assistant_voice(yaml_text))
        decisions = '\n'.join(data.get('decisions') or [])
        self.assertNotIn('助手回应', decisions)
        self.assertNotIn(ASSISTANT_VOICE, decisions)
        last_topic = str(data.get('last_topic') or '')
        self.assertNotIn('助手', last_topic)
        self.assertNotIn(ASSISTANT_VOICE, last_topic)

    def test_assistant_voice_not_in_any_yaml_field(self):
        data = dh.build_day_handoff_from_messages(self._rows(), day_str='2026-07-26')
        yaml_text = dh.format_day_handoff_yaml(data)
        self.assertNotIn(ASSISTANT_VOICE, yaml_text)
        self.assertNotIn('助手回应', yaml_text)
        for value in data.values():
            if isinstance(value, list):
                joined = '\n'.join(value)
            else:
                joined = str(value)
            self.assertNotIn(ASSISTANT_VOICE, joined)
            self.assertNotIn('助手回应', joined)


class DayHandoffFetchTests(unittest.TestCase):
    def test_fetch_day_messages_uses_half_open_interval(self):
        executed: list[tuple[str, tuple[str, ...]]] = []

        class FakeCursor:
            def fetchall(self):
                return []

        class FakeConn:
            def execute(self, sql, params=()):
                executed.append((sql, params))
                return FakeCursor()

            def close(self):
                pass

        with mock.patch.object(dh, 'chat_day_window', return_value=(
            '2026-07-26', '2026-07-26 04:00:00', '2026-07-27 03:59:59', '2026-07-27 04:00:00',
        )):
            dh.fetch_day_messages(lambda: FakeConn(), '2026-07-26')

        sql, params = executed[0]
        self.assertIn('created_at >= ?', sql)
        self.assertIn('created_at < ?', sql)
        self.assertNotIn('created_at <= ?', sql)
        self.assertEqual(params, ('2026-07-26 04:00:00', '2026-07-27 04:00:00'))


class DayHandoffValidationTests(unittest.TestCase):
    def test_valid_fixture_passes(self):
        self.assertEqual(dh.validate_day_handoff(_valid_handoff_data()), [])

    def test_missing_metadata_rejected(self):
        for key in dh.HANDOFF_SCALAR_KEYS:
            data = _valid_handoff_data()
            data.pop(key)
            errors = dh.validate_day_handoff(data)
            self.assertTrue(any('missing key: %s' % key in e for e in errors), key)

    def test_invalid_sha256_rejected(self):
        errors = dh.validate_day_handoff(_valid_handoff_data(source_sha256='abc'))
        self.assertTrue(any('source_sha256' in e for e in errors))

    def test_invalid_message_ids_and_count_rejected(self):
        errors = dh.validate_day_handoff(_valid_handoff_data(
            source_first_message_id=10,
            source_last_message_id=1,
            source_message_count=6,
        ))
        self.assertTrue(any('source_first_message_id' in e for e in errors))

        errors = dh.validate_day_handoff(_valid_handoff_data(
            source_first_message_id=1,
            source_last_message_id=2,
            source_message_count=0,
        ))
        self.assertTrue(any('source message ids must be 0 when count is 0' in e for e in errors))

    def test_invalid_time_window_rejected(self):
        errors = dh.validate_day_handoff(_valid_handoff_data(
            source_start_at='2026-07-26 05:00:00',
        ))
        self.assertTrue(any('source_start_at does not match' in e for e in errors))

        errors = dh.validate_day_handoff(_valid_handoff_data(
            source_end_at='2026-07-27 04:00:00',
        ))
        self.assertTrue(any('source_end_at does not match' in e for e in errors))

    def test_wrong_extraction_mode_rejected(self):
        errors = dh.validate_day_handoff(_valid_handoff_data(extraction_mode='loose'))
        self.assertTrue(any('extraction_mode must be conservative_rules' in e for e in errors))

    def test_assistant_voice_in_field_rejected(self):
        errors = dh.validate_day_handoff(_valid_handoff_data(
            last_topic='助手：' + ASSISTANT_VOICE,
        ))
        self.assertTrue(any('assistant voice' in e for e in errors))


class DayHandoffParserTests(unittest.TestCase):
    def _valid_yaml(self) -> str:
        return dh.format_day_handoff_yaml(_valid_handoff_data(
            topics=['疲劳'],
            explicit_user_requests=['不要给我列建议，只要陪我'],
            last_topic='用户谈及休息',
            source_first_message_id=1,
            source_last_message_id=2,
            source_message_count=2,
        ))

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

    def test_parse_rejects_missing_metadata_key(self):
        text = self._valid_yaml().replace('source_sha256: "%s"\n' % VALID_SHA256, '')
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
        data = _valid_handoff_data(
            topics=['疲劳'],
            explicit_user_requests=['不要给我列建议，只要陪我'],
            last_topic='用户谈及休息',
            source_first_message_id=1,
            source_last_message_id=2,
            source_message_count=2,
        )
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

    def test_reject_parent_directory_symlink(self):
        real_dir = tempfile.mkdtemp()
        link_dir = os.path.join(self._tmpdir, 'linked-parent')
        os.symlink(real_dir, link_dir)
        handoff = os.path.join(link_dir, 'day_handoff_20260726.yaml')
        with open(handoff, 'w', encoding='utf-8') as fh:
            fh.write(dh.format_day_handoff_yaml(_valid_handoff_data()))
        with self.assertRaises(ValueError):
            dh.resolve_shadow_handoff_path(handoff)

    def test_read_does_not_follow_symlink_replacement(self):
        real = self._write_valid_file()
        os.remove(real)
        os.symlink('/etc/passwd', real)
        with self.assertRaises((ValueError, OSError)):
            dh.load_day_handoff_from_path(real)

    def test_refuse_overwrite_existing_file(self):
        self._write_valid_file()
        with self.assertRaises(FileExistsError):
            self._write_valid_file()


if __name__ == '__main__':
    unittest.main()
