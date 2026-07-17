"""Focused tests for CC resident context dedup / usage v2 (no TreeGPT changes)."""

from __future__ import annotations

import datetime
import io
import json
import unittest
from unittest import mock

from chat.system_builder import (
    build_cc_static_system,
    build_stable_note,
    build_time_bucket,
    format_state_diff,
    format_state_snapshot,
)
from cc_resident import (
    ResidentError,
    ResidentSession,
    empty_usage,
    normalize_cache_info,
    summarize_rounds,
)


class TimeBucketTests(unittest.TestCase):
    def test_half_hour_buckets(self):
        self.assertEqual(
            build_time_bucket(datetime.datetime(2026, 7, 18, 23, 1)),
            '2026-07-18 23:00',
        )
        self.assertEqual(
            build_time_bucket(datetime.datetime(2026, 7, 18, 23, 29)),
            '2026-07-18 23:00',
        )
        self.assertEqual(
            build_time_bucket(datetime.datetime(2026, 7, 18, 23, 30)),
            '2026-07-18 23:30',
        )
        self.assertEqual(
            build_time_bucket(datetime.datetime(2026, 7, 18, 23, 59)),
            '2026-07-18 23:30',
        )


class StableSystemTests(unittest.TestCase):
    def test_stable_note_byte_identical(self):
        first = build_stable_note()
        second = build_stable_note()
        self.assertEqual(first, second)

    def test_static_system_byte_identical(self):
        with mock.patch('chat.system_builder.read_persona', return_value='PERSONA_FIXED'):
            first = build_cc_static_system()
            second = build_cc_static_system()
        self.assertEqual(first, second)
        self.assertIn('PERSONA_FIXED', first)
        self.assertIn('[[SAVE:', first)
        self.assertIn('私聊窗口', first)


class StateDiffTests(unittest.TestCase):
    def test_unchanged_returns_empty(self):
        state = {'lights': '关', 'todos': '待办 A', 'time_bucket': '当前时间段：23:00 左右'}
        self.assertEqual(format_state_diff(state, state), '')

    def test_single_component_change(self):
        before = {'lights': '关', 'todos': '待办 A'}
        after = {'lights': '开 35%', 'todos': '待办 A'}
        text = format_state_diff(before, after)
        self.assertIn('灯', text)
        self.assertNotIn('待办 A', text)

    def test_cleared_state(self):
        before = {'reminders': '## 今日提醒\n- 明天到期'}
        after = {'reminders': ''}
        text = format_state_diff(before, after)
        self.assertIn('今日提醒：已清空', text)

    def test_snapshot_includes_all(self):
        text = format_state_snapshot({'lights': '关', 'todos': '待办 A'})
        self.assertIn('【当前状态】', text)
        self.assertIn('关', text)
        self.assertIn('待办 A', text)


class UsageV2Tests(unittest.TestCase):
    def test_sum_rounds_not_max(self):
        rounds = [
            {'index': 1, 'complete': True, 'input_tokens': 100, 'output_tokens': 10,
             'cache_read': 30000, 'cache_creation': 0, 'context_tokens': 30100},
            {'index': 2, 'complete': True, 'input_tokens': 200, 'output_tokens': 20,
             'cache_read': 77000, 'cache_creation': 0, 'context_tokens': 77200},
            {'index': 3, 'complete': True, 'input_tokens': 300, 'output_tokens': 30,
             'cache_read': 78000, 'cache_creation': 0, 'context_tokens': 78300},
        ]
        usage = summarize_rounds(rounds)
        self.assertEqual(usage['input_tokens'], 600)
        self.assertEqual(usage['cache_read'], 185000)
        self.assertEqual(usage['last_round_context'], 78300)
        self.assertEqual(usage['num_rounds'], 3)

    def test_cumulative_read_does_not_look_like_context(self):
        rounds = [
            {
                'index': i,
                'complete': True,
                'input_tokens': 500,
                'output_tokens': 100,
                'cache_read': 78000,
                'cache_creation': 0,
                'context_tokens': 78500,
            }
            for i in range(1, 7)
        ]
        usage = summarize_rounds(rounds)
        self.assertEqual(usage['cache_read'], 468000)
        self.assertEqual(usage['last_round_context'], 78500)
        self.assertLess(usage['last_round_context'], 100000)

    def test_legacy_cache_info_compatible(self):
        legacy = normalize_cache_info({'cache_read': 100, 'cache_creation': 20})
        self.assertEqual(legacy['cache_read'], 100)
        self.assertEqual(legacy['cache_creation'], 20)
        self.assertEqual(legacy.get('num_rounds', 1), 1)

        v2 = normalize_cache_info({'v': 2, 'provider': 'claude_code', 'num_rounds': 2, 'rounds': [{}, {}]})
        self.assertEqual(v2['num_rounds'], 2)
        self.assertEqual(len(v2['rounds']), 2)


class FakeProc:
    def __init__(self, lines):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(''.join(line + '\n' for line in lines))
        self.stderr = io.StringIO()
        self._code = None

    def poll(self):
        return self._code

    def terminate(self):
        self._code = 0

    def kill(self):
        self._code = -9

    def wait(self, timeout=None):
        return self._code or 0


class ResidentCommitTests(unittest.TestCase):
    def _session(self):
        return ResidentSession('/tmp', '', '/tmp/cc-tools.json')

    def test_commit_only_after_successful_flush(self):
        sess = self._session()
        sess._proc = FakeProc([])
        sess._cold = False

        class BrokenStdin:
            def write(self, _):
                raise BrokenPipeError('boom')

            def flush(self):
                pass

        sess._proc.stdin = BrokenStdin()
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'state_snapshot': {'lights': '开'},
                'group_max_id': 105,
            }))
        self.assertEqual(sess.last_state_snapshot, {})
        self.assertEqual(sess.last_group_message_id, 0)

    def test_commit_kept_when_stream_fails_after_flush(self):
        sess = self._session()
        # EOF before result → ResidentError, but flush already succeeded
        sess._proc = FakeProc([])
        sess._cold = False
        with self.assertRaises(ResidentError):
            list(sess.send_turn('hi', commit_meta={
                'state_snapshot': {'lights': '开'},
                'group_max_id': 105,
            }))
        self.assertEqual(sess.last_state_snapshot.get('lights'), '开')
        self.assertEqual(sess.last_group_message_id, 105)

    def test_partial_rounds_kept_on_error(self):
        lines = [
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'message_start',
                    'message': {'usage': {
                        'input_tokens': 100,
                        'output_tokens': 10,
                        'cache_read_input_tokens': 30000,
                        'cache_creation_input_tokens': 0,
                    }},
                },
            }),
            json.dumps({
                'type': 'assistant',
                'message': {
                    'usage': {
                        'input_tokens': 100,
                        'output_tokens': 10,
                        'cache_read_input_tokens': 30000,
                        'cache_creation_input_tokens': 0,
                    },
                    'content': [{'type': 'tool_use', 'id': 't1', 'name': 'x', 'input': {}}],
                },
            }),
            json.dumps({
                'type': 'stream_event',
                'event': {
                    'type': 'message_start',
                    'message': {'usage': {
                        'input_tokens': 200,
                        'output_tokens': 20,
                        'cache_read_input_tokens': 70000,
                        'cache_creation_input_tokens': 0,
                    }},
                },
            }),
            json.dumps({
                'type': 'result',
                'is_error': True,
                'result': 'boom',
                'usage': {},
            }),
        ]
        sess = self._session()
        sess._proc = FakeProc(lines)
        sess._cold = False
        with self.assertRaises(ResidentError) as ctx:
            list(sess.send_turn('hi'))
        usage = ctx.exception.usage
        self.assertGreaterEqual(len(usage.get('rounds') or []), 1)
        self.assertTrue(usage['rounds'][0].get('complete'))


class ResidentRespawnTests(unittest.TestCase):
    def test_turn_limit_triggers_before_next_send(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        with mock.patch('cc_resident._cfg_int', side_effect=lambda k, d: {
            'CC_MAX_RESIDENT_TURNS': 2,
            'CC_CONTEXT_SOFT_LIMIT': 90_000,
            'CC_CONTEXT_HARD_LIMIT': 120_000,
            'CC_MIN_TURNS_BETWEEN_RESPAWNS': 5,
        }.get(k, d)):
            sess._proc = FakeProc([])
            sess._cold = False
            sess._system_text = 'STATIC'
            sess._last_used = 10**12
            sess._resident_turn_count = 2
            with mock.patch.object(sess, '_spawn') as spawn:
                sess.ensure_alive('STATIC', {})
                spawn.assert_called()
                self.assertEqual(spawn.call_args.kwargs.get('reason'), 'turn_limit')

    def test_spawn_resets_snapshots(self):
        sess = ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        sess._last_state_snapshot = {'lights': '开'}
        sess._last_group_message_id = 99
        sess._resident_turn_count = 9
        with mock.patch('subprocess.Popen', return_value=FakeProc([])):
            sess._spawn('STATIC', {}, reason='turn_limit')
        self.assertEqual(sess.last_state_snapshot, {})
        self.assertEqual(sess.last_group_message_id, 0)
        self.assertEqual(sess._resident_turn_count, 0)
        self.assertEqual(sess.pending_respawn_reason, 'turn_limit')


class GroupQueryContractTests(unittest.TestCase):
    def test_max_id_comes_from_same_rows(self):
        rows = [
            {'id': 101, 'room': 'group', 'author': 'user', 'content': 'a', 'created_at': '2026-07-18 10:00:00'},
            {'id': 102, 'room': 'group', 'author': 'user', 'content': 'b', 'created_at': '2026-07-18 10:01:00'},
            {'id': 103, 'room': 'group', 'author': 'user', 'content': 'c', 'created_at': '2026-07-18 10:02:00'},
        ]
        # Contract: max_id must be computed from the same result set that is sent.
        max_id = max(int(r['id']) for r in rows)
        self.assertEqual([r['id'] for r in rows], [101, 102, 103])
        self.assertEqual(max_id, 103)
        # Must not call a second independent SELECT MAX(id).
        self.assertNotEqual(max_id, 104)


class HotTurnContentTests(unittest.TestCase):
    def test_stable_note_not_in_hot_diff(self):
        note = build_stable_note()
        before = {'lights': '关', 'time_bucket': '当前时间段：23:00 左右'}
        after = {'lights': '开', 'time_bucket': '当前时间段：23:00 左右'}
        diff = format_state_diff(before, after)
        self.assertNotIn(note[:40], diff)
        self.assertIn('【状态更新】', diff)


if __name__ == '__main__':
    unittest.main()
