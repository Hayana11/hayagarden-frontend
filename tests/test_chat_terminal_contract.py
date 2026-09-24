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
        start = source.index('def _stream_cc_daily_soft_window(')
        end = source.index("\n\n@app.route('/chat/stream'", start)
        daily = source[start:end]
        self.assertIn('cleanup = _rescue_and_abort(error_code, respawn=True)', daily)
        self.assertIn('yield _sse_json(_chat_stream_failure_event(', daily)
        self.assertNotIn("'t': 'done', 'ok': False", daily)

    def test_t6b_terminal_wrapper_releases_lock_and_stops_at_first_terminal(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text(encoding='utf-8')
        start = source.index('if _daily_ctx.enabled() and not _rewrite_id:')
        end = source.index('                    return\n                text, thinking = None, None', start)
        wrapper = source[start:end]
        terminal = wrapper.index('if _chat_sse_terminal_kind(_chunk):')
        release = wrapper.index('_gen_release(None)', terminal)
        emit = wrapper.index('yield _chunk', terminal)
        self.assertLess(release, emit)
        self.assertIn('break', wrapper[emit:])

    def test_t7_frontend_consumes_err_as_terminal(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'src' / 'lib' / 'chat.ts').read_text(encoding='utf-8')
        self.assertIn("case 'err':", source)
        self.assertIn("result = { ok: false", source)

    def test_failure_code_taxonomy_keeps_recovery_state_out_of_rollback_allowlist(self):
        self.assertEqual(
            'TURN_LEVEL', cc_resident.resident_failure_class('provider_error'),
        )
        self.assertEqual(
            'TURN_LEVEL', cc_resident.resident_failure_class('provider_refusal'),
        )
        self.assertEqual(
            'PROCESS_LEVEL', cc_resident.resident_failure_class('stdin_write_failed'),
        )
        self.assertEqual(
            'PROCESS_LEVEL', cc_resident.resident_failure_class('provider_hard_timeout'),
        )
        self.assertEqual(
            'RUNTIME_LEVEL', cc_resident.resident_failure_class('claude_runtime_startup_failed'),
        )
        self.assertEqual(
            'RUNTIME_LEVEL', cc_resident.resident_failure_class('claude_runtime_rollback_pending'),
        )
        self.assertNotIn('claude_runtime_rollback_pending', cc_resident._RUNTIME_FAILURE_CODES)

    def test_provider_error_taxonomy_is_safe_and_typed(self):
        error = cc_resident.classify_provider_error_event({
            'type': 'result',
            'is_error': True,
            'error': {'type': 'invalid_request_error'},
            'result': 'reasoning_extraction private-provider-detail secret-token',
        }, refusal_marker=True)
        self.assertEqual('provider_refusal', error['error_code'])
        self.assertEqual('reasoning_extraction', error['provider_error_category'])
        self.assertEqual('TURN_LEVEL', error['turn_failure_class'])
        self.assertFalse(error['retryable'])
        self.assertNotIn('private-provider-detail', error['public_message'])
        self.assertNotIn('secret-token', error['public_message'])
        cases = (
            ({'error': {'type': 'invalid_request_error'}}, 'invalid_request', 'TURN_LEVEL'),
            ({'error': {'type': 'rate_limit_error'}}, 'rate_limit', 'TURN_LEVEL'),
            ({'error': {'type': 'connection_error'}}, 'provider_error', 'PROCESS_LEVEL'),
        )
        for event, error_code, failure_class in cases:
            with self.subTest(event=event):
                result = cc_resident.classify_provider_error_event(event)
                self.assertEqual(error_code, result['error_code'])
                self.assertEqual(failure_class, result['turn_failure_class'])

    def test_frontend_done_false_is_visible_and_finality_events_are_nonterminal(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        frontend = (root / 'app' / 'src' / 'lib' / 'chat.ts').read_text(encoding='utf-8')
        self.assertIn("result = ev.ok === false", frontend)
        self.assertIn('本轮生成未完成。', frontend)
        gateway = (root / 'gateway.py').read_text(encoding='utf-8')
        helper = gateway[gateway.index('def _chat_sse_terminal_kind('):gateway.index('_WAKE_LIVE_TRACE_FIELDS')]
        self.assertIn("event.get('t') in ('err', 'done')", helper)
        self.assertNotIn("'turn_final'", helper)
        self.assertNotIn("'turn_reconcile'", helper)

    def test_daily_partial_and_complete_paths_close_after_persistence(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text(encoding='utf-8')
        start = source.index('def _stream_cc_daily_soft_window(')
        end = source.index("\n\n@app.route('/chat/stream'", start)
        daily = source[start:end]
        self.assertIn('persist_partial_daily_stream_rescue(', daily)
        self.assertIn("partial_rescue=bool(cleanup.get('partial_rescue_performed'))", daily)
        persist = daily.index('persist_daily_assistant_for_plan(')
        persisted = daily.index("yield ('persisted', canonical.content, canonical.thinking)")
        success_terminal = daily.index("'ok': True", persisted)
        self.assertLess(persist, persisted)
        self.assertLess(persisted, success_terminal)

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

    def test_t11_live_result_receipt_is_typed_and_not_end_turn_derived(self):
        receipt = cc_resident.ProviderTerminalReceipt.from_result_event(
            {'type': 'result', 'is_error': False, 'stop_reason': 'end_turn'},
            turn_identity='turn-1',
            process_generation=3,
            claude_session_id='session-1',
        )
        self.assertEqual(receipt.source, 'resident_live_stdout')
        self.assertEqual(receipt.process_generation, 3)
        with self.assertRaises(ValueError):
            cc_resident.ProviderTerminalReceipt.from_result_event(
                {'type': 'assistant', 'stop_reason': 'end_turn'},
                turn_identity='turn-1',
                process_generation=3,
                claude_session_id='session-1',
            )
        with self.assertRaises(ValueError):
            cc_resident.ProviderTerminalReceipt.from_result_event(
                {'type': 'result', 'is_error': True},
                turn_identity='turn-1',
                process_generation=3,
                claude_session_id='session-1',
            )

        for stop_reason in ('tool_deferred', '', 'future_reason'):
            with self.subTest(stop_reason=stop_reason):
                with self.assertRaises(ValueError):
                    cc_resident.ProviderTerminalReceipt.from_result_event(
                        {
                            'type': 'result',
                            'is_error': False,
                            'stop_reason': stop_reason,
                        },
                        turn_identity='turn-1',
                        process_generation=3,
                        claude_session_id='session-1',
                    )

    def test_t12_daily_runtime_forwards_receipt_without_rederiving_terminal(self):
        source = pathlib.Path(__file__).resolve().parents[1] / 'chat' / 'daily_runtime.py'
        text = source.read_text(encoding='utf-8')
        self.assertIn('terminal_receipt: Optional[cc_resident.ProviderTerminalReceipt]', text)
        self.assertIn('transcript_process_generation=plan.transcript_process_generation', text)
        self.assertIn('terminal_receipt=plan.terminal_receipt', text)

    def test_t10_cleanup_and_next_turn_contract_is_wired(self):
        source = (pathlib.Path(__file__).resolve().parents[1] / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('_rescue_and_abort(error_code, respawn=True)', source)
        self.assertIn("_gen_release(None)", source)
        self.assertIn("'_daily_partial_rescued'", source)


if __name__ == '__main__':
    unittest.main()
