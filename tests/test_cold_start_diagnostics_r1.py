"""UH-A0 cold-start timing diagnostics tests without provider traffic."""
from __future__ import annotations

import json
import os
import sys
import unittest

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


if __name__ == '__main__':
    unittest.main()
