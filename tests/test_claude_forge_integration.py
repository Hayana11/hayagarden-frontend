"""Integration tests for spike harness with mocked live runner (no API)."""
from __future__ import annotations

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
from tools.claude_forge_subprocess import SubprocessRunResult, run_subprocess_with_timeout  # noqa: E402

FIXTURE_ROOT = ROOT / 'tests' / 'fixtures' / 'claude_forge_spike'
REQUIRED_CASES = ('0', '1', '2A', '2B', '3A', '5B', '6', '7')
FORBIDDEN_STATES = {'NOT_IMPLEMENTED_IN_SPIKE', 'LIVE_SKIPPED_NO_ASSISTANT'}
OLD_SHA = '24656ca'
OLD_PROMPT = '请只回复两个字：收到'


def _auth_status_process(payload: Any, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(
        args=['npx', 'auth', 'status'],
        returncode=returncode,
        stdout=stdout,
        stderr='sensitive auth status diagnostics must not enter the report',
    )


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
        if evt.get('type') == 'assistant':
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    candidate = str(block.get('text') or '').strip()
                    if candidate.startswith('CANARY-'):
                        return candidate
    return generate_history_canary()


def _mock_subprocess_runner(
    work_root: Path,
    claude_home: Path,
    isolated_cwd: Path,
    *,
    create_stdout_canary: str | None = None,
    create_jsonl_canary: str | None = None,
    stdout_session_id: str | None = None,
):
    def runner(*, cmd, cwd, env, stdin_payload, timeout_seconds, popen_factory=None):
        resume_sid = None
        for i, part in enumerate(cmd):
            if part == '--resume' and i + 1 < len(cmd):
                resume_sid = cmd[i + 1]
        session_id = resume_sid or new_uuid()
        path = session_jsonl_path_for_cwd(str(isolated_cwd), session_id, claude_home=claude_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not resume_sid:
            payload = json.loads(stdin_payload.strip())
            prompt = str(((payload.get('message') or {}).get('content')) or '')
            requested_canary = prompt.rsplit('：', 1)[-1]
            disk_canary = create_jsonl_canary if create_jsonl_canary is not None else requested_canary
            stream_canary = create_stdout_canary if create_stdout_canary is not None else requested_canary
            stream_sid = stdout_session_id if stdout_session_id is not None else session_id
            user_id = new_uuid()
            assistant_id = new_uuid()
            base = [
                {'type': 'user', 'uuid': user_id, 'parentUuid': None, 'sessionId': session_id,
                 'message': {'role': 'user', 'content': prompt}},
                {'type': 'assistant', 'uuid': assistant_id, 'parentUuid': user_id, 'sessionId': session_id,
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': disk_canary}]}},
            ]
            dump_jsonl(path, base, allowed_output_root=work_root)
            return SubprocessRunResult(
                exit_code=0,
                stdout_lines=_stream_stdout(stream_canary, stream_sid),
                process_started=True,
            )
        if not path.is_file():
            return SubprocessRunResult(exit_code=1, process_started=True)
        canary = _extract_canary(load_jsonl(path))
        _append_live_round(path, session_id=session_id, canary=canary)
        return SubprocessRunResult(
            exit_code=0,
            stdout_lines=_stream_stdout(canary, session_id),
            process_started=True,
        )

    return runner


class IntegrationTests(unittest.TestCase):
    def _run_mocked_live(self, tmp: str, **runner_kwargs: Any) -> dict:
        work_root = Path(tmp)
        isolated = work_root / 'isolated-project'
        isolated.mkdir(parents=True)
        claude_home = work_root / 'claude-home'
        claude_home.mkdir(parents=True)
        runner = _mock_subprocess_runner(work_root, claude_home, isolated, **runner_kwargs)
        with mock.patch.object(spike, 'explicit_auth_available', return_value=(True, 'ANTHROPIC_API_KEY')):
            with mock.patch.object(spike, 'claude_version_check', return_value=('2.1.220 (Claude Code)', True, '')):
                with mock.patch.object(spike, '_subprocess_runner', runner):
                    report = spike.run_spike(
                        work_root,
                        structural_only=False,
                        command='mocked-integration',
                    )
        return report.to_dict()

    def test_required_cases_enter_live_probe_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp)
            by_id = {c['case_id']: c for c in data['cases']}
            for cid in REQUIRED_CASES:
                self.assertIn(cid, by_id)
                self.assertEqual(by_id[cid]['state'], 'API_ACCEPTED_FIRST_DELTA', cid)
            self.assertEqual(data['verdict'], 'GO')

    def test_subscription_auth_default_off_does_not_check_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(spike, 'explicit_auth_available', return_value=(False, 'missing')):
                with mock.patch.object(
                    spike,
                    'isolated_subscription_auth_status',
                    side_effect=AssertionError('isolated auth must remain opt-in'),
                ):
                    with mock.patch.object(
                        spike,
                        'claude_version_check',
                        return_value=('2.1.220 (Claude Code)', True, ''),
                    ):
                        report = spike.run_spike(Path(tmp), structural_only=False)
            self.assertFalse(report.auth_available)
            self.assertEqual(report.auth_source, 'missing')
            self.assertEqual(report.live_probe_status, 'NOT_RUN_NO_CREDENTIALS')

    def test_explicit_environment_auth_preserves_existing_behavior(self) -> None:
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake-test-key'}, clear=False):
            self.assertEqual(spike.explicit_auth_available(), (True, 'ANTHROPIC_API_KEY'))
        with mock.patch.dict(
            os.environ,
            {'ANTHROPIC_API_KEY': '', 'CLAUDE_CODE_OAUTH_TOKEN': 'fake-test-token'},
            clear=False,
        ):
            self.assertEqual(spike.explicit_auth_available(), (True, 'CLAUDE_CODE_OAUTH_TOKEN'))

    def test_isolated_subscription_auth_accepts_pro(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / 'claude-home'
            isolated_cwd = root / 'isolated-project'
            claude_home.mkdir()
            isolated_cwd.mkdir()
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process({'loggedIn': True, 'subscriptionType': 'pro'}),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(claude_home, isolated_cwd),
                    (True, spike.ISOLATED_SUBSCRIPTION_AUTH_SOURCE),
                )

    def test_isolated_subscription_auth_accepts_max(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / 'claude-home'
            isolated_cwd = root / 'isolated-project'
            claude_home.mkdir()
            isolated_cwd.mkdir()
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process({'loggedIn': True, 'subscriptionType': 'max'}),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(claude_home, isolated_cwd),
                    (True, spike.ISOLATED_SUBSCRIPTION_AUTH_SOURCE),
                )

    def test_isolated_subscription_auth_rejects_not_logged_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process({'loggedIn': False}),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(root / 'claude-home', root / 'isolated-project'),
                    (False, 'isolated_auth_not_logged_in'),
                )

    def test_isolated_subscription_auth_rejects_wrong_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process({'loggedIn': True, 'subscriptionType': 'team'}),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(root / 'claude-home', root / 'isolated-project'),
                    (False, 'isolated_auth_wrong_subscription'),
                )

    def test_isolated_subscription_auth_rejects_explicit_api_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process({'loggedIn': True, 'authMethod': 'api-key'}),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(root / 'claude-home', root / 'isolated-project'),
                    (False, 'isolated_auth_wrong_subscription'),
                )

    def test_isolated_subscription_auth_rejects_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process('', returncode=1),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(root / 'claude-home', root / 'isolated-project'),
                    (False, 'isolated_auth_status_command_failed'),
                )

    def test_isolated_subscription_auth_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                spike,
                '_auth_status_runner',
                return_value=_auth_status_process('not-json'),
            ):
                self.assertEqual(
                    spike.isolated_subscription_auth_status(root / 'claude-home', root / 'isolated-project'),
                    (False, 'isolated_auth_status_invalid'),
                )

    def test_isolated_auth_sensitive_status_fields_do_not_enter_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sensitive_email = 'private-user@example.invalid'
            sensitive_org = 'private-organization-name'
            status = {
                'loggedIn': False,
                'email': sensitive_email,
                'organizationName': sensitive_org,
                'credentialPath': 'private-credential-path',
            }
            with mock.patch.object(spike, 'explicit_auth_available', return_value=(False, 'missing')):
                with mock.patch.object(
                    spike,
                    '_auth_status_runner',
                    return_value=_auth_status_process(status),
                ):
                    with mock.patch.object(
                        spike,
                        'claude_version_check',
                        return_value=('2.1.220 (Claude Code)', True, ''),
                    ):
                        report = spike.run_spike(
                            Path(tmp),
                            structural_only=False,
                            allow_isolated_subscription_auth=True,
                        )
            serialized = json.dumps(report.to_dict())
            self.assertNotIn(sensitive_email, serialized)
            self.assertNotIn(sensitive_org, serialized)
            self.assertNotIn('private-credential-path', serialized)
            self.assertEqual(report.auth_source, 'isolated_auth_not_logged_in')

    def test_isolated_auth_status_uses_fixed_command_and_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / 'claude-home'
            isolated_cwd = root / 'isolated-project'
            runner = mock.Mock(
                return_value=_auth_status_process({'loggedIn': True, 'subscriptionType': 'pro'}),
            )
            with mock.patch.object(spike, '_auth_status_runner', runner):
                spike.isolated_subscription_auth_status(claude_home, isolated_cwd)
            args, kwargs = runner.call_args
            self.assertEqual(
                args[0],
                ['npx', '--yes', '@anthropic-ai/claude-code@2.1.220', 'auth', 'status'],
            )
            self.assertEqual(kwargs['cwd'], str(isolated_cwd))
            self.assertEqual(kwargs['env']['CLAUDE_CONFIG_DIR'], str(claude_home))
            self.assertEqual(kwargs['env']['DISABLE_AUTOUPDATER'], '1')

    def test_isolated_auth_status_removes_auth_and_provider_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inherited = {name: f'test-{name}' for name in spike.AUTH_PROVIDER_OVERRIDE_VARS}
            runner = mock.Mock(
                return_value=_auth_status_process({'loggedIn': True, 'subscriptionType': 'max'}),
            )
            with mock.patch.dict(os.environ, inherited, clear=False):
                with mock.patch.object(spike, '_auth_status_runner', runner):
                    spike.isolated_subscription_auth_status(
                        root / 'claude-home',
                        root / 'isolated-project',
                    )
                child_env = runner.call_args.kwargs['env']
                for name, value in inherited.items():
                    self.assertNotIn(name, child_env)
                    self.assertEqual(os.environ.get(name), value)

    def test_structural_only_does_not_execute_isolated_auth_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                spike,
                'isolated_subscription_auth_status',
                side_effect=AssertionError('structural-only must not inspect auth status'),
            ):
                report = spike.run_spike(
                    Path(tmp),
                    structural_only=True,
                    allow_isolated_subscription_auth=True,
                )
            self.assertFalse(report.auth_available)
            self.assertEqual(report.auth_source, 'ignored_structural_only')

    def test_isolated_auth_keeps_mocked_required_cases_and_go_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            isolated = work_root / 'isolated-project'
            isolated.mkdir()
            claude_home = work_root / 'claude-home'
            claude_home.mkdir()
            runner = _mock_subprocess_runner(work_root, claude_home, isolated)
            with mock.patch.object(spike, 'explicit_auth_available', return_value=(False, 'missing')):
                with mock.patch.object(
                    spike,
                    'isolated_subscription_auth_status',
                    return_value=(True, spike.ISOLATED_SUBSCRIPTION_AUTH_SOURCE),
                ):
                    with mock.patch.object(
                        spike,
                        'claude_version_check',
                        return_value=('2.1.220 (Claude Code)', True, ''),
                    ):
                        with mock.patch.object(spike, '_subprocess_runner', runner):
                            report = spike.run_spike(
                                work_root,
                                structural_only=False,
                                allow_isolated_subscription_auth=True,
                            )
            by_id = {case.case_id: case for case in report.cases}
            for case_id in REQUIRED_CASES:
                self.assertEqual(by_id[case_id].state, 'API_ACCEPTED_FIRST_DELTA', case_id)
            self.assertEqual(report.verdict, 'GO')
            self.assertEqual(report.auth_source, spike.ISOLATED_SUBSCRIPTION_AUTH_SOURCE)

    def test_explicit_auth_has_priority_over_isolated_subscription_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            isolated = work_root / 'isolated-project'
            isolated.mkdir()
            claude_home = work_root / 'claude-home'
            claude_home.mkdir()
            runner = _mock_subprocess_runner(work_root, claude_home, isolated)
            with mock.patch.object(
                spike,
                'explicit_auth_available',
                return_value=(True, 'ANTHROPIC_API_KEY'),
            ):
                with mock.patch.object(
                    spike,
                    'isolated_subscription_auth_status',
                    side_effect=AssertionError('explicit auth must win without mixing'),
                ):
                    with mock.patch.object(
                        spike,
                        'claude_version_check',
                        return_value=('2.1.220 (Claude Code)', True, ''),
                    ):
                        with mock.patch.object(spike, '_subprocess_runner', runner):
                            report = spike.run_spike(
                                work_root,
                                structural_only=False,
                                allow_isolated_subscription_auth=True,
                            )
            self.assertEqual(report.auth_source, 'ANTHROPIC_API_KEY')
            self.assertEqual(report.verdict, 'GO')

    def test_isolated_claude_home_symlink_is_rejected(self) -> None:
        original_is_symlink = Path.is_symlink

        def selective_is_symlink(path: Path) -> bool:
            return path.name == 'claude-home' or original_is_symlink(path)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(Path, 'is_symlink', selective_is_symlink):
                with self.assertRaisesRegex(ValueError, 'FORGE_SYMLINK:claude_home'):
                    spike._prepare_isolated_workspace(Path(tmp))

    def test_work_root_inside_repository_is_rejected(self) -> None:
        candidate = ROOT / 'never-create-isolated-auth-work-root'
        with self.assertRaisesRegex(ValueError, 'FORGE_WORK_ROOT_INSIDE_REPOSITORY'):
            spike._prepare_isolated_workspace(candidate)
        self.assertFalse(candidate.exists())

    def test_native_case0_preserves_create_jsonl_before_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp)
            c0 = next(c for c in data['cases'] if c['case_id'] == '0')
            self.assertEqual(c0['state'], 'API_ACCEPTED_FIRST_DELTA')
            self.assertFalse(c0['native_jsonl_rewritten'])
            self.assertEqual(c0['native_create_sha256'], c0['native_before_sha256'])
            self.assertEqual(c0['source_sha256'], c0['native_create_sha256'])

    def test_native_case0_does_not_call_jsonl_rewrite_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_root = Path(tmp)
            isolated = work_root / 'isolated-project'
            claude_home = work_root / 'claude-home'
            isolated.mkdir()
            claude_home.mkdir()
            runner = _mock_subprocess_runner(work_root, claude_home, isolated)
            with mock.patch.object(spike, '_subprocess_runner', runner):
                with mock.patch.object(
                    spike,
                    'prepare_history_for_live_gate',
                    side_effect=AssertionError('native CASE 0 must not prepare/rewrite history'),
                ):
                    with mock.patch.object(
                        spike,
                        'dump_jsonl',
                        side_effect=AssertionError('native CASE 0 must not dump/rewrite JSONL'),
                    ):
                        result = spike._run_case0_native_control(
                            isolated_cwd=isolated,
                            claude_home=claude_home,
                            work_root=work_root,
                        )
            self.assertEqual(result.state, 'API_ACCEPTED_FIRST_DELTA', result.live_gate_failures)
            self.assertFalse(result.native_jsonl_rewritten)

    def test_native_create_stdout_canary_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp, create_stdout_canary='WRONG-STDOUT-CANARY')
            c0 = next(c for c in data['cases'] if c['case_id'] == '0')
            self.assertEqual(c0['state'], 'NATIVE_CREATE_FAIL')
            self.assertIn('native_create_stdout_canary_mismatch', c0['live_gate_failures'])

    def test_native_create_jsonl_canary_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp, create_jsonl_canary='WRONG-JSONL-CANARY')
            c0 = next(c for c in data['cases'] if c['case_id'] == '0')
            self.assertEqual(c0['state'], 'NATIVE_CREATE_FAIL')
            self.assertIn('native_create_jsonl_canary_mismatch', c0['live_gate_failures'])

    def test_native_create_stdout_session_must_match_jsonl_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = self._run_mocked_live(tmp, stdout_session_id=new_uuid())
            c0 = next(c for c in data['cases'] if c['case_id'] == '0')
            self.assertEqual(c0['state'], 'NATIVE_CREATE_FAIL')
            self.assertIn('native_create_filename_session_mismatch', c0['live_gate_failures'])

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
        new_user_id = new_uuid()
        after = before + [
            {'type': 'user', 'uuid': new_user_id, 'parentUuid': a, 'sessionId': sid,
             'message': {'role': 'user', 'content': build_live_user_prompt()}},
            {'type': 'assistant', 'uuid': new_uuid(), 'parentUuid': new_user_id, 'sessionId': sid,
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

    def test_subprocess_closed_pipes_then_sleep_obeys_absolute_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'closed_pipes_sleep.py'
            script.write_text(
                'import os, time\n'
                'os.close(1)\n'
                'os.close(2)\n'
                'time.sleep(30)\n',
                encoding='utf-8',
            )
            start = time.monotonic()
            result = run_subprocess_with_timeout(
                cmd=[sys.executable, str(script)],
                cwd=tmp,
                env=os.environ.copy(),
                stdin_payload='',
                timeout_seconds=0.3,
            )
            elapsed = time.monotonic() - start
            self.assertTrue(result.timed_out)
            self.assertLess(elapsed, 1.5)

    def test_symlink_work_root_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / 'real'
            link = Path(tmp) / 'link'
            real.mkdir()
            try:
                link.symlink_to(real)
            except OSError as exc:
                self.skipTest(f'symlink unavailable: {exc}')
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
            self.assertIn('tested_commit_sha', data)
            self.assertEqual(data['artifact_commit_sha'], 'artifact_commit_pending')
            self.assertEqual(data['live_probe_status'], 'NOT_RUN_NO_CREDENTIALS')

    def test_results_record_unit_and_harness_commands_and_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / 'results.json'
            unit_command = 'python3 -m unittest relevant-tests -v'
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / 'scripts' / 'spike_claude_forge_resume.py'),
                    '--structural-only',
                    '--work-root', tmp,
                    '--report', str(report_path),
                    '--unit-test-command', unit_command,
                    '--unit-test-exit-code', '0',
                    '--unit-test-count', '47',
                ],
                capture_output=True,
                text=True,
                cwd=str(ROOT),
                check=False,
            )
            self.assertEqual(proc.returncode, 2)
            data = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertEqual(data['unit_test_command'], unit_command)
            self.assertEqual(data['unit_test_exit_code'], 0)
            self.assertEqual(data['unit_test_count'], 47)
            self.assertIn('--structural-only', data['harness_command'])
            self.assertEqual(data['harness_exit_code'], 2)


if __name__ == '__main__':
    unittest.main()
