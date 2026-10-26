import json
import pathlib
import sqlite3
import threading
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


    def test_normal_success_emits_one_canonical_terminal(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='stream-success',
        )
        recorder.record('provider_started', dedupe=True)
        recorder.record(
            'provider_request_observed',
            _request_identity='provider-request-1',
        )
        recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
        )
        with self.assertLogs('gateway', level='INFO') as captured:
            self.assertTrue(gateway._emit_turn_flight_terminal_once(recorder))
            self.assertFalse(gateway._emit_turn_flight_terminal_once(recorder))
        lines = [
            line for line in captured.output if 'turn_flight_terminal ' in line
        ]
        self.assertEqual(1, len(lines))
        payload = json.loads(
            lines[0].split('turn_flight_terminal ', 1)[1],
        )
        self.assertEqual('success', payload['terminal_status'])
        self.assertEqual(
            recorder.turn_observation_id,
            payload['turn_observation_id'],
        )


    def test_cancel_after_stdin_reports_last_completed_stage_before_terminal(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        for stage in (
            'turn_created',
            'resident_turn_started',
            'stdin_begin',
            'stdin_flushed',
        ):
            recorder.record(stage, dedupe=True)
        recorder.record(
            'turn_cancelled',
            terminal_reason='client_stream_cancelled_after_stdin',
        )
        recorder.record('generation_lock_released', dedupe=True)
        snapshot = recorder.terminal(
            terminal_status='cancelled',
            terminal_reason='client_stream_cancelled_after_stdin',
        )
        self.assertEqual('stdin_flushed', snapshot['last_completed_stage'])
        self.assertEqual('provider_started', snapshot['next_missing_stage'])
        self.assertNotEqual('turn_cancelled', snapshot['last_completed_stage'])
        self.assertNotEqual('turn_closed', snapshot['last_completed_stage'])
        self.assertTrue(any(
            event['event'] == 'generation_lock_released'
            for event in snapshot['events']
        ))


    def test_success_summary_uses_last_progress_stage_not_terminal_meta(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        for stage in (
            'turn_created',
            'user_message_persisted',
            'generation_lock_acquired',
            'resident_selected',
            'resident_turn_started',
            'stdin_begin',
            'stdin_flushed',
            'provider_started',
            'first_output',
            'provider_terminal',
            'assistant_persisted',
            'terminal_mapping_committed',
        ):
            recorder.record(stage, dedupe=True)
        snapshot = recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
        )
        self.assertEqual('terminal_mapping_committed', snapshot['last_completed_stage'])
        self.assertIsNone(snapshot['next_missing_stage'])
        self.assertNotEqual('turn_closed', snapshot['last_completed_stage'])


    def test_provider_started_without_request_identity_reports_zero(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        recorder.record('provider_started', dedupe=True)
        snapshot = recorder.terminal(terminal_status='failed')
        self.assertEqual(0, snapshot['provider_request_count'])


    def test_provider_request_count_deduplicates_three_request_ids(self):
        recorder = TurnFlightRecorder(user_message_id=13166)
        for request_id in ('provider-a', 'provider-a', 'provider-b', 'provider-c', 'provider-c'):
            recorder.record(
                'provider_request_observed',
                _request_identity=request_id,
            )
        snapshot = recorder.terminal(terminal_status='success')
        self.assertEqual(3, snapshot['provider_request_count'])
        self.assertEqual(
            3,
            len([
                event for event in snapshot['events']
                if event['event'] == 'provider_request_observed'
            ]),
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn('provider-a', serialized)
        self.assertNotIn('provider-b', serialized)
        self.assertNotIn('provider-c', serialized)


    def test_reconnect_does_not_emit_second_canonical_terminal(self):
        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='stream-original',
        )
        recorder.terminal(
            terminal_status='success',
            assistant_message_id=13168,
            assistant_persisted=True,
            mapping_committed=True,
        )
        with self.assertLogs('gateway', level='INFO') as captured:
            self.assertTrue(gateway._emit_turn_flight_terminal_once(recorder))
            recorder.record(
                'same_turn_reconnect_detected',
                stream_instance_id='stream-reconnect',
                stream_role='reconnect',
            )
            self.assertFalse(gateway._emit_turn_flight_terminal_once(recorder))
        self.assertEqual(
            1,
            len([line for line in captured.output if 'turn_flight_terminal ' in line]),
        )

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
        with self.assertLogs('gateway', level='INFO') as captured:
            self.assertTrue(gateway._emit_terminal_contract_diag_once(turn_data))
            self.assertFalse(gateway._emit_terminal_contract_diag_once(turn_data))
        canonical_lines = [
            line for line in captured.output if 'turn_flight_terminal ' in line
        ]
        contract_lines = [
            line for line in captured.output if 'chat_terminal_contract ' in line
        ]
        self.assertEqual(1, len(canonical_lines))
        self.assertEqual(1, len(contract_lines))
        self.assertEqual(
            'client_stream_cancelled_after_stdin',
            diag['terminal_reason'],
        )
        canonical = json.loads(
            canonical_lines[0].split('turn_flight_terminal ', 1)[1],
        )
        contract = json.loads(
            contract_lines[0].split('chat_terminal_contract ', 1)[1],
        )
        self.assertEqual(
            canonical['turn_observation_id'],
            contract['turn_observation_id'],
        )
        self.assertIn('turn_observation_id', contract_lines[0])
        self.assertIn('flight_snapshot', contract_lines[0])

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
            'provider_request_observed',
            _request_identity='provider-reconnect-1',
        )
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
    def _run_reconnect(
        self,
        *,
        active_turn,
        persisted_assistant=None,
        active_recorder=None,
    ):
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
                gateway, '_gen_active_recorder', new=active_recorder,
            ))
            stack.enter_context(mock.patch.object(
                gateway,
                '_gen_active_identity',
                new=(
                    gateway._gen_recorder_identity(active_recorder)
                    if active_recorder is not None else None
                ),
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

    def test_persisted_reconnect_is_bounded_by_next_user_turn(self):
        def lookup(rows):
            def one(user_message_id):
                conn = sqlite3.connect(':memory:')
                conn.row_factory = sqlite3.Row
                conn.execute(
                    """
                    CREATE TABLE daily_message_contexts (
                        message_id INTEGER PRIMARY KEY,
                        context_id INTEGER NOT NULL,
                        context_epoch INTEGER NOT NULL,
                        role TEXT NOT NULL
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO daily_message_contexts
                        (message_id, context_id, context_epoch, role)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
                try:
                    with mock.patch.object(gateway, 'get_db', return_value=conn):
                        return gateway._find_persisted_assistant_after(
                            user_message_id,
                        )
                finally:
                    conn.close()
            return one(13166), one(13168)

        self.assertEqual(
            (13167, 13169),
            lookup([
                (13166, 32, 30, 'user'),
                (13167, 32, 30, 'assistant'),
                (13168, 32, 30, 'user'),
                (13169, 32, 30, 'assistant'),
            ]),
        )
        self.assertEqual(
            (None, 13169),
            lookup([
                (13166, 32, 30, 'user'),
                (13168, 32, 30, 'user'),
                (13169, 32, 30, 'assistant'),
            ]),
        )
        self.assertEqual(
            (None, 13169),
            lookup([
                (13166, 32, 30, 'user'),
                (13167, 33, 31, 'assistant'),
                (13168, 32, 30, 'user'),
                (13169, 32, 30, 'assistant'),
            ]),
        )


    def test_reconnect_wait_stops_when_active_turn_changes(self):
        import moments_turn

        recorder = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='reconnect-active-change',
        )
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                gateway, '_find_persisted_assistant_after', return_value=None,
            ))
            active_lookup = stack.enter_context(mock.patch.object(
                moments_turn,
                'get_active_turn',
                side_effect=[
                    {
                        'user_message_id': 13166,
                        'turn_key': 'turn-13166',
                    },
                    {
                        'user_message_id': 13167,
                        'turn_key': 'turn-13167',
                    },
                ],
            ))
            sleep = stack.enter_context(mock.patch.object(gateway.time, 'sleep'))
            chunks = list(gateway._stream_same_turn_reconnect_wait(
                user_message_id=13166,
                recorder=recorder,
                stream_instance_id='reconnect-active-change',
                timeout_sec=30,
            ))
        self.assertTrue(chunks)
        self.assertIn('same_turn_reconnect', ''.join(chunks))
        self.assertEqual(2, active_lookup.call_count)
        sleep.assert_called_once_with(0.5)
        self.assertNotIn('13167', ''.join(chunks))


    def test_persisted_reconnect_does_not_adopt_newer_active_recorder(self):
        newer = TurnFlightRecorder(
            user_message_id=13167,
            turn_key='turn-13167',
            stream_instance_id='newer-active',
        )
        before = newer.events()
        self._run_reconnect(
            active_turn=None,
            persisted_assistant=13168,
            active_recorder=newer,
        )
        self.assertEqual(before, newer.events())


    def test_different_turn_waiter_never_reuses_previous_turn_result(self):
        owner = TurnFlightRecorder(
            user_message_id=13166,
            turn_key='turn-13166',
            stream_instance_id='owner',
        )
        waiter = TurnFlightRecorder(
            user_message_id=13167,
            turn_key='turn-13167',
            stream_instance_id='waiter',
        )
        identity_owner = {
            'user_message_id': 13166,
            'turn_key': 'turn-13166',
        }
        identity_waiter = {
            'user_message_id': 13167,
            'turn_key': 'turn-13167',
        }
        with gateway._gen_cond:
            old_state = (
                gateway._gen_busy,
                gateway._gen_busy_since,
                gateway._gen_last_result,
                gateway._gen_last_result_identity,
                gateway._gen_pending_delivery,
                gateway._gen_active_recorder,
                gateway._gen_active_identity,
                gateway._gen_owner_token,
                gateway._gen_owner_fence_required,
            )
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time()
            gateway._gen_last_result = None
            gateway._gen_last_result_identity = None
            gateway._gen_pending_delivery = None
            gateway._gen_active_recorder = owner
            gateway._gen_active_identity = gateway._gen_logical_identity(identity_owner)
            gateway._gen_owner_token = None
            gateway._gen_owner_fence_required = False
        result = {}

        def wait_for_turn():
            result['value'] = gateway._gen_acquire_or_wait(
                wait_timeout=2,
                flight_recorder=waiter,
                logical_identity=identity_waiter,
            )

        thread = threading.Thread(target=wait_for_turn)
        thread.start()
        deadline = time.time() + 1
        while time.time() < deadline and not waiter.events():
            time.sleep(0.001)
        self.assertTrue(waiter.events())
        gateway._gen_release(
            ('owner text', 'owner thinking'),
            flight_recorder=owner,
        )
        thread.join(2)
        self.assertFalse(thread.is_alive())
        try:
            self.assertEqual(('own', None), result['value'])
            self.assertTrue(any(
                event['event'] == 'generation_lock_acquired'
                for event in waiter.events()
            ))
            self.assertFalse(any(
                event['event'] == 'generation_lock_reused'
                for event in waiter.events()
            ))
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_last_result_identity,
                    gateway._gen_pending_delivery,
                    gateway._gen_active_recorder,
                    gateway._gen_active_identity,
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
                ) = old_state


    def test_stale_owner_after_force_unlock_cannot_release_new_owner(self):
        owner_a = TurnFlightRecorder(user_message_id=13166, turn_key='turn-a')
        owner_b = TurnFlightRecorder(user_message_id=13167, turn_key='turn-b')
        identity_a = {'user_message_id': 13166, 'turn_key': 'turn-a'}
        identity_b = {'user_message_id': 13167, 'turn_key': 'turn-b'}
        old_state = (
            gateway._gen_busy,
            gateway._gen_busy_since,
            gateway._gen_last_result,
            gateway._gen_last_result_identity,
            gateway._gen_pending_delivery,
            gateway._gen_active_recorder,
            gateway._gen_active_identity,
            gateway._gen_owner_token,
            gateway._gen_owner_fence_required,
        )
        try:
            with gateway._gen_cond:
                gateway._gen_busy = False
                gateway._gen_last_result = None
                gateway._gen_last_result_identity = None
                gateway._gen_pending_delivery = None
                gateway._gen_active_recorder = None
                gateway._gen_active_identity = None
                gateway._gen_owner_token = None
                gateway._gen_owner_fence_required = False
            token_a = {}
            self.assertEqual(
                ('own', None),
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=owner_a,
                    logical_identity=identity_a,
                    owner_token_out=token_a,
                ),
            )
            self.assertIsNotNone(token_a.get('token'))
            self.assertTrue(gateway._gen_release(None, force=True))

            token_b = {}
            self.assertEqual(
                ('own', None),
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=owner_b,
                    logical_identity=identity_b,
                    owner_token_out=token_b,
                ),
            )
            self.assertNotEqual(token_a['token'], token_b['token'])
            before = (
                gateway._gen_last_result,
                gateway._gen_active_recorder,
                gateway._gen_active_identity,
            )
            self.assertFalse(gateway._gen_release(
                ('stale A result', ''),
                expected_owner_token=token_a['token'],
                flight_recorder=owner_a,
            ))
            self.assertTrue(gateway._gen_busy)
            self.assertIs(owner_b, gateway._gen_active_recorder)
            self.assertEqual(
                gateway._gen_logical_identity(identity_b),
                gateway._gen_active_identity,
            )
            self.assertEqual(before, (
                gateway._gen_last_result,
                gateway._gen_active_recorder,
                gateway._gen_active_identity,
            ))
            self.assertTrue(gateway._gen_release(
                ('B result', ''),
                expected_owner_token=token_b['token'],
                flight_recorder=owner_b,
            ))
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_last_result_identity,
                    gateway._gen_pending_delivery,
                    gateway._gen_active_recorder,
                    gateway._gen_active_identity,
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
                ) = old_state


    def test_stale_owner_release_cannot_clear_newer_lock_after_client_disconnect(self):
        owner_a = TurnFlightRecorder(user_message_id=13166, turn_key='turn-a')
        owner_b = TurnFlightRecorder(user_message_id=13167, turn_key='turn-b')
        token_a = {}
        token_b = {}
        old_state = (
            gateway._gen_busy,
            gateway._gen_busy_since,
            gateway._gen_last_result,
            gateway._gen_last_result_identity,
            gateway._gen_pending_delivery,
            gateway._gen_active_recorder,
            gateway._gen_active_identity,
            gateway._gen_owner_token,
            gateway._gen_owner_fence_required,
        )
        try:
            with gateway._gen_cond:
                gateway._gen_busy = False
                gateway._gen_last_result = None
                gateway._gen_last_result_identity = None
                gateway._gen_pending_delivery = None
                gateway._gen_active_recorder = None
                gateway._gen_active_identity = None
                gateway._gen_owner_token = None
                gateway._gen_owner_fence_required = False
            self.assertEqual(
                ('own', None),
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=owner_a,
                    logical_identity={'user_message_id': 13166, 'turn_key': 'turn-a'},
                    owner_token_out=token_a,
                ),
            )
            self.assertTrue(gateway._gen_release(
                None,
                expected_owner_token=token_a['token'],
                flight_recorder=owner_a,
            ))
            self.assertEqual(
                ('own', None),
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=owner_b,
                    logical_identity={'user_message_id': 13167, 'turn_key': 'turn-b'},
                    owner_token_out=token_b,
                ),
            )
            self.assertFalse(gateway._gen_release(
                ('late A result', ''),
                expected_owner_token=token_a['token'],
                flight_recorder=owner_a,
            ))
            self.assertTrue(gateway._gen_busy)
            self.assertIs(owner_b, gateway._gen_active_recorder)
            self.assertTrue(gateway._gen_release(
                ('B result', ''),
                expected_owner_token=token_b['token'],
                flight_recorder=owner_b,
            ))
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_last_result_identity,
                    gateway._gen_pending_delivery,
                    gateway._gen_active_recorder,
                    gateway._gen_active_identity,
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
                ) = old_state


    def test_force_cancel_does_not_release_owner_that_replaced_observed_target(self):
        owner_a = TurnFlightRecorder(user_message_id=13166, turn_key='turn-a')
        owner_b = TurnFlightRecorder(user_message_id=13167, turn_key='turn-b')
        identity_a = {'user_message_id': 13166, 'turn_key': 'turn-a'}
        identity_b = {'user_message_id': 13167, 'turn_key': 'turn-b'}
        token_a = {}
        token_b = {}
        old_state = (
            gateway._gen_busy,
            gateway._gen_busy_since,
            gateway._gen_last_result,
            gateway._gen_last_result_identity,
            gateway._gen_pending_delivery,
            gateway._gen_active_recorder,
            gateway._gen_active_identity,
            gateway._gen_owner_token,
            gateway._gen_owner_fence_required,
        )
        original_release = gateway._gen_release
        try:
            with gateway._gen_cond:
                gateway._gen_busy = False
                gateway._gen_last_result = None
                gateway._gen_last_result_identity = None
                gateway._gen_pending_delivery = None
                gateway._gen_active_recorder = None
                gateway._gen_active_identity = None
                gateway._gen_owner_token = None
                gateway._gen_owner_fence_required = False
            self.assertEqual(
                ('own', None),
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=owner_a,
                    logical_identity=identity_a,
                    owner_token_out=token_a,
                ),
            )

            def turnover_release(result, **kwargs):
                if kwargs.get('force'):
                    self.assertTrue(original_release(
                        None,
                        expected_owner_token=token_a['token'],
                        flight_recorder=owner_a,
                    ))
                    self.assertEqual(
                        ('own', None),
                        gateway._gen_acquire_or_wait(
                            wait_timeout=0,
                            flight_recorder=owner_b,
                            logical_identity=identity_b,
                            owner_token_out=token_b,
                        ),
                    )
                    with gateway._gen_cond:
                        gateway._gen_last_result = ('B current', '')
                        gateway._gen_last_result_identity = (
                            gateway._gen_logical_identity(identity_b)
                        )
                return original_release(result, **kwargs)

            with mock.patch.object(
                gateway, '_gen_release', side_effect=turnover_release,
            ):
                with gateway.app.test_client() as client:
                    response = client.post(
                        '/chat/cancel',
                        json={'force': True, 'source': 'target-fence-test'},
                    )
                    payload = response.get_json()

            self.assertFalse(payload['released'])
            self.assertTrue(payload['owner_changed'])
            with gateway._gen_cond:
                self.assertTrue(gateway._gen_busy)
                self.assertEqual(token_b['token'], gateway._gen_owner_token)
                self.assertIs(owner_b, gateway._gen_active_recorder)
                self.assertEqual(
                    gateway._gen_logical_identity(identity_b),
                    gateway._gen_active_identity,
                )
                self.assertEqual(('B current', ''), gateway._gen_last_result)
                self.assertEqual(
                    gateway._gen_logical_identity(identity_b),
                    gateway._gen_last_result_identity,
                )
            self.assertFalse(any(
                event.get('event') == 'force_unlock_applied'
                and event.get('unlock_applied')
                for event in owner_a.events()
            ))
            self.assertTrue(original_release(
                ('B result', ''),
                expected_owner_token=token_b['token'],
                flight_recorder=owner_b,
            ))
        finally:
            with gateway._gen_cond:
                current_busy = gateway._gen_busy
                current_token = gateway._gen_owner_token
                current_recorder = gateway._gen_active_recorder
            if current_busy and current_token is not None:
                original_release(
                    None,
                    expected_owner_token=current_token,
                    flight_recorder=current_recorder,
                )
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_last_result_identity,
                    gateway._gen_pending_delivery,
                    gateway._gen_active_recorder,
                    gateway._gen_active_identity,
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
                ) = old_state


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
            old_result_identity = gateway._gen_last_result_identity
            old_recorder = gateway._gen_active_recorder
            old_identity = gateway._gen_active_identity
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time()
            gateway._gen_last_result = None
            gateway._gen_last_result_identity = None
            gateway._gen_active_recorder = original
            gateway._gen_active_identity = gateway._gen_logical_identity({
                'user_message_id': 13166,
                'turn_key': 'turn-13166',
            })
            gateway._gen_owner_token = None
            gateway._gen_owner_fence_required = False
        try:
            with self.assertRaises(RuntimeError):
                gateway._gen_acquire_or_wait(
                    wait_timeout=0,
                    flight_recorder=recorder,
                    logical_identity={
                        'user_message_id': 13167,
                        'turn_key': 'turn-13167',
                    },
                )
        finally:
            with gateway._gen_cond:
                gateway._gen_busy = old_busy
                gateway._gen_busy_since = old_since
                gateway._gen_last_result = old_result
                gateway._gen_last_result_identity = old_result_identity
                gateway._gen_active_recorder = old_recorder
                gateway._gen_active_identity = old_identity
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
            gateway._gen_last_result_identity,
            gateway._gen_active_recorder,
            gateway._gen_active_identity,
            gateway._gen_owner_token,
            gateway._gen_owner_fence_required,
        )
        with gateway._gen_cond:
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time() - 999
            gateway._gen_last_result = None
            gateway._gen_last_result_identity = None
            gateway._gen_active_recorder = recorder
            gateway._gen_active_identity = gateway._gen_recorder_identity(recorder)
            gateway._gen_owner_token = None
            gateway._gen_owner_fence_required = False
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
            self.assertTrue(all(
                field not in event
                for event in recorder.events()
                if event['event'] == 'force_unlock_requested'
                for field in ('owner_alive', 'resident_active', 'turn_terminal')
            ))
        finally:
            with gateway._gen_cond:
                (
                    gateway._gen_busy,
                    gateway._gen_busy_since,
                    gateway._gen_last_result,
                    gateway._gen_last_result_identity,
                    gateway._gen_active_recorder,
                    gateway._gen_active_identity,
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
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
            gateway._gen_owner_token,
            gateway._gen_owner_fence_required,
        )
        with gateway._gen_cond:
            gateway._gen_busy = True
            gateway._gen_busy_since = time.time() - 999
            gateway._gen_last_result = None
            gateway._gen_active_recorder = recorder
            gateway._gen_owner_token = None
            gateway._gen_owner_fence_required = False
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
                    gateway._gen_owner_token,
                    gateway._gen_owner_fence_required,
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
