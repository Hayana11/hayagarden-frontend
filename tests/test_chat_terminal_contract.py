import pathlib
import unittest

import cc_resident


class ChatTerminalContractTests(unittest.TestCase):
    def test_t1_transport_heartbeat_is_not_provider_progress(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'heartbeat'})
        self.assertIsNone(tracker.last_provider_activity_at)
        self.assertFalse(tracker.end_turn_seen)

    def test_t2_real_provider_progress_refreshes_activity(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'stream_event', 'event': {
            'type': 'content_block_delta',
            'delta': {'type': 'text_delta', 'text': 'x'},
        }})
        self.assertEqual(tracker.last_provider_event_type, 'stream_event:content_block_delta')
        self.assertIsNotNone(tracker.last_provider_activity_at)

    def test_t3_result_within_grace_is_normal(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'stream_event', 'event': {
            'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
        }})
        self.assertFalse(tracker.grace_expired(tracker.end_turn_seen_at + 29.9))
        tracker.observe({'type': 'result', 'stop_reason': 'end_turn', 'is_error': False})
        self.assertTrue(tracker.result_seen)
        self.assertFalse(tracker.grace_expired(tracker.end_turn_seen_at + 30))

    def test_t4_missing_result_expires_and_has_exact_reason(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'assistant', 'message': {
            'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': 'done'}],
        }})
        self.assertTrue(tracker.grace_expired(tracker.end_turn_seen_at + 30.1))
        self.assertEqual(tracker.terminal_reason, 'result_missing_after_end_turn')
        self.assertFalse(tracker.result_seen)

    def test_t5_partial_rescue_contract_remains_non_complete(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'chat' / 'daily_runtime.py').read_text(encoding='utf-8')
        for marker in ("'stream_interrupted': True", "'turn_incomplete': True", "'partial_rescue': True"):
            self.assertIn(marker, source)
        self.assertIn("display_segments: str = ''", source)

    def test_t6_gateway_emits_one_abnormal_terminal_envelope(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text(encoding='utf-8')
        start = source.index("if getattr(exc, 'error_code', None) == 'result_missing_after_end_turn':")
        block = source[start:source.index("        if _daily_plan:", start)]
        self.assertIn("'t': 'err'", block)
        self.assertIn("return None", block)
        self.assertNotIn("'t': 'done'", block)

    def test_t7_frontend_consumes_err_as_terminal(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'src' / 'lib' / 'chat.ts').read_text(encoding='utf-8')
        self.assertIn("case 'err':", source)
        self.assertIn("result = { ok: false", source)

    def test_t8_slow_turn_without_end_turn_has_no_post_end_grace(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'stream_event', 'event': {'type': 'message_start'}})
        self.assertFalse(tracker.grace_expired(999999999))

    def test_t9_boundary_has_one_terminal_outcome(self):
        tracker = cc_resident.ProviderTerminalTracker(30)
        tracker.observe({'type': 'assistant', 'message': {'stop_reason': 'end_turn'}})
        self.assertTrue(tracker.grace_expired(tracker.end_turn_seen_at + 30.1))
        tracker.observe({'type': 'result'})
        self.assertEqual(tracker.terminal_reason, 'result_missing_after_end_turn')
        self.assertFalse(tracker.result_seen)

    def test_t10_cleanup_and_next_turn_contract_is_wired(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn("_rescue_and_abort('result_missing_after_end_turn'", source)
        self.assertIn("_gen_release(None)", source)
        self.assertIn("'_daily_partial_rescued'", source)


if __name__ == '__main__':
    unittest.main()
