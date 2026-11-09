"""UH-A0 cold-start timing diagnostics tests without provider traffic."""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cc_resident


class ColdStartDiagnosticTests(unittest.TestCase):
    def test_first_failed_stage_is_stage_specific(self):
        record = {
            'process_spawn_at': 1.0,
            'reached_stdin': True,
            'stdin_flush_at': 2.0,
            'claude_init_event_at': 3.0,
            'first_provider_message_start_at': None,
            'first_thinking_or_text_at': None,
        }
        self.assertEqual(
            cc_resident._cold_start_failure_stage(record),
            'brain_mcp_init_or_provider_startup',
        )
        record['first_provider_message_start_at'] = 4.0
        self.assertEqual(
            cc_resident._cold_start_failure_stage(record),
            'provider_first_content',
        )
        record['first_thinking_or_text_at'] = 5.0
        self.assertIsNone(cc_resident._cold_start_failure_stage(record))

    def test_summary_is_redacted_and_groups_generations(self):
        diagnostic = {
            'correlation_id': 'request-1',
            'request_received_at': 1.0,
            'generation_records': [],
        }
        first = cc_resident._cold_start_record(
            diagnostic, 110, 'process_dead',
        )
        first['process_spawn_at'] = 2.0
        first['reached_stdin'] = True
        first['stdin_flush_at'] = 3.0
        cc_resident._cold_start_mark_process_exit(
            diagnostic, None, 'stdout_eof_before_terminal', record=first,
        )
        second = cc_resident._cold_start_record(
            diagnostic, 111, 'process_dead',
        )
        second['process_spawn_at'] = 4.0
        second['reached_stdin'] = True
        second['stdin_flush_at'] = 5.0
        second['claude_init_event_at'] = 6.0
        second['first_provider_message_start_at'] = 7.0
        cc_resident._cold_start_sync_summary(diagnostic)
        encoded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
        self.assertEqual(len(diagnostic['generation_records']), 2)
        self.assertIn('stdout_eof_before_terminal', encoded)
        self.assertNotIn('prompt', encoded.lower())
        self.assertNotIn('secret', encoded.lower())
        self.assertNotIn('memory', encoded.lower())
        self.assertIn('generation_records', encoded)


class TurnTimingTests(unittest.TestCase):
    """Every Daily turn leaves one [TURN-TIMING] line, hot turns included."""

    def setUp(self):
        logger = logging.getLogger(cc_resident._TURN_TIMING_LOGGER_NAME)
        self._saved = (list(logger.handlers), logger.level, logger.propagate,
                       getattr(logger, '_hayagarden_timing_handler', False))
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger._hayagarden_timing_handler = False
        self.addCleanup(self._restore)
        self.root_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.WARNING)  # production's root level

    def _restore(self):
        logger = logging.getLogger(cc_resident._TURN_TIMING_LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        handlers, level, propagate, flag = self._saved
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate
        logger._hayagarden_timing_handler = flag
        logging.getLogger().setLevel(self.root_level)

    def _emit(self, diagnostic):
        stream = io.StringIO()
        with mock.patch.object(sys, 'stderr', stream):
            cc_resident._log_cold_start_timing(diagnostic)
        lines = [line for line in stream.getvalue().splitlines() if line.startswith('[TURN-TIMING] ')]
        self.assertEqual(len(lines), 1, stream.getvalue())
        return json.loads(lines[0][len('[TURN-TIMING] '):])

    def test_hot_turn_is_logged_past_a_warning_root_logger(self):
        payload = self._emit({
            'correlation_id': 'req-hot',
            'turn_kind': 'hot',
            'request_received_at': 100.0,
            'plan_ready_at': 100.5,
            'hot_context_plan_start_at': 100.6,
            'hot_context_plan_end_at': 108.6,
            'hot_context_plan_decision': 'NO_OP',
            'stdin_flush_at': 108.9,
            'first_thinking_or_text_at': 112.4,
            'terminal_at': 140.0,
            'generation_records': [],
        })
        self.assertEqual(payload['stage'], 'TURN_TIMING')
        self.assertEqual(payload['turn_kind'], 'hot')
        self.assertEqual(payload['hot_context_plan_decision'], 'NO_OP')
        self.assertEqual(payload['durations_ms'], {
            'plan_ms': 500,
            'hot_context_plan_ms': 8000,
            'pre_provider_ms': 8900,
            'provider_first_content_ms': 3500,
            'birth_ms': 12400,
            'total_ms': 40000,
        })
        self.assertEqual(payload['generations'], [])

    def test_cold_turn_keeps_generation_detail_and_spawn_durations(self):
        diagnostic = {
            'correlation_id': 'req-cold',
            'request_received_at': 10.0,
            'plan_ready_at': 16.0,
            'ensure_alive_start_at': 16.1,
            'ensure_alive_end_at': 17.0,
            'generation_records': [],
        }
        record = cc_resident._cold_start_record(diagnostic, 7, 'process_dead')
        record['process_spawn_at'] = 16.2
        record['claude_init_event_at'] = 36.2
        cc_resident._mark_turn_stdin_flushed(diagnostic, record)
        diagnostic['stdin_flush_at'] = 36.5  # pin the clock-based mark for the assertion
        diagnostic['first_thinking_or_text_at'] = 40.5
        diagnostic['terminal_at'] = 70.0
        payload = self._emit(diagnostic)
        self.assertEqual(payload['turn_kind'], 'cold')
        self.assertEqual(payload['durations_ms']['spawn_to_init_ms'], 20000)
        self.assertEqual(payload['durations_ms']['birth_ms'], 30500)
        self.assertEqual(payload['generations'][0]['spawn_reason'], 'process_dead')
        self.assertTrue(payload['generations'][0]['reached_stdin'])

    def test_turn_without_request_time_is_not_logged(self):
        stream = io.StringIO()
        with mock.patch.object(sys, 'stderr', stream):
            cc_resident._log_cold_start_timing({'generation_records': []})
            cc_resident._log_cold_start_timing(None)
        self.assertEqual(stream.getvalue(), '')

    def test_stdin_mark_touches_only_this_turns_record(self):
        diagnostic = {'request_received_at': 1.0, 'generation_records': []}
        stale = {'gen': 3, 'reached_stdin': False, 'stdin_flush_at': None}
        cc_resident._mark_turn_stdin_flushed(diagnostic, stale)
        self.assertIsNotNone(diagnostic['stdin_flush_at'])
        self.assertEqual(stale, {'gen': 3, 'reached_stdin': False, 'stdin_flush_at': None})
        cc_resident._mark_turn_stdin_flushed(None, stale)  # no diagnostic: no-op, no error

    def test_daily_runtime_phase_marks_are_first_write_wins(self):
        from chat import daily_runtime

        plan = types.SimpleNamespace(cold_start_diagnostic={'request_received_at': 1.0})
        daily_runtime._turn_timing_mark(plan, 'hot_context_plan_start_at')
        first = plan.cold_start_diagnostic['hot_context_plan_start_at']
        daily_runtime._turn_timing_mark(plan, 'hot_context_plan_start_at')
        self.assertEqual(plan.cold_start_diagnostic['hot_context_plan_start_at'], first)
        daily_runtime._turn_timing_mark(plan, 'hot_context_plan_end_at', decision='RESPAWN')
        self.assertEqual(plan.cold_start_diagnostic['hot_context_plan_decision'], 'RESPAWN')
        daily_runtime._turn_timing_mark(types.SimpleNamespace(), 'hot_context_plan_start_at')  # no record


if __name__ == '__main__':
    unittest.main()
