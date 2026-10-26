import pathlib
import time
import unittest
from contextlib import ExitStack
from unittest import mock

import gateway
from chat.turn_flight_recorder import TurnFlightRecorder


class TurnFlightRecorderTests(unittest.TestCase):
    def test_normal_success_has_one_terminal_snapshot(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            request_id='req-13166',
            context_id=32,
            context_epoch=30,
            resident_generation=154,
            resident_turn_sequence=7,
            process_generation=9,
            stream_instance_id='stream-a',
        )
        for stage in (
            'turn_created',
            'user_message_persisted',
            'generation_lock_acquired',
            'resident_selected',
            'resident_turn_started',
            'stdin_begin',
            'stdin_flushed',
            'provider_started',
            'provider_request_id_seen',
            'first_output',
            'provider_terminal',
            'assistant_persisted',
            'terminal_mapping_committed',
            'turn_reconciled',
            'generation_lock_released',
        ):
            recorder.record(stage, dedupe=True)
        first = recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
            reconcile_applied=True,
            turn_terminal=True,
        )
        second = recorder.terminal(
            terminal_status='failed',
            error_code='must_not_replace_success',
        )
        self.assertEqual(first, second)
        self.assertEqual('success', first['terminal_status'])
        self.assertEqual(13166, first['user_message_id'])
        self.assertEqual(1, first['stream_count'])
        self.assertTrue(first['assistant_persisted'])
        self.assertTrue(first['mapping_committed'])
        self.assertEqual('turn_closed', first['event'])

    def test_daily_cancellation_snapshot_keeps_existing_contract(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='stream-a',
        )
        turn_data = {
            'user_message_id': 13166,
            'request_id': 'req-13166',
            '_turn_flight_recorder': recorder,
        }
        state = {
            'user_message_id': 13166,
            'request_id': 'req-13166',
            'resident_stream_entered': True,
            'stdin_write_started': True,
            'stdin_flushed': True,
            'provider_event_seen': False,
            'provider_request_id_seen': False,
            'first_text_or_thinking_seen': False,
            'terminal_reason': 'client_stream_cancelled_after_stdin',
        }
        diag = gateway._record_daily_stream_cancellation(
            turn_data,
            state,
            cleanup_action='abort_daily_turn',
            abort_requested=True,
            respawn_requested=True,
        )
        with self.assertLogs('gateway', level='WARNING') as captured:
            self.assertTrue(gateway._emit_terminal_contract_diag_once(turn_data))
            self.assertFalse(gateway._emit_terminal_contract_diag_once(turn_data))
        lines = [line for line in captured.output if 'chat_terminal_contract ' in line]
        self.assertEqual(1, len(lines))
        self.assertEqual(
            'client_stream_cancelled_after_stdin',
            diag['terminal_reason'],
        )
        self.assertIn('turn_observation_id', lines[0])
        self.assertIn('flight_snapshot', lines[0])

    def test_network_reconnects_share_one_logical_terminal_summary(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='stream-a',
            stream_role='original',
        )
        recorder.record('generation_lock_acquired', dedupe=True)
        recorder.record('provider_started', dedupe=True)
        recorder.record(
            'same_turn_reconnect_detected',
            stream_instance_id='stream-b',
            stream_role='reconnect',
        )
        recorder.record(
            'same_turn_reconnect_detected',
            stream_instance_id='stream-c',
            stream_role='reconnect',
        )
        recorder.record(
            'turn_reconciled',
            stream_instance_id='stream-c',
            stream_role='reconnect',
            reconcile_applied=True,
        )
        snapshot = recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
        )
        self.assertEqual(3, snapshot['stream_count'])
        self.assertEqual(2, snapshot['same_turn_collisions'])
        self.assertEqual(1, snapshot['provider_request_count'])
        self.assertEqual(13168, snapshot['assistant_message_id'])

    def test_tool_failure_does_not_fail_successful_turn(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        recorder.record('tool_call_started', tool_name='read_board')
        recorder.record(
            'tool_call_failed',
            tool_name='read_board',
            success=False,
            latency_ms=42,
        )
        recorder.record('provider_terminal')
        recorder.record(
            'assistant_persisted',
            assistant_message_id=13168,
            assistant_persisted=True,
        )
        snapshot = recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
        )
        self.assertEqual('success', snapshot['terminal_status'])
        self.assertFalse(any(
            event['event'] == 'turn_failed'
            for event in snapshot['events']
        ))
        self.assertEqual('read_board', snapshot['events'][0]['tool_name'])

    def test_projection_mismatch_can_reconcile_before_success(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        recorder.record(
            'turn_reconciled',
            reconcile_applied=True,
            reason='projection_mismatch',
        )
        snapshot = recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
            reconcile_applied=True,
        )
        self.assertTrue(snapshot['reconcile_applied'])
        self.assertEqual('success', snapshot['terminal_status'])


class SameTurnReconnectRouteTests(unittest.TestCase):
    def _run_reconnect(self, *, active_turn, persisted_assistant=None):
        import moments_turn

        prepared = {
            'content': '',
            'user_message_id': 13166,
            'request_id': 'req-reconnect',
            'turn_key': 'new-stream-key',
        }
        wait_result = (
            'data: {"t":"done","ok":true,"same_turn_reconnect":true}\n\n'
        )
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                gateway, '_get_provider', return_value='claude_code',
            ))
            stack.enter_context(mock.patch.object(
                moments_turn, 'prepare_turn', return_value=dict(prepared),
            ))
            stack.enter_context(mock.patch.object(
                moments_turn, 'insert_user_message', return_value=dict(prepared),
            ))
            stack.enter_context(mock.patch.object(
                moments_turn, 'get_active_turn', return_value=active_turn,
            ))
            activate = stack.enter_context(mock.patch.object(
                moments_turn, 'activate_turn',
                side_effect=AssertionError('same-turn reconnect activated a new turn'),
            ))
            release = stack.enter_context(mock.patch.object(
                moments_turn, 'release_turn',
            ))
            acquire = stack.enter_context(mock.patch.object(
                gateway, '_gen_acquire_or_wait',
                side_effect=AssertionError('same-turn reconnect acquired generation lock'),
            ))
            find = stack.enter_context(mock.patch.object(
                gateway, '_find_persisted_assistant_after',
                return_value=persisted_assistant,
            ))
            wait = stack.enter_context(mock.patch.object(
                gateway, '_stream_same_turn_reconnect_wait',
                return_value=iter([wait_result]),
            ))
            with gateway.app.test_client() as client:
                response = client.post(
                    '/chat/stream',
                    json={'user_message_id': 13166},
                    buffered=False,
                )
                body = b''.join(response.response).decode('utf-8')
                response.close()

        self.assertIn('same_turn_reconnect', body)
        activate.assert_not_called()
        release.assert_not_called()
        acquire.assert_not_called()
        find.assert_not_called() if active_turn is not None else find.assert_called_once()
        wait.assert_called_once()
        self.assertEqual(13166, wait.call_args.kwargs['user_message_id'])

    def test_same_turn_reconnect_while_original_provider_alive_does_not_start_second_turn(self):
        self._run_reconnect(
            active_turn={
                'conversation_id': 'default',
                'turn_key': 'original-key',
                'user_message_id': 13166,
                'started_at': '2026-10-26 12:00:00',
            },
        )

    def test_same_turn_reconnect_after_original_persistence_reuses_result(self):
        self._run_reconnect(
            active_turn=None,
            persisted_assistant=13168,
        )

    def test_different_turn_collision_keeps_lock_path_distinct(self):
        recorder = TurnFlightRecorder(
            user_message_id=13167,
            turn_key='turn-13167',
            stream_instance_id='stream-b',
        )
        original = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='stream-a',
        )
        with gateway._gen_cond:
            old_busy = gateway._gen_busy
            old_since = gateway._gen_busy_since
            old_result = gateway._gen_last_result
            old_recorder = gateway._gen_active_recorder
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time()
            gateway._gen_last_result = None
            gateway._gen_active_recorder = original
        try:
            with self.assertRaises(RuntimeError):
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=recorder,
                )
        finally:
            with gateway._gen_cond:
                gateway._gen_busy = old_busy
                gateway._gen_busy_since = old_since
                gateway._gen_last_result = old_result
                gateway._gen_active_recorder = old_recorder
        events = recorder.events()
        self.assertEqual('generation_lock_wait_begin', events[0]['event'])
        self.assertFalse(any(
            event['event'] == 'same_turn_reconnect_detected'
            for event in events
        ))
        self.assertEqual('13167', str(recorder.terminal(
            terminal_status='failed',
            error_code='different_turn_generation_lock_busy',
        )['user_message_id']))

    def test_non_force_unlock_never_uses_age_as_zombie_proof(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
        )
        old_state = (
            gateway._gen_busy,
            gateway._gen_busy_since,
            gateway._gen_last_result,
            gateway._gen_active_recorder,
        )
        with gateway._gen_cond:
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time() - 999
            gateway._gen_last_result = None
            gateway._gen_active_recorder = recorder
        try:
            with mock.patch.object(
                gateway, '_gen_release',
                side_effect=AssertionError('non-force unlock released by age'),
            ):
                with gateway.app.test_client() as client:
                    response = client.post(
                        '/chat/cancel',
                        json={'force': False, 'source': 'test_non_force'},
                    )
                    payload = response.get_json()
            self.assertTrue(payload['skipped'])
            self.assertEqual('age_is_observation_only', payload['reason'])
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_active_recorder,
                ) = old_state

    def test_explicit_force_unlock_is_recorded_and_allowed(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
        )
        old_state = (
            gateway._gen_busy,
            gateway._gen_busy_since,
            gateway._gen_last_result,
            gateway._gen_active_recorder,
        )
        with gateway._gen_cond:
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time() - 999
            gateway._gen_last_result = None
            gateway._gen_active_recorder = recorder
        try:
            with mock.patch.object(gateway, '_gen_release', return_value=True) as release:
                with gateway.app.test_client() as client:
                    response = client.post(
                        '/chat/cancel',
                        json={'force': True, 'source': 'explicit_test'},
                    )
                    payload = response.get_json()
            self.assertTrue(payload['released'])
            release.assert_called_once()
            names = [event['event'] for event in recorder.events()]
            self.assertIn('force_unlock_requested', names)
            self.assertIn('force_unlock_applied', names)
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_active_recorder,
                ) = old_state

    def test_legacy_age_only_unlock_is_removed_from_legacy_path(self):
        source = (
            pathlib.Path(__file__).resolve().parents[1] / 'static' / 'chat.html'
        ).read_text(encoding='utf-8')
        self.assertNotIn(
            'if(d.busy&&d.age_sec>maxAge) releaseGenLock(true);',
            source,
        )
        self.assertIn('age is observation only', source)

    def test_mutation_same_turn_detection_is_required(self):
        source = pathlib.Path(gateway.__file__).read_text(encoding='utf-8')
        self.assertIn('same_turn_reconnect_detected', source)
        self.assertIn('_stream_same_turn_reconnect_wait', source)


if __name__ == '__main__':
    unittest.main()
