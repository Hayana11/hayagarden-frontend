"""The 出生 breakdown: each step between her message and the reply's first word.

Unit tests for chat.turn_birth, the Daily runtime's detail labels, and the full
/chat/stream Daily flow (real prepare + assembly, fake resident): the `birth`
event goes out once, before the first word, and is kept with the reply.
"""
from __future__ import annotations

import types
import unittest
from unittest import mock

from chat import daily_runtime as dr
from chat import turn_birth
from tests.test_memory_surface_daily import _Flow


class SummarizeTests(unittest.TestCase):
    def test_rows_follow_real_order_and_add_up_to_the_total(self):
        diagnostic = {
            'birth_started_at': 100.0,
            'request_received_at': 100.4,
            'turn_kind': 'hot',
            'phases': [
                # recorded out of order on purpose
                {'key': 'proof', 'label': '上下文核对', 'start': 102.0, 'end': 110.0, 'detail': '通过'},
                {'key': 'persona', 'label': '载入人设', 'start': 100.0, 'end': 100.3, 'detail': '人设 4.1k 字'},
                {'key': 'history', 'label': '载入最近对话', 'start': 100.5, 'end': 101.0, 'detail': None},
            ],
            'ensure_alive_end_at': 110.2,
            'stdin_flush_at': 110.5,
        }
        summary = turn_birth.summarize(diagnostic, first_content_at=114.0)
        self.assertEqual(summary['total_ms'], 14000)
        self.assertEqual(summary['turn_kind'], 'hot')
        self.assertEqual(
            [(r['key'], r['ms']) for r in summary['phases']],
            [('persona', 300), ('other', 200), ('history', 500), ('other', 1000),
             ('proof', 8000), ('other', 200), ('deliver', 300), ('first_word', 3500)],
        )
        self.assertEqual(sum(r['ms'] for r in summary['phases']), summary['total_ms'])
        self.assertEqual(summary['phases'][0]['detail'], '人设 4.1k 字')

    def test_cold_start_splits_tool_connection_from_the_first_word(self):
        diagnostic = {
            'request_received_at': 10.0,
            'phases': [{'key': 'spawn', 'label': '启动 Claude', 'start': 10.0, 'end': 12.0, 'detail': '新进程'}],
            'ensure_alive_end_at': 12.0,
            'stdin_flush_at': 12.1,
            'claude_init_event_at': 30.1,
        }
        rows = turn_birth.summarize(diagnostic, first_content_at=34.1)['phases']
        self.assertEqual([r['key'] for r in rows], ['spawn', 'deliver', 'tools', 'first_word'])
        self.assertEqual(rows[2]['ms'], 18000)  # MCP startup, the part she waits on
        self.assertEqual(rows[3]['ms'], 4000)

    def test_small_gaps_are_not_rows_and_overlaps_never_double_count(self):
        diagnostic = {
            'request_received_at': 0.0,
            'phases': [
                {'key': 'a', 'label': 'A', 'start': 0.0, 'end': 1.0, 'detail': None},
                {'key': 'b', 'label': 'B', 'start': 0.5, 'end': 2.0, 'detail': None},  # overlaps A
                {'key': 'c', 'label': 'C', 'start': 2.05, 'end': 3.0, 'detail': None},  # 50 ms gap
            ],
        }
        rows = turn_birth.summarize(diagnostic, first_content_at=3.0)['phases']
        self.assertEqual([(r['key'], r['ms']) for r in rows], [('a', 1000), ('b', 1000), ('c', 950)])

    def test_no_start_no_summary(self):
        self.assertIsNone(turn_birth.summarize({'phases': []}))
        self.assertIsNone(turn_birth.summarize(None))

    def test_phase_records_detail_and_never_raises(self):
        diagnostic = {}
        with turn_birth.phase(diagnostic, 'memory', '想起记忆') as box:
            box['detail'] = '浮现 1 条'
        [row] = diagnostic['phases']
        self.assertEqual((row['key'], row['label'], row['detail']), ('memory', '想起记忆', '浮现 1 条'))
        with turn_birth.phase(None, 'x', 'X'):  # no record: still runs the block
            pass
        with self.assertRaises(ValueError):  # the block's own error is not swallowed
            with turn_birth.phase(diagnostic, 'boom', 'Boom'):
                raise ValueError('boom')
        self.assertEqual(diagnostic['phases'][-1]['key'], 'boom')  # but the time is kept

    def test_phase_count_is_bounded(self):
        diagnostic = {}
        for i in range(turn_birth.MAX_PHASES + 5):
            turn_birth.record_phase(diagnostic, 'k%d' % i, 'K', float(i), float(i) + 0.5)
        self.assertEqual(len(diagnostic['phases']), turn_birth.MAX_PHASES)


class DetailLabelTests(unittest.TestCase):
    def test_history_detail_counts_sizes_never_text(self):
        assembly = {'layers': [
            {'kind': 'static', 'text': 'x' * 9000},
            {'kind': 'day_handoff', 'text': 'h' * 500},
            {'kind': 'carryover', 'messages': [{'content': 'a' * 100}] * 6},
            {'kind': 'current_day_history', 'messages': [{'content': 'b' * 100}] * 24},
        ]}
        self.assertEqual(dr._history_timing_detail(assembly), '今天 24 条 · 延续 6 条 · 交接 · 3.5k 字')
        self.assertEqual(dr._history_timing_detail({}), '今天 0 条 · 0 字')

    def test_memory_and_chunk_details(self):
        self.assertEqual(dr._memory_timing_detail({'mode': 'on', 'decision': 'surfaced'}), '浮现 1 条')
        self.assertEqual(dr._memory_timing_detail({'mode': 'shadow', 'decision': 'surfaced'}), '没有浮现')
        self.assertEqual(dr._memory_timing_detail({'mode': 'off'}), '未开启')
        self.assertEqual(dr._chunks_timing_detail({'c1': 'x' * 800, 'c2': 'y' * 700}), '2 块 · 1.5k 字')

    def test_proof_end_stamp_records_a_row_with_its_decision(self):
        plan = types.SimpleNamespace(cold_start_diagnostic={'request_received_at': 1.0})
        dr._turn_timing_mark(plan, 'hot_context_plan_start_at')
        dr._turn_timing_mark(plan, 'hot_context_plan_end_at', decision='NO_OP')
        [row] = plan.cold_start_diagnostic['phases']
        self.assertEqual((row['key'], row['label'], row['detail']), ('proof', '上下文核对', '通过'))
        dr._turn_timing_mark(plan, 'capacity_swap_start_at')
        dr._turn_timing_mark(plan, 'capacity_swap_end_at')
        self.assertEqual(plan.cold_start_diagnostic['phases'][-1]['label'], '容量切换')


class StreamTests(_Flow):
    """The real /chat/stream Daily flow sends and keeps the birth breakdown."""

    def test_birth_goes_out_before_the_first_word_and_stays_with_the_reply(self):
        _uid, events = self.turn('想吃希腊菜了')
        kinds = [e.get('t') for e in events]
        self.assertEqual(kinds.count('birth'), 1)
        first_word = min(i for i, k in enumerate(kinds) if k in ('think', 'text'))
        self.assertLess(kinds.index('birth'), first_word)

        birth = next(e['d'] for e in events if e.get('t') == 'birth')
        keys = [row['key'] for row in birth['phases']]
        for key in ('persona', 'claim', 'history', 'memory', 'spawn'):
            self.assertIn(key, keys)
        self.assertLess(keys.index('persona'), keys.index('claim'))
        self.assertLess(keys.index('claim'), keys.index('history'))
        spawn = next(row for row in birth['phases'] if row['key'] == 'spawn')
        self.assertEqual((spawn['label'], spawn['detail']), ('启动 Claude', '新进程'))  # first turn is cold
        self.assertEqual(birth['turn_kind'], 'cold')
        self.assertTrue(next(r for r in birth['phases'] if r['key'] == 'persona')['detail'].startswith('人设 '))
        self.assertLessEqual(sum(row['ms'] for row in birth['phases']), birth['total_ms'] + len(keys))

        self.assertEqual(self.cache_info()['birth'], birth)  # history can open it after a reload

    def test_hot_turn_says_the_resident_was_reused(self):
        self.turn('想吃希腊菜了')
        _uid, events = self.turn('再说说赫泰拉', created_at='2026-07-27 10:05:00')
        birth = next(e['d'] for e in events if e.get('t') == 'birth')
        self.assertEqual(birth['turn_kind'], 'hot')
        spawn = next(row for row in birth['phases'] if row['key'] == 'spawn')
        self.assertEqual(spawn['label'], '确认常驻进程')

    def test_no_breakdown_means_no_event_and_the_reply_is_unaffected(self):
        with mock.patch('chat.turn_birth.summarize', return_value=None):
            _uid, events = self.turn('想吃希腊菜了')
        self.assertFalse(any(e.get('t') == 'birth' for e in events))
        self.assertTrue(any(e.get('t') in ('think', 'text') for e in events))
        self.assertNotIn('birth', self.cache_info())


if __name__ == '__main__':
    unittest.main()
