"""R1 diagnostics for resident usage provenance and shared Wake cleanup."""

import io
import json
import sys
import threading
import types
import unittest
from unittest import mock

import cc_resident
from chat import unified_heartbeat_a1 as uh
from tools.cc_jsonl_usage import attach_jsonl_usage


class _NoopWatchdog:
    def __init__(self, **kwargs):
        self.fired_reason = None

    def start(self):
        return None

    def stop(self):
        return None

    def note_activity(self):
        return None


class _FakeProc:
    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(
            ''.join(json.dumps(event) + '\n' for event in events)
        )
        self.stderr = io.StringIO()
        self.pid = 4242

    def poll(self):
        return None

    def terminate(self):
        return None

    def wait(self, timeout=None):
        return None

    def kill(self):
        return None


def _stream_event(event_type, usage=None, **extra):
    event = {'type': event_type}
    event.update(extra)
    if usage is not None:
        event['usage'] = dict(usage)
    if event_type == 'message_start':
        event.setdefault('message', {})['usage'] = dict(usage or {})
    return {'type': 'stream_event', 'event': event}


def _usage(input_tokens, output_tokens, cache_read=0, cache_creation=0):
    return {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'cache_read_input_tokens': cache_read,
        'cache_creation_input_tokens': cache_creation,
    }


def _assistant(request_id, usage, content=None):
    return {
        'type': 'assistant',
        'requestId': request_id,
        'message': {
            'usage': dict(usage),
            'content': list(content or []),
        },
    }


def _wake_logs(lines):
    return [
        json.loads(line.split('[WAKE-LIVE] ', 1)[1])
        for line in lines
        if '[WAKE-LIVE] ' in line
    ]


class ResidentUsageProvenanceTests(unittest.TestCase):
    def _run(self, events):
        resident = cc_resident.ResidentSession('/tmp', '', '/tmp/cc-tools.json')
        resident._proc = _FakeProc(events)
        with (
            mock.patch.object(cc_resident, 'StreamWatchdog', _NoopWatchdog),
            mock.patch.object(resident, '_commit_sent_context'),
            mock.patch.object(
                resident,
                '_attach_jsonl_usage_with_retry',
                side_effect=lambda usage, *args, **kwargs: usage,
            ),
        ):
            with self.assertLogs('cc_resident', level='INFO') as captured:
                output = list(resident.send_turn(
                    'INTERNAL_PROMPT_SENTINEL',
                    diagnostic_wake_run_id='wake-r1-test',
                ))
        return output, _wake_logs(captured.output), '\n'.join(captured.output)

    def test_message_start_usage_is_recorded(self):
        _, logs, _ = self._run([
            _stream_event(
                'message_start',
                _usage(3, 0, cache_read=10, cache_creation=20),
            ),
            {'type': 'result'},
        ])
        event = next(
            row for row in logs
            if row.get('stage') == 'ROUND_USAGE'
            and row.get('event_source') == 'message_start'
        )
        self.assertEqual(event['round_index'], 1)
        self.assertEqual(event['input_tokens'], 3)
        self.assertEqual(event['cache_read_input_tokens'], 10)
        self.assertEqual(event['cache_creation_input_tokens'], 20)
        self.assertFalse(event['round_already_complete'])

    def test_message_delta_usage_is_recorded(self):
        _, logs, _ = self._run([
            _stream_event(
                'message_start',
                _usage(1, 0),
            ),
            _stream_event(
                'message_delta',
                _usage(1, 7, cache_read=11, cache_creation=12),
            ),
            {'type': 'result'},
        ])
        event = next(
            row for row in logs
            if row.get('stage') == 'ROUND_USAGE'
            and row.get('event_source') == 'message_delta'
        )
        self.assertEqual(event['output_tokens'], 7)
        self.assertEqual(event['cache_read_input_tokens'], 11)
        self.assertEqual(event['cache_creation_input_tokens'], 12)
        self.assertEqual(event['current_round_after']['output_tokens'], 7)

    def test_assistant_usage_is_recorded(self):
        _, logs, _ = self._run([
            _stream_event('message_start', _usage(1, 0)),
            _assistant('req-r1', _usage(1, 9)),
            {'type': 'result'},
        ])
        event = next(
            row for row in logs
            if row.get('stage') == 'ROUND_USAGE'
            and row.get('event_source') == 'assistant'
        )
        self.assertEqual(event['provider_request_id'], 'req-r1')
        self.assertEqual(event['output_tokens'], 9)
        self.assertEqual(event['current_round_after']['output_tokens'], 9)

    def test_result_without_usage_logs_false(self):
        _, logs, _ = self._run([
            _stream_event('message_start', _usage(1, 0)),
            {'type': 'result'},
        ])
        event = next(
            row for row in logs
            if row.get('stage') == 'ROUND_USAGE'
            and row.get('event_source') == 'result'
        )
        self.assertFalse(event['usage_present'])
        self.assertIsNone(event['input_tokens'])
        self.assertIsNone(event['output_tokens'])
        self.assertFalse(event['round_already_complete'])

    def test_close_reasons_include_tool_use_and_provider_result(self):
        _, logs, _ = self._run([
            _stream_event('message_start', _usage(3, 0)),
            _assistant(
                'req-r1',
                _usage(3, 44),
                content=[{
                    'type': 'tool_use',
                    'id': 'tool-r1',
                    'name': 'read_only',
                    'input': {'secret': 'ARGS_SENTINEL'},
                }],
            ),
            _stream_event('message_start', _usage(1, 0)),
            _assistant('req-r2', _usage(1, 1153)),
            {'type': 'result'},
        ])
        closes = [
            row for row in logs if row.get('stage') == 'ROUND_CLOSE'
        ]
        self.assertEqual(
            [row['close_reason'] for row in closes],
            ['assistant_tool_use', 'provider_result'],
        )
        self.assertEqual(closes[0]['final_usage']['output_tokens'], 44)
        self.assertEqual(closes[1]['final_usage']['output_tokens'], 1153)

    def test_result_usage_is_not_applied_after_closed_round(self):
        output, logs, _ = self._run([
            _stream_event('message_start', _usage(3, 0)),
            _assistant(
                'req-r1',
                _usage(3, 44),
                content=[{
                    'type': 'tool_use',
                    'id': 'tool-r1',
                    'name': 'read_only',
                    'input': {'secret': 'ARGS_SENTINEL'},
                }],
            ),
            {
                'type': 'result',
                'requestId': 'req-r1',
                'usage': _usage(3, 53),
            },
        ])
        done = next(payload for event, payload in output if event == 'done')
        stream_usage = done[2]
        result_event = next(
            row for row in logs
            if row.get('stage') == 'ROUND_USAGE'
            and row.get('event_source') == 'result'
        )
        self.assertEqual(stream_usage['output_tokens'], 44)
        self.assertEqual(result_event['output_tokens'], 53)
        self.assertTrue(result_event['round_already_complete'])
        self.assertIsNone(result_event['current_round_before'])
        self.assertEqual(result_event['round_index'], 1)

    def test_provenance_logs_exclude_content(self):
        _, _, rendered = self._run([
            _stream_event('message_start', _usage(1, 0)),
            _assistant(
                'req-r1',
                _usage(1, 9),
                content=[{
                    'type': 'tool_use',
                    'id': 'tool-r1',
                    'name': 'read_only',
                    'input': {'secret': 'ARGS_SENTINEL'},
                }],
            ),
            {
                'type': 'result',
                'result': 'RESULT_SENTINEL',
                'usage': _usage(1, 9),
            },
        ])
        self.assertNotIn('INTERNAL_PROMPT_SENTINEL', rendered)
        self.assertNotIn('ARGS_SENTINEL', rendered)
        self.assertNotIn('RESULT_SENTINEL', rendered)


class JsonlRequestDiagnosticsTests(unittest.TestCase):
    def test_jsonl_requests_are_usage_only_and_do_not_replace_stream_totals(self):
        stream = {
            'input_tokens': 4,
            'output_tokens': 1197,
            'cache_read': 155763,
            'cache_creation': 3570,
            'rounds': [],
        }
        replay = {
            'request_count': 2,
            'request_ids': ['req-1', 'req-2'],
            'records': [
                {
                    'request_id': 'req-1',
                    'input_tokens': 3,
                    'output_tokens': 44,
                    'cache_read': 76139,
                    'cache_creation': 3485,
                },
                {
                    'request_id': 'req-2',
                    'input_tokens': 1,
                    'output_tokens': 1162,
                    'cache_read': 79624,
                    'cache_creation': 85,
                },
                ],
            'totals': {
                'input_tokens': 4,
                'output_tokens': 1206,
                'cache_read': 155763,
                'cache_creation': 3570,
            },
            'duplicate_rows_ignored': 0,
            'conflicting_duplicate_rows': 0,
            'invalid_json_rows': 0,
        }
        merged = attach_jsonl_usage(stream, replay)
        self.assertEqual(merged['output_tokens'], 1197)
        self.assertEqual(
            merged['jsonl_usage']['jsonl_requests'],
            [
                {
                    'request_id': 'req-1',
                    'input_tokens': 3,
                    'output_tokens': 44,
                    'cache_read': 76139,
                    'cache_creation': 3485,
                },
                {
                    'request_id': 'req-2',
                    'input_tokens': 1,
                    'output_tokens': 1162,
                    'cache_read': 79624,
                    'cache_creation': 85,
                },
            ],
        )
        self.assertFalse(merged['jsonl_usage']['stream_totals_match'])


class CleanupProofTests(unittest.TestCase):
    def test_cleanup_begin_and_end_are_visible(self):
        binding = types.SimpleNamespace(
            context_id=12,
            context_epoch=3,
            resident_generation=4,
            resident_key='chat:3:4',
        )
        state = {'binding': binding}
        resident = types.SimpleNamespace(
            generation=4,
            _proc=types.SimpleNamespace(pid=9001),
            alive=True,
        )
        resident._alive = lambda: resident.alive
        gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            _gen_cond=threading.Condition(),
            _gen_busy=False,
            _gen_pending_delivery=None,
        )

        def close(_resident, *, expected_key):
            self.assertEqual(expected_key, binding.resident_key)
            resident.alive = False
            state['binding'] = None
            return True

        with (
            mock.patch('config_store.get_bool', return_value=True),
            mock.patch.object(uh.dr, 'get_local_binding', side_effect=lambda: state['binding']),
            mock.patch.object(
                uh.dr,
                'close_local_resident_if_bound',
                side_effect=close,
            ),
            mock.patch.dict(sys.modules, {'gateway': gateway}),
        ):
            with self.assertLogs('chat.unified_heartbeat_a1', level='INFO') as captured:
                result = uh.retire_shared_resident_after_failed_delivery(
                    cache_info={
                        'provider': 'claude_code',
                        'source': 'wake',
                        'b3_authority': True,
                    },
                    window_identity={
                        'context_id': 12,
                        'context_epoch': 3,
                        'resident_generation': 4,
                    },
                    reason='normal_wake_main_chat_jsonl_not_final',
                )

        self.assertTrue(result)
        events = _wake_logs(captured.output)
        self.assertEqual(
            [row['stage'] for row in events],
            ['RESIDENT_CLEANUP_BEGIN', 'RESIDENT_CLEANUP_END'],
        )
        self.assertEqual(events[0]['resident_generation'], 4)
        self.assertEqual(events[0]['resident_pid'], 9001)
        self.assertTrue(events[0]['local_binding_present'])
        self.assertEqual(
            events[0]['reason'],
            'normal_wake_main_chat_jsonl_not_final',
        )
        self.assertFalse(events[1]['resident_alive'])
        self.assertFalse(events[1]['local_binding_present'])
        self.assertTrue(events[1]['close_return'])


if __name__ == '__main__':
    unittest.main()
