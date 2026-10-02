import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest import mock

import cc_resident
import gateway


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


    def test_gmsg_r1_cli_terminal_result_missing_uses_completion_message(self):
        error = cc_resident.ResidentError(
            'provider stderr secret',
            error_code='cli_terminal_result_missing',
        )
        event = gateway._chat_stream_failure_event(error)
        expected = '回复已完整生成，收尾时连接异常；已保留已收到的内容。'
        self.assertEqual('err', event['t'])
        self.assertEqual('cli_terminal_result_missing', event['code'])
        self.assertEqual('cli_terminal_result_missing', event['error_code'])
        self.assertEqual(expected, event['d'])
        self.assertEqual(expected, event['message'])
        self.assertNotIn('provider stderr secret', event['d'])
        self.assertNotIn(
            'Claude Code 本轮未能完成收尾；已停止当前生成。',
            event['d'],
        )

    def test_gmsg_r2_cli_terminal_result_missing_preserves_partial_rescue(self):
        error = cc_resident.ResidentError(
            'provider stderr secret',
            error_code='cli_terminal_result_missing',
        )
        event = gateway._chat_stream_failure_event(error, partial_rescue=True)
        self.assertEqual(
            '回复已完整生成，收尾时连接异常；已保留已收到的内容。',
            event['message'],
        )
        self.assertTrue(event['partial_rescue'])

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

    def test_t11b_no_second_terminal_reconciliation_helper_exists(self):
        self.assertFalse(hasattr(cc_resident, '_reconcile_terminal_timeout'))

    def _run_deterministic_send_turn(
        self,
        events,
        watchdog_reason,
        *,
        signal_after_read=False,
        signal_on_first_activity=False,
        signal_on_probe=False,
        block_readline=False,
        release_result_after_sigterm=False,
        transcript_rows=None,
        transcript_cursor_offset=0,
        transcript_path_mismatch=False,
        recovery_grace=0.1,
        close_after_first_chunk=False,
        capture_exception=False,
        mark_killed_nonreusable=False,
    ):
        """Run the actual send_turn path with a deterministic watchdog signal."""
        watchdogs = []
        kill_calls = []

        class FakeStream:
            def __init__(self, rows):
                self._rows = iter(json.dumps(row) + chr(10) for row in rows)
                self.readline_started = threading.Event()
                self.readline_finished = threading.Event()
                self.release_readline = threading.Event()
                self.sigterm_sent = threading.Event()

            def readline(self):
                if block_readline:
                    self.readline_started.set()
                    self.release_readline.wait(2.0)
                    self.readline_finished.set()
                    return ''
                if release_result_after_sigterm:
                    self.sigterm_sent.wait(2.0)
                self.readline_finished.set()
                return next(self._rows, '')

            def close(self):
                return None

        class FakeStdin:
            def __init__(self):
                self.closed = False

            def write(self, payload):
                self.payload = payload

            def flush(self):
                return None

            def close(self):
                self.closed = True

        class FakeProc:
            pid = 7001

            def __init__(self, rows):
                self.stdin = FakeStdin()
                self.stdout = FakeStream(rows)
                self.stderr = FakeStream([])

            def poll(self):
                return None

            def terminate(self):
                self.stdout.sigterm_sent.set()
                return None

            def wait(self, timeout=None):
                return None

            def kill(self):
                return None

        class DeterministicWatchdog:
            def __init__(self, *, on_timeout, **kwargs):
                self._on_timeout = on_timeout
                self._on_probe = kwargs.get('on_probe')
                self.reason = watchdog_reason
                self.committed = False
                self.probe_consumed = False
                self._hard_deadline = __import__('time').monotonic() + 60
                watchdogs.append(self)

            @property
            def hard_deadline(self):
                return self._hard_deadline

            def start(self):
                if signal_on_probe:
                    self.probe_consumed = bool(self._on_probe())
                    if not self.probe_consumed and self.reason is not None:
                        self.committed = True
                        self._on_timeout(self.reason)
                elif block_readline:
                    self._thread = threading.Thread(
                        target=self._fire_after_reader_blocks,
                        daemon=True,
                    )
                    self._thread.start()
                elif not signal_after_read and not signal_on_first_activity:
                    self.committed = True
                    if self.reason is not None:
                        self._on_timeout(self.reason)

            def _fire_after_reader_blocks(self):
                session._proc.stdout.readline_started.wait(2.0)
                self.committed = True
                if self.reason is not None:
                    self._on_timeout(self.reason)

            def stop(self):
                if signal_after_read and not self.committed:
                    self.committed = True
                    if self.reason is not None:
                        self._on_timeout(self.reason)
                thread = getattr(self, '_thread', None)
                if thread is not None:
                    thread.join(2.0)
                return None

            def claim_recovery(self, probe_generation, claim_lifecycle):
                if self.committed:
                    return False
                if not claim_lifecycle():
                    return False
                self.committed = True
                return True

            def note_activity(self):
                if signal_on_first_activity and not self.committed:
                    self.committed = True
                    if self.reason is not None:
                        self._on_timeout(self.reason)

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
        def fake_kill(quiet=False):
            kill_calls.append(bool(quiet))
            if mark_killed_nonreusable:
                session._proc = None
                session._cold = True
                session._next_spawn_reason = 'process_dead'
            if block_readline:
                session._proc.stdout.release_readline.set()

        session._kill = fake_kill

        transcript_temp = None
        patchers = []
        if signal_on_probe:
            original_cfg_int = cc_resident._cfg_int
            patchers.append(mock.patch.object(
                cc_resident,
                '_cfg_int',
                side_effect=lambda key, default: (
                    recovery_grace
                    if key in (
                        'CC_TERMINAL_RECOVERY_EOF_GRACE',
                        'CC_TERMINAL_RECOVERY_SIGTERM_GRACE',
                    )
                    else original_cfg_int(key, default)
                ),
            ))
        if transcript_rows is not None:
            transcript_temp = tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', delete=False,
            )
            for row in transcript_rows:
                transcript_temp.write(json.dumps(row) + chr(10))
            transcript_temp.close()
            cursor = {
                'path': transcript_temp.name,
                'offset': int(transcript_cursor_offset),
            }
            expected_path = transcript_temp.name
            if transcript_path_mismatch:
                expected_path = transcript_temp.name + '.other'
            import tools.cc_jsonl_usage as jsonl_usage
            patchers.extend([
                mock.patch.object(
                    jsonl_usage, 'snapshot_session_jsonl', return_value=cursor,
                ),
                mock.patch.object(
                    jsonl_usage, 'session_jsonl_path', return_value=expected_path,
                ),
            ])
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    cc_resident, 'StreamWatchdog', DeterministicWatchdog,
                ))
                for patcher in patchers:
                    stack.enter_context(patcher)
                generator = session.send_turn('race', commit_meta=None)
                if close_after_first_chunk:
                    chunks = [next(generator)]
                    generator.close()
                else:
                    try:
                        chunks = list(generator)
                    except BaseException as exc:
                        if not capture_exception:
                            raise
                        return exc, session, watchdogs, kill_calls
        finally:
            if transcript_temp is not None:
                try:
                    os.unlink(transcript_temp.name)
                except OSError:
                    pass
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

    def test_t13b_result_claim_precedes_first_activity_watchdog_stall(self):
        """A result already read must beat a synchronous note_activity stall."""
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [{
                    'type': 'result',
                    'is_error': False,
                    'stop_reason': 'end_turn',
                }],
                'stall',
                signal_on_first_activity=True,
            )
        )
        self.assertEqual(['done'], [kind for kind, _ in chunks])
        usage = chunks[0][1][2]
        self.assertIsInstance(
            usage.terminal_receipt,
            cc_resident.ProviderTerminalReceipt,
        )
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual('provider_result', usage['_obs_terminal_linearization_source'])
        self.assertEqual(1, usage['_obs_stale_stall_rejected_count'])
        self.assertEqual(0, usage['_obs_actual_stall_accepted_count'])
        self.assertEqual(0, usage['_obs_late_result_rejected_count'])
        self.assertEqual(1, usage['_obs_duplicate_terminal_signal_count'])
        self.assertEqual([], kill_calls)

    def test_a1_r1_blocked_readline_timeout_kills_once_and_unblocks_reader(self):
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            block_readline=True,
            capture_exception=True,
        )
        self.assertIsInstance(error, cc_resident.ResidentError)
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertEqual(['stall'], [watchdog.reason for watchdog in watchdogs])
        self.assertTrue(session._proc.stdout.readline_started.is_set())
        self.assertTrue(session._proc.stdout.readline_finished.is_set())
        self.assertEqual([True], kill_calls)
        diagnostics = error.diagnostics
        self.assertEqual('STALL', diagnostics['terminal_outcome'])
        self.assertEqual(1, diagnostics['cleanup_count'])
        self.assertTrue(diagnostics['resident_killed'])
        timeout_diag = diagnostics['timeout_diagnostics']
        self.assertEqual('READLINE', timeout_diag['reader_state'])
        self.assertEqual('stall', timeout_diag['timeout_candidate'])
        self.assertIn('thread_dump', timeout_diag)
        self.assertIn('child_wchan', timeout_diag)
        self.assertIn('child_stack', timeout_diag)
        self.assertEqual(1, len([x for x in [timeout_diag] if x]))

    def test_a1_r2_success_rejects_stall_without_cleanup_or_kill(self):
        chunks, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [{
                'type': 'result',
                'is_error': False,
                'stop_reason': 'end_turn',
            }],
            'stall',
            signal_on_first_activity=True,
        )
        usage = chunks[0][1][2]
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(1, usage['_obs_stale_stall_rejected_count'])
        self.assertEqual(0, usage['_obs_actual_stall_accepted_count'])
        self.assertEqual(0, usage['_obs_cleanup_count'])
        self.assertIsNone(usage['_obs_timeout_diagnostics'])
        self.assertFalse(session.is_cold())
        self.assertEqual([], kill_calls)

    def test_a1_r3_hard_timeout_kills_once(self):
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'hard',
            capture_exception=True,
        )
        self.assertIsInstance(error, cc_resident.ResidentError)
        self.assertEqual('provider_hard_timeout', error.error_code)
        self.assertEqual(['hard'], [watchdog.reason for watchdog in watchdogs])
        self.assertEqual([True], kill_calls)
        diagnostics = error.diagnostics
        self.assertEqual('HARD_TIMEOUT', diagnostics['terminal_outcome'])
        self.assertEqual(1, diagnostics['cleanup_count'])
        self.assertTrue(diagnostics['resident_killed'])
        self.assertEqual('hard', diagnostics['timeout_diagnostics']['timeout_candidate'])

    def _current_turn_end_turn_rows(self):
        return [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'timestamp': '2026-10-02T00:00:00.000Z',
                'message': {'stop_reason': 'end_turn'},
            },
        ]

    def _terminal_result_event(self):
        return {
            'type': 'result',
            'is_error': False,
            'stop_reason': 'end_turn',
        }

    def test_c_r1_current_turn_end_turn_recovers_after_stdin_eof(self):
        chunks, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [self._terminal_result_event()],
            'stall',
            signal_on_probe=True,
            transcript_rows=self._current_turn_end_turn_rows(),
        )
        usage = chunks[0][1][2]
        recovery = usage['_obs_terminal_recovery']
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF', recovery['recovery_stage'],
        )
        self.assertEqual('SUCCESS', recovery['final_outcome'])
        self.assertEqual(1, usage['_obs_cleanup_count'])
        self.assertEqual([True], kill_calls)
        self.assertTrue(session.is_cold())
        self.assertEqual('terminal_recovery', session._next_spawn_reason)
        self.assertEqual(0, usage['_obs_actual_stall_accepted_count'])

    def test_c_r2_sigterm_flush_recovers_and_forces_next_respawn(self):
        chunks, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [self._terminal_result_event()],
            'stall',
            signal_on_probe=True,
            release_result_after_sigterm=True,
            transcript_rows=self._current_turn_end_turn_rows(),
        )
        usage = chunks[0][1][2]
        recovery = usage['_obs_terminal_recovery']
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_SIGTERM', recovery['recovery_stage'],
        )
        self.assertIsNotNone(recovery['sigterm_sent_at'])
        self.assertEqual(1, usage['_obs_cleanup_count'])
        self.assertEqual([True], kill_calls)
        self.assertTrue(session.is_cold())

    def test_c_r3_missing_result_after_bounded_recovery_is_failure(self):
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=self._current_turn_end_turn_rows(),
            capture_exception=True,
        )
        self.assertIsInstance(error, cc_resident.ResidentError)
        self.assertEqual('cli_terminal_result_missing', error.error_code)
        self.assertEqual('CLI_TERMINAL_RESULT_MISSING', error.diagnostics['terminal_outcome'])
        self.assertEqual(1, error.diagnostics['cleanup_count'])
        self.assertEqual([True], kill_calls)
        self.assertEqual(
            'CLI_TERMINAL_RESULT_MISSING',
            error.diagnostics['timeout_diagnostics']['terminal_outcome'],
        )

    def test_h_r1_recovery_controller_start_failure_is_bounded(self):
        with mock.patch.object(
            cc_resident.threading.Thread,
            'start',
            side_effect=RuntimeError('synthetic recovery controller start failure'),
        ):
            error, session, watchdogs, kill_calls = (
                self._run_deterministic_send_turn(
                    [],
                    'stall',
                    signal_on_probe=True,
                    transcript_rows=self._current_turn_end_turn_rows(),
                    capture_exception=True,
                    mark_killed_nonreusable=True,
                )
            )

        self.assertIsInstance(error, cc_resident.ResidentError)
        self.assertEqual('cli_terminal_result_missing', error.error_code)
        self.assertEqual(
            'CLI_TERMINAL_RESULT_MISSING',
            error.diagnostics['terminal_outcome'],
        )
        self.assertEqual(1, error.diagnostics['cleanup_count'])
        self.assertEqual(
            'cli_terminal_result_missing',
            error.diagnostics['timeout_candidate_type'],
        )
        self.assertEqual([True], kill_calls)
        self.assertIsNone(session._proc)
        self.assertTrue(session.is_cold())
        self.assertEqual('process_dead', session._next_spawn_reason)

        recovery = error.usage['_obs_terminal_recovery']
        self.assertIsNotNone(recovery)
        self.assertEqual(
            'CLI_TERMINAL_RESULT_MISSING',
            recovery['recovery_stage'],
        )
        self.assertEqual(
            'CLI_TERMINAL_RESULT_MISSING',
            recovery['final_outcome'],
        )
        self.assertIsNotNone(recovery['total_recovery_latency_ms'])

        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertTrue(watchdogs[0].committed)
        self.assertEqual(
            0,
            error.diagnostics['actual_stall_accepted_count'],
        )

    def test_c_r4_previous_turn_end_turn_does_not_start_recovery(self):
        previous = self._current_turn_end_turn_rows()[0]
        offset = len(json.dumps(previous) + chr(10))
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=[previous],
            transcript_cursor_offset=offset,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertEqual('STALL', error.diagnostics['terminal_outcome'])
        self.assertEqual(1, error.diagnostics['cleanup_count'])
        self.assertEqual([True], kill_calls)

    def test_c_r5_transcript_identity_mismatch_does_not_start_recovery(self):
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=self._current_turn_end_turn_rows(),
            transcript_path_mismatch=True,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertEqual('STALL', error.diagnostics['terminal_outcome'])
        self.assertEqual([True], kill_calls)


    def test_s_r1_sidechain_end_turn_does_not_start_recovery(self):
        rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': None, 'content': [
                    {'type': 'tool_use', 'name': 'Task'},
                ]},
            },
            {
                'type': 'assistant',
                'isSidechain': True,
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=rows,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertFalse(watchdogs[0].probe_consumed)
        self.assertFalse(session._proc.stdin.closed)
        self.assertEqual([True], kill_calls)
        timeout_diag = error.diagnostics['timeout_diagnostics']
        self.assertIsNone(
            timeout_diag['last_current_turn_assistant_stop_reason'],
        )
        self.assertFalse(
            timeout_diag['current_turn_durable_assistant_end_turn'],
        )

    def test_s_r2_first_main_end_turn_is_not_final_proof(self):
        rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
            {
                'type': 'assistant',
                'message': {
                    'stop_reason': 'tool_use',
                    'content': [{'type': 'tool_use', 'name': 'Task'}],
                },
            },
        ]
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=rows,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertFalse(watchdogs[0].probe_consumed)
        self.assertFalse(session._proc.stdin.closed)
        self.assertEqual([True], kill_calls)
        timeout_diag = error.diagnostics['timeout_diagnostics']
        self.assertEqual(
            'tool_use',
            timeout_diag['last_current_turn_assistant_stop_reason'],
        )
        self.assertFalse(
            timeout_diag['current_turn_durable_assistant_end_turn'],
        )

    def test_s_r3_last_main_chain_end_turn_can_start_recovery(self):
        rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'assistant',
                'isSidechain': True,
                'message': {'stop_reason': 'end_turn'},
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [self._terminal_result_event()],
                'stall',
                signal_on_probe=True,
                transcript_rows=rows,
            )
        )
        usage = chunks[0][1][2]
        self.assertEqual(['done'], [kind for kind, _ in chunks])
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)

    def test_s_r4_sidechain_after_main_end_turn_does_not_break_proof(self):
        rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
            {
                'type': 'assistant',
                'isSidechain': True,
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [self._terminal_result_event()],
                'stall',
                signal_on_probe=True,
                transcript_rows=rows,
            )
        )
        usage = chunks[0][1][2]
        self.assertEqual(['done'], [kind for kind, _ in chunks])
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)

    def test_s_r5_metadata_after_main_end_turn_does_not_break_proof(self):
        rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
            {'type': 'attachment', 'id': 'attachment-1'},
            {'type': 'system', 'subtype': 'meta'},
        ]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [self._terminal_result_event()],
                'stall',
                signal_on_probe=True,
                transcript_rows=rows,
            )
        )
        usage = chunks[0][1][2]
        self.assertEqual(['done'], [kind for kind, _ in chunks])
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)


    def test_offset_r1_cursor_zero_includes_first_current_turn_event(self):
        rows = [
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [self._terminal_result_event()],
                'stall',
                signal_on_probe=True,
                transcript_rows=rows,
                transcript_cursor_offset=0,
            )
        )
        usage = chunks[0][1][2]
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertEqual(
            0,
            usage['_obs_terminal_recovery']['proof']['event_offset'],
        )
        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)

    def test_offset_r2_current_first_event_starts_at_captured_offset(self):
        previous = {
            'type': 'assistant',
            'message': {'stop_reason': 'tool_use'},
        }
        current = {
            'type': 'assistant',
            'message': {'stop_reason': 'end_turn'},
        }
        previous_bytes = json.dumps(previous) + chr(10)
        start_offset = len(previous_bytes)
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                [self._terminal_result_event()],
                'stall',
                signal_on_probe=True,
                transcript_rows=[previous, current],
                transcript_cursor_offset=start_offset,
            )
        )
        usage = chunks[0][1][2]
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            start_offset,
            usage['_obs_terminal_recovery']['proof']['event_offset'],
        )
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertTrue(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)

    def test_offset_r3_first_current_end_turn_then_nonterminal_rejects(self):
        rows = [
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'tool_use'},
            },
        ]
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=rows,
            transcript_cursor_offset=0,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertFalse(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)
        timeout_diag = error.diagnostics['timeout_diagnostics']
        self.assertEqual(
            'tool_use',
            timeout_diag['last_current_turn_assistant_stop_reason'],
        )
        self.assertFalse(
            timeout_diag['current_turn_durable_assistant_end_turn'],
        )

    def test_offset_r4_first_current_nonterminal_then_sidechain_end_turn(self):
        rows = [
            {
                'type': 'assistant',
                'message': {'stop_reason': 'tool_use'},
            },
            {
                'type': 'assistant',
                'isSidechain': True,
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [],
            'stall',
            signal_on_probe=True,
            transcript_rows=rows,
            transcript_cursor_offset=0,
            capture_exception=True,
        )
        self.assertEqual('provider_stall_timeout', error.error_code)
        self.assertFalse(watchdogs[0].probe_consumed)
        self.assertEqual([True], kill_calls)
        timeout_diag = error.diagnostics['timeout_diagnostics']
        self.assertEqual(
            'tool_use',
            timeout_diag['last_current_turn_assistant_stop_reason'],
        )
        self.assertFalse(
            timeout_diag['current_turn_durable_assistant_end_turn'],
        )

    def test_c_r6_normal_result_before_probe_does_not_touch_recovery(self):
        chunks, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [self._terminal_result_event()],
            'stall',
            signal_on_first_activity=True,
        )
        usage = chunks[0][1][2]
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertIsNone(usage['_obs_terminal_recovery'])
        self.assertEqual([], kill_calls)

    def test_c_r7_recovery_hard_deadline_remains_authoritative(self):
        hard_calls = []
        missing_calls = []
        controller = cc_resident.TerminalRecoveryController(
            eof_grace=10,
            sigterm_grace=10,
            hard_deadline=__import__('time').monotonic() - 1,
            close_stdin=lambda: None,
            send_sigterm=lambda: None,
            submit_hard_timeout=lambda: hard_calls.append(True),
            submit_result_missing=lambda: missing_calls.append(True),
        )
        self.assertTrue(controller.start({
            'detected_at': 1,
            'provider_last_activity_at': 1,
        }))
        controller.wait_for_final()
        self.assertEqual([True], hard_calls)
        self.assertEqual([], missing_calls)
        self.assertEqual('HARD_TIMEOUT', controller.snapshot()['recovery_stage'])

    def _watchdog_for_probe_lifecycle(self, on_probe, on_timeout=None):
        return cc_resident.StreamWatchdog(
            stall_timeout=360,
            hard_timeout=3600,
            on_timeout=on_timeout or (lambda reason: None),
            is_proc_alive=lambda: True,
            probe_timeout=20,
            on_probe=lambda generation: on_probe(generation),
        )

    def _age_watchdog_into_probe_epoch(self, watchdog):
        watchdog._last_activity_at = time.monotonic() - 21

    def test_p_r1_missed_probe_rearms_after_activity_and_hits_second_epoch(self):
        probe_results = []
        recovery_started = []
        durable_end_turn = [False]

        def on_probe(_generation):
            probe_results.append(bool(durable_end_turn[0]))
            if durable_end_turn[0]:
                recovery_started.append('POST_COMPLETION_RECOVERY')
                return True
            return False

        watchdog = self._watchdog_for_probe_lifecycle(on_probe)
        self._age_watchdog_into_probe_epoch(watchdog)
        self.assertFalse(watchdog._try_probe())
        self.assertEqual([False], probe_results)

        # A real provider event ends epoch 1 and re-arms the next probe.  The
        # durable current-turn end_turn is written after that activity.
        watchdog.note_activity()
        durable_end_turn[0] = True
        self._age_watchdog_into_probe_epoch(watchdog)
        self.assertTrue(watchdog._try_probe())
        self.assertEqual([False, True], probe_results)
        self.assertEqual(['POST_COMPLETION_RECOVERY'], recovery_started)
        self.assertIsNone(watchdog.fired_reason)

    def test_p_r2_same_inactivity_epoch_probes_once(self):
        probe_calls = []
        timeout_calls = []
        watchdog = self._watchdog_for_probe_lifecycle(
            lambda _generation: probe_calls.append(True) or False,
            on_timeout=lambda reason: timeout_calls.append(reason),
        )
        self._age_watchdog_into_probe_epoch(watchdog)
        self.assertFalse(watchdog._try_probe())
        for _ in range(4):
            self.assertFalse(watchdog._try_probe())
        self.assertEqual([True], probe_calls)
        self.assertEqual([], timeout_calls)
        self.assertIsNone(watchdog.fired_reason)

    def test_p_r3_activity_rearms_exactly_one_probe_per_epoch(self):
        probe_calls = []
        watchdog = self._watchdog_for_probe_lifecycle(
            lambda _generation: probe_calls.append(len(probe_calls)) or False,
        )
        for epoch in range(3):
            self._age_watchdog_into_probe_epoch(watchdog)
            self.assertFalse(watchdog._try_probe())
            self.assertEqual(epoch + 1, len(probe_calls))
            self.assertFalse(watchdog._try_probe())
            self.assertEqual(epoch + 1, len(probe_calls))
            watchdog.note_activity()
            if epoch == 1:
                watchdog.note_activity()
        self.assertEqual(3, len(probe_calls))

    def test_p_r4_terminal_or_active_recovery_cannot_rearm_probe(self):
        cases = (
            ('SUCCESS', lambda watchdog: watchdog.stop()),
            ('STALL', lambda watchdog: (
                setattr(
                    watchdog,
                    '_last_activity_at',
                    time.monotonic() - 361,
                ),
                watchdog._try_fire('stall'),
            )),
            ('HARD_TIMEOUT', lambda watchdog: (
                setattr(
                    watchdog,
                    '_started_at',
                    time.monotonic() - 3601,
                ),
                watchdog._try_fire('hard'),
            )),
            ('POST_COMPLETION_RECOVERY', lambda watchdog: (
                self._age_watchdog_into_probe_epoch(watchdog),
                watchdog._try_probe(),
            )),
        )
        for name, terminalize in cases:
            with self.subTest(name=name):
                probe_calls = []
                watchdog = self._watchdog_for_probe_lifecycle(
                    lambda _generation: probe_calls.append(True) or True,
                )
                terminalize(watchdog)
                watchdog.note_activity()
                self._age_watchdog_into_probe_epoch(watchdog)
                self.assertFalse(watchdog._try_probe())
                self.assertLessEqual(len(probe_calls), 1)

    def test_g_r1_success_wins_during_probe_rejects_recovery_side_effects(self):
        authority = cc_resident.TurnTerminalAuthority(turn_identity='g-r1')
        scan_started = threading.Event()
        release_scan = threading.Event()
        close_stdin_calls = []
        sigterm_calls = []
        watchdog_holder = []
        start_lock = threading.Lock()

        def on_probe(probe_generation):
            scan_started.set()
            release_scan.wait(1.0)
            watchdog = watchdog_holder[0]
            if not watchdog.claim_recovery(
                probe_generation,
                lambda: authority.claim_terminal_recovery(probe_generation),
            ):
                return False
            with start_lock:
                if authority.outcome != cc_resident.TurnTerminalAuthority.RUNNING:
                    return True
                close_stdin_calls.append(True)
            return True

        watchdog = cc_resident.StreamWatchdog(
            stall_timeout=360,
            hard_timeout=3600,
            on_timeout=lambda reason: None,
            is_proc_alive=lambda: True,
            probe_timeout=20,
            on_probe=on_probe,
        )
        watchdog_holder.append(watchdog)
        self._age_watchdog_into_probe_epoch(watchdog)
        probe_thread = threading.Thread(target=watchdog._try_probe)
        probe_thread.start()
        self.assertTrue(scan_started.wait(1.0))

        self.assertTrue(authority.accept_provider_result(self._authority_receipt()))
        watchdog.note_activity()
        release_scan.set()
        probe_thread.join(1.0)

        snapshot = authority.snapshot()
        self.assertEqual('SUCCESS', snapshot['terminal_outcome'])
        self.assertFalse(snapshot['recovery_claimed'])
        self.assertEqual([], close_stdin_calls)
        self.assertEqual([], sigterm_calls)
        self.assertEqual(0, snapshot['cleanup_count'])
        self.assertFalse(watchdog._stopped)

    def test_g_r2_activity_during_scan_stales_old_probe_and_next_epoch_can_claim(self):
        authority = cc_resident.TurnTerminalAuthority(turn_identity='g-r2')
        scan_started = threading.Event()
        release_scan = threading.Event()
        first_probe = [True]
        close_stdin_calls = []
        watchdog_holder = []

        def on_probe(probe_generation):
            if first_probe[0]:
                first_probe[0] = False
                scan_started.set()
                release_scan.wait(1.0)
            watchdog = watchdog_holder[0]
            if not watchdog.claim_recovery(
                probe_generation,
                lambda: authority.claim_terminal_recovery(probe_generation),
            ):
                return False
            close_stdin_calls.append(True)
            return True

        watchdog = cc_resident.StreamWatchdog(
            stall_timeout=360,
            hard_timeout=3600,
            on_timeout=lambda reason: None,
            is_proc_alive=lambda: True,
            probe_timeout=20,
            on_probe=on_probe,
        )
        watchdog_holder.append(watchdog)
        self._age_watchdog_into_probe_epoch(watchdog)
        first_thread = threading.Thread(target=watchdog._try_probe)
        first_thread.start()
        self.assertTrue(scan_started.wait(1.0))

        # Proof is available, but the provider resumed while the old scan was
        # in flight, so the old generation must be rejected.
        watchdog.note_activity()
        release_scan.set()
        first_thread.join(1.0)
        self.assertFalse(authority.recovery_claimed)
        self.assertEqual([], close_stdin_calls)

        # The next inactivity epoch is eligible and may now claim recovery.
        self._age_watchdog_into_probe_epoch(watchdog)
        self.assertTrue(watchdog._try_probe())
        self.assertTrue(authority.recovery_claimed)
        self.assertEqual([True], close_stdin_calls)

    def test_g_r3_recovery_claim_wins_then_result_recovers_and_tears_down_once(self):
        authority = cc_resident.TurnTerminalAuthority(turn_identity='g-r3')
        close_stdin_calls = []
        sigterm_calls = []
        cleanup_calls = []
        controller = cc_resident.TerminalRecoveryController(
            eof_grace=1,
            sigterm_grace=1,
            hard_deadline=time.monotonic() + 10,
            close_stdin=lambda: close_stdin_calls.append(True),
            send_sigterm=lambda: sigterm_calls.append(True),
            submit_hard_timeout=lambda: None,
            submit_result_missing=lambda: None,
        )
        self.assertTrue(authority.claim_terminal_recovery(3))
        self.assertTrue(controller.start({
            'detected_at': time.time(),
            'provider_last_activity_at': time.time(),
        }))
        self.assertTrue(authority.accept_provider_result(self._authority_receipt()))
        controller.mark_terminal(cc_resident.TurnTerminalAuthority.SUCCESS)
        controller.wait_for_final()
        self.assertTrue(authority.begin_cleanup(
            'terminal_recovery_nonreusable', allow_success=True,
        ))
        cleanup_calls.append(True)

        snapshot = authority.snapshot()
        self.assertEqual('SUCCESS', snapshot['terminal_outcome'])
        self.assertEqual(1, snapshot['cleanup_count'])
        self.assertEqual([True], close_stdin_calls)
        self.assertEqual([], sigterm_calls)
        self.assertEqual([True], cleanup_calls)
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            controller.snapshot()['recovery_stage'],
        )

    def test_g_r4_duplicate_recovery_claim_has_one_owner_and_one_teardown(self):
        authority = cc_resident.TurnTerminalAuthority(turn_identity='g-r4')
        barrier = threading.Barrier(2)
        claims = []
        controller_count = []
        lock = threading.Lock()

        def attempt_claim():
            barrier.wait()
            claimed = authority.claim_terminal_recovery(4)
            with lock:
                claims.append(claimed)
                if claimed:
                    controller_count.append(True)

        threads = [
            threading.Thread(target=attempt_claim),
            threading.Thread(target=attempt_claim),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1.0)

        self.assertEqual([False, True], sorted(claims))
        self.assertEqual([True], controller_count)
        self.assertTrue(authority.accept_provider_result(self._authority_receipt()))
        self.assertTrue(authority.begin_cleanup(
            'terminal_recovery_nonreusable', allow_success=True,
        ))
        self.assertFalse(authority.begin_cleanup(
            'duplicate_recovery_cleanup', allow_success=True,
        ))
        self.assertEqual(1, authority.snapshot()['cleanup_count'])

    def test_c_r8_long_current_turn_tail_proof_recovers(self):
        long_rows = [
            {
                'type': 'system',
                'subtype': 'init',
                'session_id': 'session-race',
            },
            {
                'type': 'assistant',
                'message': {
                    'stop_reason': None,
                    'content': [{'type': 'text', 'text': 'x' * 70000}],
                },
            },
            {
                'type': 'assistant',
                'message': {'stop_reason': 'end_turn'},
            },
        ]
        chunks, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [self._terminal_result_event()],
            'stall',
            signal_on_probe=True,
            transcript_rows=long_rows,
        )
        usage = chunks[0][1][2]
        self.assertEqual('SUCCESS', usage['_obs_terminal_outcome'])
        self.assertEqual(
            'RECOVERED_AFTER_STDIN_EOF',
            usage['_obs_terminal_recovery']['recovery_stage'],
        )
        self.assertEqual([True], kill_calls)

    def test_t14b_provider_error_claim_precedes_watchdog_stall_on_send_turn(self):
        error, session, watchdogs, kill_calls = self._run_deterministic_send_turn(
            [{
                'type': 'result',
                'is_error': True,
                'stop_reason': 'end_turn',
                'result': 'provider failure',
            }],
            'stall',
            signal_on_first_activity=True,
            capture_exception=True,
        )
        self.assertIsInstance(error, cc_resident.ResidentError)
        self.assertEqual('provider_error', error.error_code)
        diagnostics = error.diagnostics
        self.assertEqual('PROVIDER_ERROR', diagnostics['terminal_outcome'])
        self.assertEqual('provider_error', diagnostics['terminal_linearization_source'])
        self.assertEqual(0, diagnostics['actual_stall_accepted_count'])
        self.assertEqual(1, diagnostics['duplicate_terminal_signal_count'])
        self.assertEqual(1, diagnostics['cleanup_count'])
        self.assertEqual([True], kill_calls)

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

    def test_t15_send_turn_generator_exit_kills_once_after_authority(self):
        events = [{
            'type': 'stream_event',
            'event': {
                'type': 'content_block_delta',
                'delta': {'type': 'text_delta', 'text': 'partial'},
            },
        }]
        chunks, session, watchdogs, kill_calls = (
            self._run_deterministic_send_turn(
                events,
                None,
                close_after_first_chunk=True,
            )
        )
        self.assertEqual([('text', 'partial')], chunks)
        self.assertIsNone(watchdogs[0].reason)
        self.assertEqual([True], kill_calls)

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
