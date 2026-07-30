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
    forge_transcript,
    load_jsonl,
    new_uuid,
    scan_unknown_uuid_strings,
)
from tools.claude_forge_live_gate import (
    LiveProbeRaw,
    build_live_user_prompt,
    decide_verdict,
    evaluate_live_gate,
    generate_history_canary,
    inject_canary_into_history,
    verify_jsonl_append_chain,
    verify_jsonl_prefix_unchanged,
)
from tools.claude_forge_validator import (
    FORGE_TOOL_ORDER,
    FORGE_UUID_REFERENCE_UNKNOWN,
    validate_forged_transcript,
)

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'


def _base_events() -> list[dict]:
    sid = new_uuid()
    u, a = new_uuid(), new_uuid()
    return [
        {'type': 'user', 'uuid': u, 'parentUuid': None, 'sessionId': sid,
         'message': {'role': 'user', 'content': 'hi'}},
        {'type': 'assistant', 'uuid': a, 'parentUuid': u, 'sessionId': sid,
         'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'hello'}]}},
    ]


class LiveGateTests(unittest.TestCase):
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

    def test_append_wrong_parent_fails(self) -> None:
        before = _base_events()
        after = before + [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': 'not-leaf', 'sessionId': before[0]['sessionId'],
             'message': {'role': 'user', 'content': 'q'}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': 'x', 'sessionId': before[0]['sessionId'],
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'a'}]}},
        ]
        ok, reason = verify_jsonl_append_chain(before, after)
        self.assertFalse(ok)
        self.assertEqual(reason, 'append_user_parent_not_old_leaf')

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

    def test_probe_timeout_does_not_hang(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(30)'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        start = time.time()
        reader = threading.Thread(
            target=lambda: proc.stdout.readline() if proc.stdout else None,
            daemon=True,
        )
        reader.start()
        reader.join(timeout=0.2)
        proc.kill()
        proc.wait(timeout=2)
        self.assertLess(time.time() - start, 5)

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

    def test_live_prompt_does_not_contain_canary(self) -> None:
        canary = generate_history_canary()
        prompt = build_live_user_prompt()
        self.assertNotIn(canary, prompt)
        self.assertNotIn('CANARY-', prompt)


if __name__ == '__main__':
    unittest.main()
