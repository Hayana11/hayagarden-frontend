"""Integration tests for spike harness with mocked live runner (no API)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.spike_claude_forge_resume as spike  # noqa: E402
from tools.claude_forge_core import dump_jsonl, load_jsonl, new_uuid, session_jsonl_path_for_cwd, verify_work_root  # noqa: E402
from tools.claude_forge_live_gate import (  # noqa: E402
    build_live_user_prompt,
    generate_history_canary,
    parse_stdout_events,
    prepare_history_for_live_gate,
)
from tools.claude_forge_subprocess import run_subprocess_with_timeout  # noqa: E402

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'
REQUIRED_CASES = ('0', '1', '2A', '2B', '3A', '5B', '6', '7')
FORBIDDEN_STATES = {'NOT_IMPLEMENTED_IN_SPIKE', 'LIVE_SKIPPED_NO_ASSISTANT'}
OLD_SHA = '24656ca'
OLD_PROMPT = '请只回复两个字：收到'


def _stream_stdout(canary: str, session_id: str) -> list[str]:
    return [
        json.dumps({
            'type': 'stream_event',
            'event': {'delta': {'type': 'text_delta', 'text': canary}},
        }),
        json.dumps({
            'type': 'assistant',
            'sessionId': session_id,
            'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]},
        }),
        json.dumps({'type': 'result', 'is_error': False, 'sessionId': session_id}),
    ]


class _MockProc:
    def __init__(self, *, stdout_lines: list[str], exit_code: int = 0, delay: float = 0):
        self._stdout = io.StringIO('\n'.join(stdout_lines) + '\n')
        self.stdin = io.StringIO()
        self._stderr = io.StringIO()
        self._exit_code = exit_code
        self._delay = delay
        self.pid = 424242
        self._done = False

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    def poll(self):
        return None if not self._done else self._exit_code

    def wait(self, timeout=None):
        if self._delay:
            time.sleep(self._delay)
        self._done = True
        return self._exit_code

    def kill(self):
        self._done = True
        return None


def _append_live_round(jsonl_path: Path, *, session_id: str, canary: str) -> None:
    events = load_jsonl(jsonl_path)
    old_leaf = events[-1]['uuid']
    u_id = new_uuid()
    a_id = new_uuid()
    user_evt = {
        'type': 'user', 'uuid': u_id, 'parentUuid': old_leaf, 'sessionId': session_id,
        'message': {'role': 'user', 'content': build_live_user_prompt()},
    }
    asst_evt = {
        'type': 'assistant', 'uuid': a_id, 'parentUuid': u_id, 'sessionId': session_id,
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': canary}]},
    }
    new_bytes = (
        json.dumps(user_evt, ensure_ascii=False) + '\n'
        + json.dumps(asst_evt, ensure_ascii=False) + '\n'
    ).encode('utf-8')
    with jsonl_path.open('ab') as handle:
        handle.write(new_bytes)


def _extract_canary(events: list[dict]) -> str:
    for evt in reversed(events):
        msg = evt.get('message') or {}
        content = msg.get('content')
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        marker = '[history-canary:'
        if marker in text:
            return text.rsplit(marker, 1)[1].split(']', 1)[0]
    return generate_history_canary()


def _mock_popen_factory(work_root: Path, claude_home: Path, isolated_cwd: Path):
    def factory(cmd, **kwargs):
        resume_sid = None
        for i, part in enumerate(cmd):
            if part == '--resume' and i + 1 < len(cmd):
                resume_sid = cmd[i + 1]
        session_id = resume_sid or new_uuid()
        path = session_jsonl_path_for_cwd(str(isolated_cwd), session_id, claude_home=claude_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not resume_sid:
            user_id = new_uuid()
            assistant_id = new_uuid()
            base = [
                {'type': 'user', 'uuid': user_id, 'parentUuid': None, 'sessionId': session_id,
                 'message': {'role': 'user', 'content': '测试消息 A'}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': session_id,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': '测试回复 A'}]}},
            ]
            dump_jsonl(path, base, allowed_output_root=work_root)
            return _MockProc(stdout_lines=_stream_stdout('测试回复 A', session_id))
        if not path.is_file():
            return _MockProc(stdout_lines=[], exit_code=1)
        canary = _extract_canary(load_jsonl(path))
        _append_live_round(path, session_id=session_id, canary=canary)
        return _MockProc(stdout_lines=_stream_stdout(canary, session_id))

    return factory


class IntegrationTests(unittest.TestCase):
    def _run_mocked_live(self, tmp: str) -> dict:
        work_root = Path(tmp)
        isolated = work_root / 'isolated-project'
        isolated.mkdir(parents=True)
        claude_home = work_root / 'claude-home'
        claude_home.mkdir(parents=True)
        factory = _mock_popen_factory(work_root, claude_home, isolated)
        with mock.patch.object(spike, 'explicit_auth_available', return_value=(True, 'ANTHROPIC_API_KEY')):
            with mock.patch.object(spike, 'claude_version_check', return_value=('2.1.220 (Claude Code)', True, '')):
                report = spike.run_spike(
                    work_root,
                    structural_only=False,
                    popen_factory=factory,
                    command='mocked-integration',
                )
        return report.to_dict()

    def test_required_cases_enter_live_probe_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp)
            by_id = {c['case_id']: c for c in data['cases']}
            for cid in REQUIRED_CASES:
                self.assertIn(cid, by_id)
                self.assertNotIn(by_id[cid]['state'], FORBIDDEN_STATES, cid)

    def test_case0_not_not_implemented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp)
            c0 = next(c for c in data['cases'] if c['case_id'] == '0')
            self.assertNotEqual(c0['state'], 'NOT_IMPLEMENTED_IN_SPIKE')

    def test_case5b_and_6_not_live_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp)
            for cid in ('5B', '6'):
                c = next(x for x in data['cases'] if x['case_id'] == cid)
                self.assertNotEqual(c['state'], 'LIVE_SKIPPED_NO_ASSISTANT')

    def test_stdout_canary_ok_but_disk_wrong_fails_gate(self) -> None:
        canary = generate_history_canary()
        sid = new_uuid()
        u, a = new_uuid(), new_uuid()
        before = [
            {'type': 'user', 'uuid': u, 'parentUuid': None, 'sessionId': sid,
             'message': {'role': 'user', 'content': 'x'}},
            {'type': 'assistant', 'uuid': a, 'parentUuid': u, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': f'[history-canary:{canary}]'}]}},
        ]
        after = before + [
            {'type': 'user', 'uuid': new_uuid(), 'parentUuid': a, 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': a, 'sessionId': sid,
             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'wrong-on-disk'}]}},
        ]
        from tools.claude_forge_live_gate import LiveProbeRaw, evaluate_live_gate

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
            before_bytes=json.dumps(before).encode(),
            after_bytes=json.dumps(after).encode(),
            before_events=before,
            after_events=after,
        )
        self.assertFalse(gate.passed)
        self.assertIn('append_assistant_canary_mismatch', gate.failures)

    def test_nested_stream_event_parses_without_duplicate(self) -> None:
        lines = [
            json.dumps({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': 'AB'}}}),
            json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'AB'}]}}),
        ]
        raw = parse_stdout_events(lines)
        self.assertTrue(raw.saw_text_delta)
        self.assertEqual(raw.assistant_text, 'AB')

    def test_subprocess_runner_timeout_kills_process_group(self) -> None:
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

    def test_symlink_work_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'real'
            link = Path(tmp) / 'link'
            real.mkdir()
            link.symlink_to(real)
            with self.assertRaises(ValueError):
                verify_work_root(link)

    def test_report_has_no_stale_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / 'results.json'
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'spike_claude_forge_resume.py'),
                 '--structural-only', '--work-root', tmp, '--report', str(report_path)],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            text = report_path.read_text(encoding='utf-8')
            self.assertNotIn(OLD_SHA, text)
            self.assertNotIn(OLD_PROMPT, text)
            data = json.loads(text)
            self.assertIn('tested_tree_sha', data)
            self.assertIn('tested_diff_sha256', data)
            self.assertIn('generated_at', data)
            self.assertEqual(data['live_probe_status'], 'NOT_RUN_NO_CREDENTIALS')


if __name__ == '__main__':
    unittest.main()
