"""Focused tests for the provider cache freshness clock contract."""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from unittest import mock

import cc_resident
from chat import daily_runtime


class _NoopWatchdog:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        return None

    def stop(self):
        return None

    def note_activity(self):
        return None


class _FakeStdin:
    def __init__(self, session, *, error=None):
        self.session = session
        self.error = error
        self.write_count = 0
        self.flush_count = 0
        self.fields_at_write = []

    def write(self, payload):
        self.write_count += 1
        self.fields_at_write.append((
            self.session.last_cache_refresh_at,
            self.session.last_cache_refresh_monotonic,
        ))
        if self.error is not None:
            raise self.error
        return len(payload)

    def flush(self):
        self.flush_count += 1


class _FakeStdout:
    def __init__(self, events):
        self.lines = [
            json.dumps(event, ensure_ascii=False) + '\n'
            for event in events
        ]

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        return ''


class _FakeProc:
    def __init__(self, session, events, *, write_error=None):
        self.stdin = _FakeStdin(session, error=write_error)
        self.stdout = _FakeStdout(events)
        self.pid = 731
        self.returncode = None

    def poll(self):
        return self.returncode


def _result_event(*, is_error=False, stop_reason='end_turn'):
    return {
        'type': 'result',
        'session_id': 'session-1',
        'stop_reason': stop_reason,
        'is_error': is_error,
        'result': 'ok' if not is_error else 'failed',
        'usage': {
            'input_tokens': 11,
            'output_tokens': 7,
        },
    }


def _resident(events, *, write_error=None):
    # Exercise the real ResidentSession lifecycle contract.  These focused
    # fixtures override process/cache fields below, but must not bypass
    # __init__ now that turn/runtime serialization lives there.
    session = cc_resident.ResidentSession(
        cwd='.',
        allowed_tools='',
        mcp_config_path='',
    )
    session._tool_profile = cc_resident.TOOL_PROFILE_LEGACY
    session._proc = None
    session._session_id = 'session-1'
    session._generation = 4
    session._cold = True
    session._last_used = 123.0
    session._last_cache_refresh_at = 10.0
    session._last_cache_refresh_monotonic = 20.0
    session._reset_session_meta(respawn_reason=None)
    session._session_id = 'session-1'
    session._generation = 4
    session._proc = _FakeProc(session, events, write_error=write_error)
    session._kill = mock.Mock()
    session._attach_jsonl_usage_with_retry = mock.Mock(
        side_effect=lambda usage, *args, **kwargs: usage,
    )
    return session


class CacheFreshnessClockTests(unittest.TestCase):
    def test_new_generation_and_reset_clear_clock(self):
        session = _resident([_result_event()])
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)
        session.commit_cache_freshness(wall_at=100.0, monotonic_at=200.0)
        session._reset_session_meta(respawn_reason='history_rewrite')
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)

    def test_commit_is_atomic_pair_and_does_not_change_last_used(self):
        session = _resident([_result_event()])
        session._last_used = 321.0
        self.assertTrue(session.commit_cache_freshness(
            wall_at=100.0,
            monotonic_at=200.0,
        ))
        self.assertEqual(session.last_cache_refresh_at, 100.0)
        self.assertEqual(session.last_cache_refresh_monotonic, 200.0)
        self.assertEqual(session._last_used, 321.0)

    def test_invalid_commit_is_ignored(self):
        session = _resident([_result_event()])
        self.assertFalse(session.commit_cache_freshness(
            wall_at=float('nan'),
            monotonic_at=200.0,
        ))
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)

    def test_successful_send_captures_candidate_before_write_without_committing(self):
        session = _resident([_result_event()])
        with mock.patch.object(cc_resident.StreamWatchdog, '__init__', return_value=None), \
             mock.patch.object(cc_resident.StreamWatchdog, 'start'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'stop'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'note_activity'), \
             mock.patch('tools.cc_jsonl_usage.snapshot_session_jsonl', return_value=None), \
             mock.patch.object(cc_resident.time, 'time', return_value=100.0), \
             mock.patch.object(cc_resident.time, 'monotonic', return_value=200.0):
            events = list(session.send_turn('hello'))
        done = [payload for event, payload in events if event == 'done']
        self.assertEqual(len(done), 1)
        usage = done[0][2]
        self.assertEqual(
            session._proc.stdin.fields_at_write,
            [(None, None)],
        )
        self.assertEqual(usage._candidate_cache_refresh_at, 100.0)
        self.assertEqual(usage._candidate_cache_refresh_monotonic, 200.0)
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)

    def test_broken_pipe_does_not_update_clock(self):
        session = _resident(
            [_result_event()],
            write_error=BrokenPipeError('closed'),
        )
        session.commit_cache_freshness(wall_at=10.0, monotonic_at=20.0)
        with mock.patch.object(cc_resident.StreamWatchdog, '__init__', return_value=None), \
             mock.patch('tools.cc_jsonl_usage.snapshot_session_jsonl', return_value=None), \
             mock.patch.object(cc_resident.time, 'time', return_value=100.0), \
             mock.patch.object(cc_resident.time, 'monotonic', return_value=200.0):
            with self.assertRaises(cc_resident.ResidentError):
                list(session.send_turn('hello'))
        self.assertEqual(session.last_cache_refresh_at, 10.0)
        self.assertEqual(session.last_cache_refresh_monotonic, 20.0)

    def test_provider_terminal_failure_does_not_update_clock(self):
        session = _resident([_result_event(is_error=True)])
        session.commit_cache_freshness(wall_at=10.0, monotonic_at=20.0)
        with mock.patch.object(cc_resident.StreamWatchdog, '__init__', return_value=None), \
             mock.patch.object(cc_resident.StreamWatchdog, 'start'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'stop'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'note_activity'), \
             mock.patch('tools.cc_jsonl_usage.snapshot_session_jsonl', return_value=None), \
             mock.patch.object(cc_resident.time, 'time', return_value=100.0), \
             mock.patch.object(cc_resident.time, 'monotonic', return_value=200.0):
            with self.assertRaises(cc_resident.ResidentError):
                list(session.send_turn('hello'))
        self.assertEqual(session.last_cache_refresh_at, 10.0)
        self.assertEqual(session.last_cache_refresh_monotonic, 20.0)

    def test_jsonl_finality_failure_never_commits_in_send_turn(self):
        session = _resident([_result_event()])
        session._attach_jsonl_usage_with_retry = mock.Mock(
            return_value={
                'jsonl_usage': {'stream_totals_match': False},
                'rounds': [],
            },
        )
        with mock.patch.object(cc_resident.StreamWatchdog, '__init__', return_value=None), \
             mock.patch.object(cc_resident.StreamWatchdog, 'start'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'stop'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'note_activity'), \
             mock.patch('tools.cc_jsonl_usage.snapshot_session_jsonl', return_value=None):
            events = list(session.send_turn('hello'))
        usage = [payload for event, payload in events if event == 'done'][0][2]
        self.assertFalse(usage['jsonl_usage']['stream_totals_match'])
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)

    def test_tool_rounds_have_one_candidate_carrier(self):
        events = [
            {
                'type': 'stream_event',
                'event': {
                    'type': 'message_start',
                    'message': {'usage': {'input_tokens': 1}},
                },
            },
            {
                'type': 'assistant',
                'message': {
                    'content': [{'type': 'tool_use', 'id': 'tool-1', 'name': 'read', 'input': {}}],
                },
            },
            {
                'type': 'user',
                'message': {
                    'content': [{'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'ok'}],
                },
            },
            _result_event(),
        ]
        session = _resident(events)
        with mock.patch.object(cc_resident.StreamWatchdog, '__init__', return_value=None), \
             mock.patch.object(cc_resident.StreamWatchdog, 'start'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'stop'), \
             mock.patch.object(cc_resident.StreamWatchdog, 'note_activity'), \
             mock.patch('tools.cc_jsonl_usage.snapshot_session_jsonl', return_value=None):
            output = list(session.send_turn('hello'))
        done = [payload for event, payload in output if event == 'done'][0]
        usage = done[2]
        self.assertIsNotNone(usage._candidate_cache_refresh_at)
        self.assertIsNotNone(usage._candidate_cache_refresh_monotonic)
        self.assertEqual(session._proc.stdin.write_count, 1)

    def test_private_candidate_is_not_serialized_in_usage_mapping(self):
        usage = cc_resident.ResidentTurnUsage(
            {'cache_read': 2},
            candidate_cache_refresh_at=100.0,
            candidate_cache_refresh_monotonic=200.0,
        )
        encoded = json.dumps(dict(usage))
        self.assertNotIn('_candidate_cache_refresh_at', encoded)
        self.assertNotIn('_candidate_cache_refresh_monotonic', encoded)

    def test_capacity_swap_contract_tracks_freshness_for_rollback(self):
        self.assertIn(
            '_last_cache_refresh_at',
            daily_runtime._CAPACITY_SWAP_PROCESS_IDENTITY,
        )
        self.assertIn(
            '_last_cache_refresh_monotonic',
            daily_runtime._CAPACITY_SWAP_PROCESS_IDENTITY,
        )

    def test_last_used_semantics_remain_independent(self):
        session = _resident([_result_event()])
        session._last_used = 777.0
        session.commit_cache_freshness(wall_at=1000.0, monotonic_at=2000.0)
        self.assertEqual(session._last_used, 777.0)


if __name__ == '__main__':
    unittest.main()


ROOT = Path(__file__).resolve().parents[1]


def _source_file(name):
    return (ROOT / name).read_text(encoding='utf-8')


def _function_source(text, name, indent=''):
    marker = indent + 'def ' + name + '('
    start = text.index(marker)
    next_marker = '\n' + indent + 'def '
    end = text.find(next_marker, start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def _assert_order(testcase, source, *markers):
    positions = [source.index(marker) for marker in markers]
    testcase.assertEqual(positions, sorted(positions), msg='order: %r' % (markers,))


class CacheFreshnessBoundaryTests(unittest.TestCase):
    def test_send_turn_has_no_commit_authority(self):
        source = _function_source(
            _source_file('cc_resident.py'), 'send_turn', indent='    ',
        )
        self.assertNotIn('commit_cache_freshness(', source)

    def test_candidate_capture_is_single_and_before_write(self):
        source = _function_source(
            _source_file('cc_resident.py'), '_send_turn_impl', indent='    ',
        )
        self.assertEqual(
            source.count('candidate_cache_refresh_at = time.time()'), 1,
        )
        _assert_order(
            self,
            source,
            'candidate_cache_refresh_at = time.time()',
            'proc.stdin.write(payload + NL)',
        )

    def test_multi_round_candidate_is_first_request_metadata(self):
        source = _function_source(
            _source_file('cc_resident.py'), '_send_turn_impl', indent='    ',
        )
        self.assertEqual(
            source.count('candidate_cache_refresh_monotonic = time.monotonic()'),
            1,
        )
        self.assertIn(
            'candidate_cache_refresh_at=candidate_cache_refresh_at',
            source,
        )

    def test_formal_chat_success_commits_once_after_handle_success(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_stream_cc_daily_soft_window',
        )
        self.assertEqual(source.count('commit_cache_freshness('), 1)
        _assert_order(
            self,
            source,
            'build_canonical_turn_for_plan(',
            'persist_daily_assistant_for_plan(',
            'handle_provider_success(',
            'commit_cache_freshness(',
        )

    def test_chat_canonical_failure_precedes_commit(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_stream_cc_daily_soft_window',
        )
        self.assertLess(
            source.index('build_canonical_turn_for_plan('),
            source.index('commit_cache_freshness('),
        )

    def test_chat_mapping_failure_cannot_precede_commit(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_stream_cc_daily_soft_window',
        )
        self.assertLess(
            source.index('handle_provider_success('),
            source.index('commit_cache_freshness('),
        )

    def test_chat_assistant_persist_failure_precedes_commit(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_stream_cc_daily_soft_window',
        )
        self.assertLess(
            source.index('assistant_persist_failed'),
            source.index('commit_cache_freshness('),
        )

    def test_chat_cursor_cas_failure_precedes_commit(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_stream_cc_daily_soft_window',
        )
        self.assertLess(
            source.index('cursor_cas_conflict'),
            source.index('commit_cache_freshness('),
        )

    def test_unified_wake_success_commits_once_after_watermark(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_run_unified_normal_main_chat_turn',
        )
        self.assertEqual(source.count('commit_cache_freshness('), 1)
        _assert_order(
            self,
            source,
            'stream_totals_match',
            'commit_shared_transcript_watermark(',
            'commit_cache_freshness(',
        )

    def test_unified_wake_jsonl_failure_precedes_commit(self):
        source = _function_source(
            _source_file('gateway.py'),
            '_run_unified_normal_main_chat_turn',
        )
        self.assertLess(
            source.index('normal_wake_main_chat_jsonl_not_final'),
            source.index('commit_cache_freshness('),
        )

    def test_unified_wake_watermark_failure_precedes_commit(self):
        source = _function_source(
            _source_file('chat/behavior_authority_b3.py'),
            '_try_invoke_shared_renderer',
        )
        _assert_order(
            self,
            source,
            'commit_shared_transcript_watermark(',
            'commit_cache_freshness(',
        )
        self.assertLess(
            source.index('uh_a1_transcript_watermark_commit_failed'),
            source.index('commit_cache_freshness('),
        )

    def test_unified_wake_skip_has_no_freshness_commit(self):
        source = _function_source(
            _source_file('chat/behavior_authority_b3.py'),
            '_try_invoke_shared_renderer',
        )
        self.assertIn('UnifiedNormalWakeSharedUnavailable', source)
        self.assertIn('return None', source)
        self.assertEqual(source.count('commit_cache_freshness('), 1)

    def test_legacy_wake_runner_does_not_touch_shared_clock(self):
        source = _source_file('wake/runners.py')
        self.assertNotIn('commit_cache_freshness(', source)
        self.assertNotIn('_last_cache_refresh_at', source)

    def test_shared_wake_private_candidate_is_not_cache_info(self):
        from chat import behavior_authority_b3 as b3

        class FakeResident:
            _model_identity = 'test-model'

            def send_turn(self, payload, *, turn_lease):
                del payload, turn_lease
                usage = cc_resident.ResidentTurnUsage(
                    {
                        'jsonl_usage': {'stream_totals_match': True},
                        'public': 'ok',
                    },
                    candidate_cache_refresh_at=101.0,
                    candidate_cache_refresh_monotonic=202.0,
                )
                yield 'done', ('hello', '', usage)

        renderer_input = b3.RendererInput(
            selected_intent='reconnect_gently',
            selected_action='message',
            content_target='wake_message',
            persona_context='persona',
            continuity_facts='facts',
            decision_identity={'wake_run_id': 'test-wake'},
        )
        with mock.patch(
            'chat.cc_history_rewrite.guard_cc_generation',
            side_effect=lambda events: events,
        ):
            result = b3.invoke_renderer_cc_hot(
                renderer_input=renderer_input,
                resident=FakeResident(),
                turn_lease={},
            )
        self.assertEqual(result['_cache_refresh_candidate_at'], 101.0)
        self.assertEqual(result['_cache_refresh_candidate_monotonic'], 202.0)
        self.assertNotIn('_cache_refresh_candidate_at', result['cache_info'])
        self.assertNotIn('_cache_refresh_candidate_monotonic', result['cache_info'])

    def test_shared_wake_fake_resident_models_commit_surface(self):
        class FakeResident:
            def __init__(self):
                self.calls = []

            def commit_cache_freshness(self, *, wall_at, monotonic_at):
                self.calls.append((wall_at, monotonic_at))
                return True

        resident = FakeResident()
        self.assertTrue(callable(resident.commit_cache_freshness))
        self.assertTrue(resident.commit_cache_freshness(
            wall_at=1.0, monotonic_at=2.0,
        ))
        self.assertEqual(resident.calls, [(1.0, 2.0)])

    def test_capacity_swap_identity_contains_both_clock_fields(self):
        identity = daily_runtime._CAPACITY_SWAP_PROCESS_IDENTITY
        self.assertIn('_last_cache_refresh_at', identity)
        self.assertIn('_last_cache_refresh_monotonic', identity)

    def test_capacity_swap_new_generation_is_cleared(self):
        session = _resident([_result_event()])
        session.commit_cache_freshness(wall_at=100.0, monotonic_at=200.0)
        session._reset_session_meta(respawn_reason='capacity_swap')
        self.assertIsNone(session.last_cache_refresh_at)
        self.assertIsNone(session.last_cache_refresh_monotonic)

    def test_capacity_swap_rollback_can_restore_both_fields(self):
        session = _resident([_result_event()])
        session.commit_cache_freshness(wall_at=100.0, monotonic_at=200.0)
        saved = {
            key: getattr(session, key)
            for key in (
                '_last_cache_refresh_at',
                '_last_cache_refresh_monotonic',
            )
        }
        session._reset_session_meta(respawn_reason='capacity_swap')
        for key, value in saved.items():
            setattr(session, key, value)
        self.assertEqual(session.last_cache_refresh_at, 100.0)
        self.assertEqual(session.last_cache_refresh_monotonic, 200.0)

    def test_capacity_forging_and_health_do_not_refresh_clock(self):
        source = _source_file('chat/daily_runtime.py')
        reprepare = _function_source(source, 'reprepare_after_capacity_swap')
        self.assertNotIn('commit_cache_freshness(', reprepare)
        self.assertNotIn('time.time()', reprepare)
