"""Regression tests for Claude forge live gate and spike harness (no live API)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claude_forge_core import (
    ForgeOptions,
    collect_event_uuids,
    dump_jsonl,
    forge_transcript,
    load_jsonl,
    new_uuid,
    scan_unknown_uuid_strings,
    verify_work_root,
)
from tools.claude_forge_live_gate import (
    LiveProbeRaw,
    RAW_ASSISTANT_USAGE_OBSERVATION,
    RAW_CONVERSATION_NODE,
    RAW_MALFORMED_CONVERSATION_EVENT,
    build_live_user_prompt,
    classify_raw_jsonl_event,
    decide_verdict,
    evaluate_live_gate,
    generate_history_canary,
    inject_canary_into_history,
    parse_raw_jsonl_append,
    parse_stdout_events,
    prepare_history_for_live_gate,
    project_conversational_events,
    verify_jsonl_append_chain,
    verify_jsonl_prefix_unchanged,
)
from tools.claude_forge_validator import (
    FORGE_TOOL_ORDER,
    FORGE_UUID_REFERENCE_UNKNOWN,
    validate_forged_transcript,
)

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'
USAGE_FIXTURE = ROOT / 'tests' / 'fixtures' / 'cc_usage_history.jsonl'


def _base_events() -> list[dict]:
    sid = new_uuid()
    u, a = new_uuid(), new_uuid()
    return [
        {'type': 'user', 'uuid': u, 'parentUuid': None, 'sessionId': sid,
         'message': {'role': 'user', 'content': 'hi'}},
        {'type': 'assistant', 'uuid': a, 'parentUuid': u, 'sessionId': sid,
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hello'}]}},
    ]


def _jsonl_bytes(events: list[dict]) -> bytes:
    return ''.join(json.dumps(evt, ensure_ascii=False) + '\n' for evt in events).encode('utf-8')


def _metadata(event_type: str, **extra: object) -> dict:
    return {'type': event_type, **extra}


def _usage_observation(
    request_id: str = 'req-usage-observation',
    *,
    input_tokens: int = 3,
) -> dict:
    return {
        'type': 'assistant',
        'requestId': request_id,
        'message': {
            'role': 'assistant',
            'usage': {'input_tokens': input_tokens, 'output_tokens': 1},
        },
    }


def _evaluate_appended(before: list[dict], appended: list[dict], *, canary: str) -> object:
    sid = str(next(evt['sessionId'] for evt in before if evt.get('sessionId')))
    raw = LiveProbeRaw(
        process_started=True,
        exit_code=0,
        assistant_text=canary,
        saw_text_delta=True,
        result_ok=True,
        result_is_error=False,
        stdout_session_id=sid,
    )
    after = before + appended
    return evaluate_live_gate(
        raw=raw,
        expected_session_id=sid,
        canary=canary,
        before_bytes=_jsonl_bytes(before),
        after_bytes=_jsonl_bytes(after),
        before_events=before,
        after_events=after,
    )


class LiveGateTests(unittest.TestCase):
    def test_cc_usage_fixture_rows_are_usage_observations_not_conversation(self) -> None:
        events = load_jsonl(USAGE_FIXTURE)
        assistant_rows = [evt for evt in events if evt.get('type') == 'assistant']
        self.assertEqual(len(assistant_rows), 4)
        self.assertTrue(all(
            classify_raw_jsonl_event(evt).kind == RAW_ASSISTANT_USAGE_OBSERVATION
            for evt in assistant_rows
        ))
        self.assertFalse(project_conversational_events(events))

    def test_usage_observation_in_old_transcript_does_not_affect_gate(self) -> None:
        base = _base_events()
        before = [base[0], _usage_observation(), base[1]]
        sid = str(base[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': base[1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_usage_observation_between_new_user_and_assistant_does_not_affect_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                _usage_observation(),
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_usage_observation_after_new_assistant_does_not_affect_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
                _usage_observation(),
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_duplicate_usage_request_id_warns_without_failing(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        usage = _usage_observation('req-duplicate')
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                usage,
                json.loads(json.dumps(usage)),
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)
        self.assertIn('duplicate_assistant_usage_request_id:req-duplicate', gate.warnings)

    def test_conflicting_usage_request_id_warns_without_failing(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                _usage_observation('req-conflict', input_tokens=3),
                _usage_observation('req-conflict', input_tokens=99),
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)
        self.assertIn('conflicting_assistant_usage_request_id:req-conflict', gate.warnings)

    def test_usage_observation_without_conversation_ids_has_no_bad_uuid_failure(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                _usage_observation(),
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)
        self.assertFalse(any('bad_uuid' in failure for failure in gate.failures))
        self.assertFalse(any('malformed_conversation_event' in failure for failure in gate.failures))

    def test_canonical_assistant_with_usage_remains_conversation_node(self) -> None:
        event = {
            'type': 'assistant',
            'uuid': new_uuid(),
            'parentUuid': new_uuid(),
            'sessionId': new_uuid(),
            'requestId': 'req-canonical',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': 'canonical body'}],
                'usage': {'input_tokens': 1, 'output_tokens': 1},
            },
        }
        self.assertEqual(classify_raw_jsonl_event(event).kind, RAW_CONVERSATION_NODE)
        self.assertEqual(project_conversational_events([event]), [event])

    def test_assistant_role_mismatch_is_malformed_and_fails_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        malformed = {
            'type': 'assistant',
            'uuid': new_uuid(),
            'sessionId': sid,
            'message': {'role': 'user', 'content': 'wrong role'},
        }
        gate = _evaluate_appended(
            before,
            [
                malformed,
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertEqual(
            classify_raw_jsonl_event(malformed).kind,
            RAW_MALFORMED_CONVERSATION_EVENT,
        )
        self.assertFalse(gate.passed)
        self.assertTrue(any('malformed_conversation_event' in failure for failure in gate.failures))

    def test_user_without_uuid_is_malformed_and_fails_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        malformed = {
            'type': 'user',
            'sessionId': sid,
            'message': {'role': 'user', 'content': 'missing uuid'},
        }
        gate = _evaluate_appended(
            before,
            [
                malformed,
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertFalse(gate.passed)
        self.assertTrue(any(
            'malformed_conversation_event' in failure and 'uuid_missing_or_invalid' in failure
            for failure in gate.failures
        ))

    def test_assistant_without_content_or_usage_is_malformed_and_fails_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        malformed = {
            'type': 'assistant',
            'uuid': new_uuid(),
            'sessionId': sid,
            'message': {'role': 'assistant'},
        }
        gate = _evaluate_appended(
            before,
            [
                malformed,
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertFalse(gate.passed)
        self.assertTrue(any(
            'malformed_conversation_event' in failure and 'content_missing' in failure
            for failure in gate.failures
        ))

    def test_malformed_event_alongside_valid_canary_round_fails_entire_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'assistant'},
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertFalse(gate.passed)
        self.assertTrue(any(
            'malformed_conversation_event' in failure and 'message_missing' in failure
            for failure in gate.failures
        ))
        self.assertTrue(gate.canary_matched)

    def test_assistant_without_session_id_is_malformed_and_fails_gate(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        malformed = {
            'type': 'assistant',
            'uuid': new_uuid(),
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'missing session'}]},
        }
        gate = _evaluate_appended(
            before,
            [
                malformed,
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertFalse(gate.passed)
        self.assertTrue(any(
            'malformed_conversation_event' in failure and 'session_id_missing' in failure
            for failure in gate.failures
        ))

    def test_old_uuid_in_usage_observation_still_fails(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        old_uuid = new_uuid()
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        usage = _usage_observation()
        usage['sourceUuid'] = old_uuid
        after = before + [
            {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            usage,
            {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        ok, _, failures = verify_jsonl_append_chain(
            before,
            after,
            expected_session_id=sid,
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
            old_uuids={old_uuid},
        )
        self.assertFalse(ok)
        self.assertTrue(any(FORGE_UUID_REFERENCE_UNKNOWN in failure for failure in failures))

    def test_duplicate_conversation_uuid_still_fails(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        assistant_id = new_uuid()
        after = before + [
            {'type': 'user', 'uuid': before[-1]['uuid'], 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        ok, _, failures = verify_jsonl_append_chain(
            before,
            after,
            expected_session_id=sid,
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
        )
        self.assertFalse(ok)
        self.assertTrue(any('append_duplicate_uuid' in failure for failure in failures))

    def test_conflicting_conversation_assistants_same_parent_still_fail(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id = new_uuid()
        after = before + [
            {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': user_id, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': user_id, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'conflict'}]}},
        ]
        ok, _, failures = verify_jsonl_append_chain(
            before,
            after,
            expected_session_id=sid,
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
        )
        self.assertFalse(ok)
        self.assertTrue(any('append_bad_parent' in failure for failure in failures))

    def test_canary_gate_fails_without_history_match(self) -> None:
        canary = generate_history_canary()
        before = _base_events()
        after = before + [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': before[-1]['uuid'], 'sessionId': before[0]['sessionId'],
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': 'x', 'sessionId': before[0]['sessionId'],
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'wrong-answer'}]}},
        ]
        raw = LiveProbeRaw(
            process_started=True,
            exit_code=0,
            assistant_text='wrong-answer',
            saw_text_delta=True,
            result_ok=True,
            result_is_error=False,
            stdout_session_id=str(before[0]['sessionId']),
        )
        gate = evaluate_live_gate(
            raw=raw,
            expected_session_id=str(before[0]['sessionId']),
            canary=canary,
            before_bytes=json.dumps(before).encode(),
            after_bytes=json.dumps(after).encode(),
            before_events=before,
            after_events=after,
        )
        self.assertFalse(gate.passed)
        self.assertIn('canary_mismatch', gate.failures)

    def test_wrong_session_id_fails(self) -> None:
        canary = 'CANARY-abc'
        before = inject_canary_into_history(_base_events(), canary)
        raw = LiveProbeRaw(
            process_started=True,
            exit_code=0,
            assistant_text=canary,
            saw_text_delta=True,
            result_ok=True,
            result_is_error=False,
            stdout_session_id='other-session-id',
        )
        gate = evaluate_live_gate(
            raw=raw,
            expected_session_id=str(before[0]['sessionId']),
            canary=canary,
            before_bytes=b'[]',
            after_bytes=b'[]',
            before_events=before,
            after_events=before,
        )
        self.assertIn('session_id_mismatch', gate.failures)

    def test_result_ok_but_nonzero_exit_fails(self) -> None:
        canary = 'CANARY-xyz'
        raw = LiveProbeRaw(
            process_started=True,
            exit_code=1,
            assistant_text=canary,
            saw_text_delta=True,
            result_ok=True,
            result_is_error=False,
            stdout_session_id='sid',
        )
        gate = evaluate_live_gate(
            raw=raw,
            expected_session_id='sid',
            canary=canary,
            before_bytes=b'x',
            after_bytes=b'xy',
            before_events=_base_events(),
            after_events=_base_events(),
        )
        self.assertIn('exit_code:1', gate.failures)

    def test_file_replaced_not_appended_fails(self) -> None:
        self.assertFalse(verify_jsonl_prefix_unchanged(b'original', b'replaced'))

    def test_append_bad_session_id_fails(self) -> None:
        before = _base_events()
        canary = generate_history_canary()
        after = before + [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': before[-1]['uuid'],
             'sessionId': 'wrong-session', 'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': before[-1]['uuid'],
             'sessionId': 'wrong-session',
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        ok, reason, failures = verify_jsonl_append_chain(
            before, after,
            expected_session_id=str(before[0]['sessionId']),
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('append_bad_session_id'))
        self.assertTrue(any(f.startswith('append_bad_session_id') for f in failures))

    def test_append_third_new_event_breaks_chain(self) -> None:
        before = _base_events()
        canary = generate_history_canary()
        u1, a1, u2 = new_uuid(), new_uuid(), new_uuid()
        after = before + [
            {'type': 'user', 'uuid': u1, 'parentUuid': before[-1]['uuid'],
             'sessionId': before[0]['sessionId'], 'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'sessionId': before[0]['sessionId'],
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            {'type': 'user', 'uuid': u2, 'parentUuid': before[-1]['uuid'],
             'sessionId': before[0]['sessionId'], 'message': {'role': 'user', 'content': 'orphan'}},
        ]
        ok, reason, failures = verify_jsonl_append_chain(
            before, after,
            expected_session_id=str(before[0]['sessionId']),
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
        )
        self.assertFalse(ok)
        self.assertTrue(any(f.startswith('append_bad_parent') for f in failures))

    def test_all_supported_metadata_without_uuid_pass_projection(self) -> None:
        sid = new_uuid()
        u0, a0, u1, a1 = new_uuid(), new_uuid(), new_uuid(), new_uuid()
        canary = generate_history_canary()
        before = [
            {'type': 'user', 'uuid': u0, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'before'}},
            _metadata('file-history-snapshot', snapshot={'trackedFileBackups': {}}),
            {'type': 'assistant', 'uuid': a0, 'parentUuid': u0, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        appended = [
            {'type': 'user', 'uuid': u1, 'parentUuid': a0, 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            _metadata('queue-operation', operation='dequeue'),
            _metadata('agent-name', agentName='spike'),
            _metadata('custom-title', customTitle='native'),
            _metadata('progress', data={'type': 'hook_progress'}),
            _metadata('system', subtype='turn_duration', durationMs=1),
            {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            _metadata('progress', data={'type': 'done'}),
        ]
        after = before + appended
        raw = LiveProbeRaw(
            process_started=True,
            exit_code=0,
            assistant_text=canary,
            saw_text_delta=True,
            result_ok=True,
            result_is_error=False,
            stdout_session_id=sid,
        )
        gate = evaluate_live_gate(
            raw=raw,
            expected_session_id=sid,
            canary=canary,
            before_bytes=_jsonl_bytes(before),
            after_bytes=_jsonl_bytes(after),
            before_events=before,
            after_events=after,
        )
        self.assertTrue(gate.passed, gate.failures)
        self.assertTrue(gate.raw_append_valid)
        self.assertFalse(any('bad_uuid' in failure for failure in gate.failures))

    def test_metadata_between_existing_conversation_events_is_ignored(self) -> None:
        base = _base_events()
        before = [base[0], _metadata('file-history-snapshot', snapshot={}), base[1]]
        sid = str(base[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': base[1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_metadata_between_new_user_and_assistant_is_ignored(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                _metadata('queue-operation', operation='dequeue'),
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_metadata_after_new_assistant_is_ignored(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        user_id, assistant_id = new_uuid(), new_uuid()
        gate = _evaluate_appended(
            before,
            [
                {'type': 'user', 'uuid': user_id, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
                 'message': {'role': 'user', 'content': build_live_user_prompt()}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': sid,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
                _metadata('progress', data={'phase': 'after-assistant'}),
            ],
            canary=canary,
        )
        self.assertTrue(gate.passed, gate.failures)

    def test_unknown_metadata_is_warning_only(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        u1, a1 = new_uuid(), new_uuid()
        after = before + [
            {'type': 'user', 'uuid': u1, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            _metadata('future-metadata-without-uuid', payload={'safe': True}),
            {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        raw = LiveProbeRaw(
            process_started=True,
            exit_code=0,
            assistant_text=canary,
            saw_text_delta=True,
            result_ok=True,
            result_is_error=False,
            stdout_session_id=sid,
        )
        gate = evaluate_live_gate(
            raw=raw,
            expected_session_id=sid,
            canary=canary,
            before_bytes=_jsonl_bytes(before),
            after_bytes=_jsonl_bytes(after),
            before_events=before,
            after_events=after,
        )
        self.assertTrue(gate.passed, gate.failures)
        self.assertIn('unknown_metadata_type:future-metadata-without-uuid', gate.warnings)

    def test_new_user_must_connect_to_resume_before_conversation_leaf(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        canary = generate_history_canary()
        u1, a1 = new_uuid(), new_uuid()
        after = before + [
            {'type': 'user', 'uuid': u1, 'parentUuid': before[0]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        ok, _, failures = verify_jsonl_append_chain(
            before,
            after,
            expected_session_id=sid,
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
        )
        self.assertFalse(ok)
        self.assertIn('append_user_not_connected_to_old_leaf', failures)

    def test_raw_append_rejects_non_object_json_line(self) -> None:
        before = _jsonl_bytes(_base_events())
        ok, _, failures = parse_raw_jsonl_append(before, before + b'[]\n')
        self.assertFalse(ok)
        self.assertIn('append_not_object:1', failures)

    def test_metadata_old_uuid_leak_fails(self) -> None:
        before = _base_events()
        sid = str(before[0]['sessionId'])
        old_uuid = new_uuid()
        canary = generate_history_canary()
        u1, a1 = new_uuid(), new_uuid()
        after = before + [
            {'type': 'user', 'uuid': u1, 'parentUuid': before[-1]['uuid'], 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            _metadata('progress', leakedReference=old_uuid),
            {'type': 'assistant', 'uuid': a1, 'parentUuid': u1, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]}},
        ]
        ok, _, failures = verify_jsonl_append_chain(
            before,
            after,
            expected_session_id=sid,
            live_user_prompt=build_live_user_prompt(),
            canary=canary,
            old_uuids={old_uuid},
        )
        self.assertFalse(ok)
        self.assertTrue(any(FORGE_UUID_REFERENCE_UNKNOWN in failure for failure in failures))

    def test_stream_event_fixture_parses_nested_delta(self) -> None:
        fixture = FIXTURE_ROOT / 'stream_event_sample.jsonl'
        lines = fixture.read_text(encoding='utf-8').splitlines()
        raw = parse_stdout_events(lines)
        self.assertTrue(raw.saw_text_delta)
        self.assertEqual(raw.assistant_text, 'XY')
        self.assertEqual(raw.stdout_session_id, '00000000-0000-4000-8000-000000000001')

    def test_append_wrong_parent_fails(self) -> None:
        before = _base_events()
        after = before + [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': 'not-leaf', 'sessionId': before[0]['sessionId'],
             'message': {'role': 'user', 'content': 'q'}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': 'x', 'sessionId': before[0]['sessionId'],
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'a'}]}},
        ]
        ok, reason, _ = verify_jsonl_append_chain(
            before, after,
            expected_session_id=str(before[0]['sessionId']),
            live_user_prompt=build_live_user_prompt(),
            canary=generate_history_canary(),
        )
        self.assertFalse(ok)
        self.assertTrue(reason.startswith('append_bad_parent'))

    def test_verdict_2a_2b_both_fail_is_nogo(self) -> None:
        cases = [
            {'case_id': '0', 'state': 'API_ACCEPTED_FIRST_DELTA'},
            {'case_id': '1', 'state': 'API_ACCEPTED_FIRST_DELTA'},
            {'case_id': '2A', 'state': 'RESUME_FAIL'},
            {'case_id': '2B', 'state': 'RESUME_FAIL'},
            {'case_id': '3A', 'state': 'RESUME_FAIL'},
        ]
        verdict, _ = decide_verdict(cases, structural_only=False, live_probe_status='RUN')
        self.assertEqual(verdict, 'NO-GO')

    def test_probe_timeout_uses_harness_runner(self) -> None:
        from tools.claude_forge_subprocess import run_subprocess_with_timeout

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'slow.py'
            script.write_text('import time; time.sleep(30)\n', encoding='utf-8')
            start = time.time()
            result = run_subprocess_with_timeout(
                cmd=[sys.executable, str(script)],
                cwd=tmp,
                env=os.environ.copy(),
                stdin_payload='',
                timeout_seconds=0.3,
            )
            self.assertLess(time.time() - start, 5)
            self.assertTrue(result.timed_out)

    def test_structural_only_ignores_host_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env['ANTHROPIC_API_KEY'] = 'sk-ant-test-fake-key-for-structural-only'
            report_path = Path(tmp) / 'results.json'
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'scripts' / 'spike_claude_forge_resume.py'),
                    '--structural-only',
                    '--work-root', tmp,
                    '--report', str(report_path),
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                env=env,
                check=False,
            )
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            data = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertTrue(data['structural_only'])
            self.assertFalse(data['auth_available'])
            self.assertEqual(data['live_probe_status'], 'NOT_RUN_NO_CREDENTIALS')
            self.assertEqual(data['verdict'], 'NO-GO')
            self.assertIn('tested_tree_sha', data)
            self.assertNotIn('24656ca', json.dumps(data))

    def test_nested_old_uuid_residual_fails(self) -> None:
        old = new_uuid()
        sid = new_uuid()
        events = [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'x'}, 'meta': {'ref': old}},
        ]
        errors = scan_unknown_uuid_strings(events[0], {old})
        self.assertTrue(errors)
        result = validate_forged_transcript(events, session_id=sid, old_uuids={old})
        self.assertFalse(result.ok)
        self.assertTrue(any(FORGE_UUID_REFERENCE_UNKNOWN in e for e in result.errors))

    def test_tool_result_before_tool_use_fails(self) -> None:
        sid = new_uuid()
        u, a = new_uuid(), new_uuid()
        tu = 'toolu_abc123456789012345678'
        events = [
            {'type': 'user', 'uuid': u, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': [
                 {'type': 'tool_result', 'tool_use_id': tu, 'content': 'x'},
             ]}},
            {'type': 'assistant', 'uuid': a, 'parentUuid': u, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [
                 {'type': 'tool_use', 'id': tu, 'name': 'Read', 'input': {}},
             ]}},
        ]
        result = validate_forged_transcript(events, session_id=sid)
        self.assertFalse(result.ok)
        self.assertTrue(any(FORGE_TOOL_ORDER in e for e in result.errors))

    def test_forge_removes_old_uuids_from_output(self) -> None:
        if not FIXTURE_ROOT.is_dir():
            subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'build_claude_forge_fixtures.py')],
                check=True,
            )
        src = load_jsonl(FIXTURE_ROOT / 'case1_plain_two_rounds.jsonl')
        old_uuids = collect_event_uuids(src)
        forged = forge_transcript(
            src,
            ForgeOptions(new_session_id=new_uuid(), cwd='/tmp/x'),
        )
        result = validate_forged_transcript(
            forged.events,
            session_id=forged.events[0]['sessionId'],
            old_uuids=old_uuids,
        )
        self.assertTrue(result.ok, result.errors)

    def test_prepare_history_user_tail_gets_assistant_canary(self) -> None:
        sid = new_uuid()
        user_only = [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': '主链消息'}},
        ]
        canary = generate_history_canary()
        out = prepare_history_for_live_gate(user_only, canary, session_id=sid, cwd='/tmp/x')
        self.assertEqual(out[-1]['type'], 'assistant')
        self.assertIn(canary, json.dumps(out[-1], ensure_ascii=False))

    def test_prepare_history_user_assistant_user_tail_appends_assistant(self) -> None:
        sid = new_uuid()
        u0, a0, u1 = new_uuid(), new_uuid(), new_uuid()
        events = [
            {'type': 'user', 'uuid': u0, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'one'}},
            {'type': 'assistant', 'uuid': a0, 'parentUuid': u0, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'two'}]}},
            {'type': 'user', 'uuid': u1, 'parentUuid': a0, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'three'}},
        ]
        canary = generate_history_canary()
        out = prepare_history_for_live_gate(events, canary, session_id=sid, cwd='/tmp/x')
        self.assertEqual(len(out), len(events) + 1)
        self.assertEqual(out[-1]['type'], 'assistant')
        self.assertEqual(out[-1]['parentUuid'], u1)
        self.assertEqual(out[1], events[1])

    def test_prepare_history_metadata_tail_uses_conversation_leaf(self) -> None:
        events = _base_events()
        events.append(_metadata('progress', data={'phase': 'tail'}))
        canary = generate_history_canary()
        out = prepare_history_for_live_gate(
            events,
            canary,
            session_id=str(events[0]['sessionId']),
            cwd='/tmp/x',
        )
        self.assertEqual(len(out), len(events))
        self.assertEqual(out[-1], events[-1])
        self.assertIn(canary, json.dumps(out[-2], ensure_ascii=False))

    def test_prepare_history_user_leaf_before_metadata_appends_assistant(self) -> None:
        sid = new_uuid()
        user_id = new_uuid()
        events = [
            {'type': 'user', 'uuid': user_id, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'tail user'}},
            _metadata('queue-operation', operation='enqueue'),
        ]
        canary = generate_history_canary()
        out = prepare_history_for_live_gate(events, canary, session_id=sid, cwd='/tmp/x')
        self.assertEqual(out[-1]['type'], 'assistant')
        self.assertEqual(out[-1]['parentUuid'], user_id)
        self.assertEqual(out[-2], events[-1])

    def test_live_prompt_does_not_contain_canary(self) -> None:
        canary = generate_history_canary()
        prompt = build_live_user_prompt()
        self.assertNotIn(canary, prompt)

    def test_symlink_middle_component_rejected_on_dump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'root'
            root.mkdir()
            mid = root / 'mid'
            mid.mkdir()
            real = root / 'real-target'
            real.mkdir()
            link = mid / 'link'
            try:
                link.symlink_to(real)
            except OSError as exc:
                self.skipTest(f'symlink unavailable: {exc}')
            out = link / 'out.jsonl'
            events = [{'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': new_uuid(),
                       'message': {'role': 'user', 'content': 'x'}}]
            with self.assertRaises(ValueError):
                dump_jsonl(out, events, allowed_output_root=root)

    def test_normal_root_atomic_write_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / 'nested' / 'out.jsonl'
            sid = new_uuid()
            events = [{'type': 'user', 'uuid': new_uuid(), 'parentUuid': None, 'sessionId': sid,
                       'message': {'role': 'user', 'content': 'x'}}]
            digest = dump_jsonl(out, events, allowed_output_root=root)
            self.assertTrue(out.is_file())
            self.assertEqual(len(digest), 64)


if __name__ == '__main__':
    unittest.main()
