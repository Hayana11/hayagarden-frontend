import json
import pathlib
import threading
import unittest
from unittest import mock

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
        authority = cc_resident.TurnTerminalAuthority(turn_identity='turn-contract')
        self.assertTrue(
            authority.submit_timeout_candidate('result_missing_after_end_turn')
        )
        self.assertEqual(
            'RESULT_MISSING_AFTER_END_TURN',
            authority.snapshot()['terminal_outcome'],
        )
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
        authority = cc_resident.TurnTerminalAuthority(turn_identity='turn-contract')
        self.assertTrue(
            authority.submit_timeout_candidate('result_missing_after_end_turn')
        )
        self.assertEqual(
            'RESULT_MISSING_AFTER_END_TURN',
            authority.snapshot()['terminal_outcome'],
        )
        self.assertTrue(tracker.result_seen)

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

    def test_t11b_validated_result_wins_only_stale_stall_race(self):
        receipt = cc_resident.ProviderTerminalReceipt.from_result_event(
            {'type': 'result', 'is_error': False, 'stop_reason': 'end_turn'},
            turn_identity='turn-race',
            process_generation=7,
            claude_session_id='session-race',
        )
        self.assertEqual(
            (None, 'result', True),
            cc_resident._reconcile_terminal_timeout(
                'stall', 'stall',
                terminal_receipt=receipt,
                provider_error=None,
            ),
        )
        self.assertEqual(
            ('hard', 'hard', False),
            cc_resident._reconcile_terminal_timeout(
                'hard', 'hard',
                terminal_receipt=receipt,
                provider_error=None,
            ),
        )
        self.assertEqual(
            ('stall', 'stall', False),
            cc_resident._reconcile_terminal_timeout(
                'stall', 'stall',
                terminal_receipt=None,
                provider_error=None,
            ),
        )
        self.assertEqual(
            ('stall', 'stall', False),
            cc_resident._reconcile_terminal_timeout(
                'stall', 'stall',
                terminal_receipt=receipt,
                provider_error={'error_code': 'provider_error'},
            ),
        )

    def _run_deterministic_send_turn(self, events, watchdog_reason, *, signal_after_read=False):
        """Run the actual send_turn path with a deterministic watchdog signal."""
        watchdogs = []
        kill_calls = []

        class FakeStream:
            def __init__(self, rows):
                self._rows = iter(json.dumps(row) + chr(10) for row in rows)

            def readline(self):
                return next(self._rows, '')

            def close(self):
                return None

        class FakeStdin:
            def write(self, payload):
                self.payload = payload

            def flush(self):
                return None

            def close(self):
                return None

        class FakeProc:
            pid = 7001

            def __init__(self, rows):
                self.stdin = FakeStdin()
                self.stdout = FakeStream(rows)
                self.stderr = FakeStream([])

            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        class DeterministicWatchdog:
            def __init__(self, *, on_timeout, **kwargs):
                self._on_timeout = on_timeout
                self.reason = watchdog_reason
                self.committed = False
                watchdogs.append(self)

            def start(self):
                if not signal_after_read:
                    self.committed = True
                    if self.reason is not None:
                        self._on_timeout(self.reason)

            def stop(self):
                if signal_after_read and not self.committed:
                    self.committed = True
                    if self.reason is not None:
                        self._on_timeout(self.reason)
                return None

            def note_activity(self):
                return None

        session = cc_resident.ResidentSession(
            '/tmp', '', '/tmp/mcp.json',
        )
        session._proc = FakeProc(events)
        session._generation = 7
        session._session_id = 'session-race'
        session._attach_jsonl_usage_with_retry = (
            lambda usage, cursor, **kwargs: usage
        )
        # Keep the fake pipe readable after the callback's kill request so the
        # fixture models the exact ordering under test: watchdog commit first,
        # reader consumes the already-complete provider result second.
        session._kill = lambda quiet=False: kill_calls.append(bool(quiet))

        with mock.patch.object(
            cc_resident, 'StreamWatchdog', DeterministicWatchdog,
        ):
            chunks = list(session.send_turn('race', commit_meta=None))
        return chunks, session, watchdogs, kill_calls

    def test_t13_send_turn_stall_then_valid_result_completes_terminal_path(self):
        events = [
            {
                'type': 'assistant',
                'message': {
                    'stop_reason': 'end_turn',
                    'content': [{'type': 'text', 'text': 'complete reply'}],
                },
            },
            {
                'type': 'result',
                'is_error': False,
                'stop_reason': 'end_turn',
                'usage': {},
            },
        ]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                events, 'stall', signal_after_read=True,
            )
        )
        self.assertEqual(['stall'], [watchdog.reason for watchdog in watchdogs])
        self.assertTrue(watchdogs[0].committed)
        self.assertEqual(['done'], [kind for kind, _ in chunks])
        text, thinking, usage, claims = chunks[0][1]
        self.assertEqual('complete reply', text)
        self.assertEqual('', thinking)
        self.assertEqual({'feedback_ids': [], 'dream_id': None, 'wake_ids': []}, claims)
        self.assertIsInstance(
            usage.terminal_receipt,
            cc_resident.ProviderTerminalReceipt,
        )
        self.assertEqual('provider_result', usage.terminal_receipt.terminal_kind)
        self.assertEqual('result', usage['_obs_terminal_reason'])
        self.assertFalse(usage['_obs_stall_result_race_recovered'])
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual('provider_result', usage['_obs_terminal_linearization_source'])
        self.assertEqual(1, usage['_obs_stale_stall_rejected_count'])
        self.assertEqual(0, usage['_obs_actual_stall_accepted_count'])
        self.assertEqual(1, usage['resident_turn_count'])
        self.assertFalse(session.is_cold())
        self.assertEqual([], kill_calls)

    def test_t14_send_turn_failure_paths_remain_failures(self):
        cases = (
            (
                'true_stall',
                [],
                'stall',
                'provider_stall_timeout',
            ),
            (
                'provider_error',
                [{
                    'type': 'result',
                    'is_error': True,
                    'stop_reason': 'end_turn',
                    'result': 'provider failure',
                }],
                None,
                'provider_error',
            ),
            (
                'hard_timeout',
                [],
                'hard',
                'provider_hard_timeout',
            ),
        )
        for name, events, watchdog_reason, expected_code in cases:
            with self.subTest(name=name):
                with self.assertRaises(cc_resident.ResidentError) as caught:
                    self._run_deterministic_send_turn(
                        events, watchdog_reason,
                    )
                self.assertEqual(expected_code, caught.exception.error_code)

    def _authority_receipt(self):
        return cc_resident.ProviderTerminalReceipt.from_result_event(
            {'type': 'result', 'is_error': False, 'stop_reason': 'end_turn'},
            turn_identity='turn-authority',
            process_generation=9,
            claude_session_id='session-authority',
        )

    def test_r1_result_linearizes_before_stall_and_keeps_resident_hot(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r1',
        )
        barrier = threading.Barrier(2)
        result_done = threading.Event()
        result_accepted = []
        stall_accepted = []

        def accept_result():
            barrier.wait()
            result_accepted.append(
                authority.accept_provider_result(self._authority_receipt())
            )
            result_done.set()

        def submit_stall():
            barrier.wait()
            result_done.wait(1.0)
            stall_accepted.append(authority.submit_timeout_candidate('stall'))

        threads = [
            threading.Thread(target=accept_result),
            threading.Thread(target=submit_stall),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1.0)

        self.assertEqual([True], result_accepted)
        self.assertEqual([False], stall_accepted)
        snapshot = authority.snapshot()
        self.assertEqual('SUCCESS', snapshot['terminal_outcome'])
        self.assertEqual('provider_result', snapshot['terminal_linearization_source'])
        self.assertEqual(1, snapshot['stale_stall_rejected_count'])
        self.assertFalse(authority.begin_cleanup('success'))
        self.assertFalse(snapshot['resident_killed'])

    def test_r2_stall_linearizes_before_late_result_and_kills_once(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r2',
        )
        barrier = threading.Barrier(2)
        stall_done = threading.Event()
        stall_accepted = []
        result_accepted = []

        def accept_stall():
            barrier.wait()
            stall_accepted.append(authority.submit_timeout_candidate('stall'))
            stall_done.set()

        def accept_result():
            barrier.wait()
            stall_done.wait(1.0)
            result_accepted.append(
                authority.accept_provider_result(self._authority_receipt())
            )

        threads = [
            threading.Thread(target=accept_stall),
            threading.Thread(target=accept_result),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1.0)

        self.assertEqual([True], stall_accepted)
        self.assertEqual([False], result_accepted)
        self.assertTrue(authority.begin_cleanup('terminal_failure'))
        self.assertFalse(authority.begin_cleanup('already_cleaned'))
        authority.record_resident_kill(
            process_present=True,
            reason='stall',
        )

        snapshot = authority.snapshot()
        self.assertEqual('STALL', snapshot['terminal_outcome'])
        self.assertEqual(1, snapshot['actual_stall_accepted_count'])
        self.assertEqual(1, snapshot['cleanup_count'])
        self.assertTrue(snapshot['resident_killed'])
        self.assertEqual(1, snapshot['late_result_rejected_count'])

    def test_r3_provider_error_and_stall_have_one_failure_owner(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r3',
        )
        barrier = threading.Barrier(2)
        provider_done = threading.Event()
        provider_accepted = []
        stall_accepted = []

        def accept_provider_error():
            barrier.wait()
            provider_accepted.append(authority.accept_provider_error({
                'error_code': 'provider_error',
                'provider_error_category': 'provider_error',
                'turn_failure_class': 'TURN_LEVEL',
            }))
            provider_done.set()

        def submit_stall():
            barrier.wait()
            provider_done.wait(1.0)
            stall_accepted.append(authority.submit_timeout_candidate('stall'))

        threads = [
            threading.Thread(target=accept_provider_error),
            threading.Thread(target=submit_stall),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1.0)

        self.assertEqual([True], provider_accepted)
        self.assertEqual([False], stall_accepted)
        self.assertEqual('PROVIDER_ERROR', authority.snapshot()['terminal_outcome'])
        self.assertFalse(authority.accept_provider_result(self._authority_receipt()))
        self.assertEqual(2, authority.snapshot()['duplicate_terminal_signal_count'])

    def test_r4_hard_timeout_stays_failure_for_late_valid_receipt(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r4',
        )
        self.assertTrue(authority.submit_timeout_candidate('hard'))
        self.assertFalse(authority.accept_provider_result(self._authority_receipt()))
        snapshot = authority.snapshot()
        self.assertEqual('HARD_TIMEOUT', snapshot['terminal_outcome'])
        self.assertEqual('hard_timeout', snapshot['terminal_reason'])
        self.assertIsNone(snapshot['result_accepted_at'])

    def test_r5_generator_exit_has_bounded_single_cleanup(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r5',
        )
        self.assertTrue(authority.accept_disconnected('generator_exit'))
        self.assertTrue(authority.begin_cleanup('terminal_failure'))
        self.assertFalse(authority.begin_cleanup('already_cleaned'))
        authority.record_resident_kill(
            process_present=True,
            reason='generator_exit',
        )
        snapshot = authority.snapshot()
        self.assertEqual('DISCONNECTED', snapshot['terminal_outcome'])
        self.assertEqual(1, snapshot['cleanup_count'])
        self.assertTrue(snapshot['resident_killed'])

    def test_r6_duplicate_terminal_signals_are_immutable(self):
        authority = cc_resident.TurnTerminalAuthority(
            turn_identity='turn-r6',
        )
        self.assertTrue(authority.submit_timeout_candidate('stall'))
        self.assertFalse(authority.submit_timeout_candidate('stall'))
        self.assertFalse(authority.submit_timeout_candidate('hard'))
        self.assertFalse(authority.accept_disconnected('late_disconnect'))
        self.assertTrue(authority.begin_cleanup('terminal_failure'))
        authority.record_resident_kill(
            process_present=True,
            reason='stall',
        )
        self.assertFalse(authority.begin_cleanup('already_cleaned'))
        snapshot = authority.snapshot()
        self.assertEqual('STALL', snapshot['terminal_outcome'])
        self.assertEqual(3, snapshot['duplicate_terminal_signal_count'])
        self.assertEqual(1, snapshot['cleanup_count'])
        self.assertTrue(snapshot['resident_killed'])

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
