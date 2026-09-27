"""R2/R4 — Continuity Native-Fork execution adapter contracts.

All process boundaries (``fork_session``, the resumed CLI subprocess, the
OAuth token getter) are injected/mocked. These tests never spawn a real
process and never call a real model; they only assert the safety invariants
R4 requires: parent transcript hash is checked before, right after fork, and
again after generation; tool execution is always disabled on the child;
persona/materialized evidence are never sent; any failure raises
``NativeForkGenerationError`` and leaves the parent untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from chat.provider_router import GenerationAuthoritySnapshot
from continuity.native_fork_eligibility import ContinuityForkPlan
from continuity.native_fork_executor import (
    NATIVE_FORK_EXECUTOR,
    NativeForkGenerationError,
    execute_continuity_native_fork,
)
from tools.claude_forge_subprocess import SubprocessRunResult


def _cc_stream(*, model='claude-opus-5', text='summary text', usage=None):
    lines = [
        json.dumps({'type': 'system', 'subtype': 'init', 'model': model}),
        json.dumps({
            'type': 'result', 'subtype': 'success', 'is_error': False,
            'result': text, 'usage': usage or {'input_tokens': 5, 'cache_read_input_tokens': 9000},
        }),
    ]
    return [line + '\n' for line in lines]


class ContinuityNativeForkExecutorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = self.tmp.name
        self.parent_path = Path(self.tmp.name) / 'parent.jsonl'
        self.parent_path.write_bytes(b'{"type":"user"}\n')
        self.child_path = Path(self.tmp.name) / 'child.jsonl'
        self.child_path.write_bytes(b'{"type":"user"}\n')

        self.plan = ContinuityForkPlan(
            eligible=True,
            reason='',
            generation_job_id='job:1',
            candidate_id='cand:1',
            parent_session_id='parent-sid',
            fork_event_uuid='ev1',
            boundary_message_id=2,
            parent_transcript_path=str(self.parent_path),
            scope_completed_turns=3,
            context_id=1,
            context_epoch=1,
            resident_generation=1,
        )
        self.authority = GenerationAuthoritySnapshot('claude_code', 'default')

    def _fork_fn(self, *, child_sid='child-sid', mutate_parent=False, raise_error=None):
        def _fn(session_id, *, directory, up_to_message_id, title):
            if raise_error is not None:
                raise raise_error
            if mutate_parent:
                self.parent_path.write_bytes(b'{"type":"user"}\n{"type":"assistant"}\n')
            return mock.Mock(session_id=child_sid)
        return _fn

    def _runtime(self):
        stack = ExitStack()
        stack.enter_context(mock.patch(
            'chat.cc_runtime.require_managed_claude_runtime', return_value='2.1.280',
        ))
        stack.enter_context(mock.patch(
            'chat.cc_runtime.claude_cmd_for_version',
            side_effect=lambda _version, *args, **kwargs: ['/managed/2.1.280', *args],
        ))
        return stack

    def _popen(self, *, stdout=None, exit_code=0, process_started=True, timed_out=False):
        def _factory(cwd, env, stdin_payload, timeout_seconds, popen_factory):
            return SubprocessRunResult(
                exit_code=exit_code,
                stdout_lines=stdout or _cc_stream(),
                process_started=process_started,
                timed_out=timed_out,
            )
        return _factory

    def _run_subprocess_patch(self, **kwargs):
        recorded = {}

        def _fake_run_subprocess(*, cmd, cwd, env, stdin_payload, timeout_seconds, popen_factory=None):
            recorded['cmd'] = cmd
            recorded['stdin_payload'] = stdin_payload
            return SubprocessRunResult(
                exit_code=kwargs.get('exit_code', 0),
                stdout_lines=kwargs.get('stdout') or _cc_stream(),
                process_started=kwargs.get('process_started', True),
                timed_out=kwargs.get('timed_out', False),
            )
        patch = mock.patch(
            'continuity.native_fork_executor.run_subprocess_with_timeout',
            side_effect=_fake_run_subprocess,
        )
        return patch, recorded

    def test_ineligible_plan_raises_before_any_side_effect(self):
        plan = ContinuityForkPlan(eligible=False, reason='flag_off')
        with self.assertRaises(NativeForkGenerationError):
            execute_continuity_native_fork(
                plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
            )

    def test_happy_path_returns_native_fork_result_and_never_mutates_parent(self):
        before = hashlib.sha256(self.parent_path.read_bytes()).hexdigest()
        patch, recorded = self._run_subprocess_patch()
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ):
            result = execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
            )
        after = hashlib.sha256(self.parent_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(result.provider, 'claude_code')
        self.assertEqual(result.actual_executor, NATIVE_FORK_EXECUTOR)
        self.assertEqual(result.text, 'summary text')
        self.assertEqual(result.usage['cache_read_input_tokens'], 9000)
        self.assertIn('continuity_native_fork_parent_session_hash', result.usage)
        self.assertEqual(result.usage['continuity_native_fork_scope_completed_turns'], 3)

    def test_resume_call_disables_tools_and_omits_system_prompt_and_persona(self):
        patch, recorded = self._run_subprocess_patch()
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ):
            execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE-ONLY', authority=self.authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
            )
        cmd = recorded['cmd']
        self.assertIn('--resume', cmd)
        self.assertEqual(cmd[cmd.index('--resume') + 1], 'child-sid')
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertEqual(cmd[cmd.index('--allowedTools') + 1], '')
        self.assertNotIn('--system-prompt', cmd)
        self.assertNotIn('--no-session-persistence', cmd)
        payload = json.loads(recorded['stdin_payload'].strip())
        self.assertEqual(payload['type'], 'user')
        self.assertIn('SUMMARIZE-ONLY', payload['message']['content'])
        self.assertIn('3', payload['message']['content'])

    def test_parent_mutated_during_fork_raises_and_is_never_swallowed_as_success(self):
        with self.assertRaisesRegex(NativeForkGenerationError, 'parent_mutated'):
            execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(mutate_parent=True), token_getter=lambda: 'tok',
            )

    def test_fork_failure_raises_fork_failed(self):
        with self.assertRaisesRegex(NativeForkGenerationError, 'fork_failed'):
            execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(raise_error=RuntimeError('nope')),
                token_getter=lambda: 'tok',
            )

    def test_missing_child_transcript_raises(self):
        with mock.patch(
            'continuity.native_fork_executor.session_jsonl_path', return_value=None,
        ):
            with self.assertRaisesRegex(NativeForkGenerationError, 'child_transcript_missing'):
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                )

    def test_model_attestation_mismatch_raises(self):
        patch, _ = self._run_subprocess_patch(stdout=_cc_stream(model='claude-sonnet-5'))
        authority = GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5')
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ):
            with self.assertRaisesRegex(NativeForkGenerationError, 'model_mismatch'):
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                )

    def test_error_result_fails_closed(self):
        error_stream = [
            json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5'}) + '\n',
            json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True, 'result': ''}) + '\n',
        ]
        patch, _ = self._run_subprocess_patch(stdout=error_stream)
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ):
            with self.assertRaisesRegex(NativeForkGenerationError, 'result_not_success'):
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                )

    def test_exit_nonzero_fails_closed(self):
        patch, _ = self._run_subprocess_patch(exit_code=1)
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ):
            with self.assertRaisesRegex(NativeForkGenerationError, 'exit_1'):
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                )

    def test_missing_token_raises_before_spawn(self):
        with self._runtime(), mock.patch(
            'continuity.native_fork_executor.session_jsonl_path',
            return_value=self.child_path,
        ), mock.patch('continuity.native_fork_executor.run_subprocess_with_timeout') as run:
            with self.assertRaisesRegex(NativeForkGenerationError, 'token_unavailable'):
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: '',
                )
            run.assert_not_called()

    def test_api_relay_authority_is_unsupported(self):
        authority = GenerationAuthoritySnapshot('api_relay', 'some-model')
        with self.assertRaisesRegex(NativeForkGenerationError, 'unsupported_provider'):
            execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE', authority=authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
            )


class ProviderStageClassificationTest(ContinuityNativeForkExecutorTest):
    """Pre-provider failures may fall back; post-provider failures never may."""

    def _execute(self, *, fork_fn=None, token='tok', runtime_error=None, child_path='default', **run_kwargs):
        starts = []
        patch, _ = self._run_subprocess_patch(**run_kwargs)
        with ExitStack() as stack:
            if runtime_error is None:
                stack.enter_context(self._runtime())
            else:
                stack.enter_context(mock.patch(
                    'chat.cc_runtime.require_managed_claude_runtime', side_effect=runtime_error,
                ))
            stack.enter_context(patch)
            stack.enter_context(mock.patch(
                'continuity.native_fork_executor.session_jsonl_path',
                return_value=self.child_path if child_path == 'default' else child_path,
            ))
            try:
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=fork_fn or self._fork_fn(), token_getter=lambda: token,
                    on_provider_start=lambda: starts.append(1),
                )
            except NativeForkGenerationError as exc:
                return exc, len(starts)
        self.fail('expected NativeForkGenerationError')

    def assertPre(self, exc, starts):
        self.assertFalse(exc.provider_started, exc.error_code)
        self.assertTrue(exc.error_code.startswith('native_fork_pre_provider:'))
        self.assertEqual(starts, 0)

    def assertPost(self, exc, starts):
        self.assertTrue(exc.provider_started, exc.error_code)
        self.assertTrue(exc.error_code.startswith('native_fork_post_provider:'))
        self.assertEqual(starts, 1)

    def test_fork_failure_is_pre_provider(self):
        self.assertPre(*self._execute(fork_fn=self._fork_fn(raise_error=RuntimeError('x'))))

    def test_token_missing_is_pre_provider(self):
        self.assertPre(*self._execute(token=''))

    def test_runtime_unavailable_is_pre_provider(self):
        self.assertPre(*self._execute(runtime_error=RuntimeError('no cli')))

    def test_child_transcript_missing_is_pre_provider(self):
        self.assertPre(*self._execute(child_path=None))

    def test_timeout_is_post_provider(self):
        self.assertPost(*self._execute(timed_out=True))

    def test_nonzero_exit_is_post_provider(self):
        self.assertPost(*self._execute(exit_code=2))

    def test_result_missing_is_post_provider(self):
        stream = [json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm'}) + '\n']
        self.assertPost(*self._execute(stdout=stream))

    def test_result_not_success_is_post_provider(self):
        stream = [json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True}) + '\n']
        self.assertPost(*self._execute(stdout=stream))

    def test_model_unattested_is_post_provider(self):
        self.authority = GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5')
        stream = [json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'x'}) + '\n']
        self.assertPost(*self._execute(stdout=stream))

    def test_model_mismatch_is_post_provider(self):
        self.authority = GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5')
        self.assertPost(*self._execute(stdout=_cc_stream(model='claude-sonnet-5')))

    def test_empty_output_is_post_provider(self):
        self.assertPost(*self._execute(stdout=_cc_stream(text='   ')))

    def test_parent_mutated_during_generation_is_post_provider(self):
        def mutating_run(**kwargs):
            self.parent_path.write_bytes(b'{"type":"user"}\n{"type":"assistant"}\n')
            return SubprocessRunResult(exit_code=0, stdout_lines=_cc_stream(), process_started=True)

        starts = []
        with self._runtime(), mock.patch(
            'continuity.native_fork_executor.run_subprocess_with_timeout', side_effect=mutating_run,
        ), mock.patch(
            'continuity.native_fork_executor.session_jsonl_path', return_value=self.child_path,
        ):
            with self.assertRaises(NativeForkGenerationError) as ctx:
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                    on_provider_start=lambda: starts.append(1),
                )
        self.assertPost(ctx.exception, len(starts))
        self.assertIn('parent_mutated', ctx.exception.error_code)

    def test_spawn_exception_is_post_provider(self):
        starts = []
        with self._runtime(), mock.patch(
            'continuity.native_fork_executor.run_subprocess_with_timeout',
            side_effect=OSError('pipe'),
        ), mock.patch(
            'continuity.native_fork_executor.session_jsonl_path', return_value=self.child_path,
        ):
            with self.assertRaises(NativeForkGenerationError) as ctx:
                execute_continuity_native_fork(
                    self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                    fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                    on_provider_start=lambda: starts.append(1),
                )
        self.assertPost(ctx.exception, len(starts))

    def test_success_reports_exactly_one_provider_start(self):
        starts = []
        patch, _ = self._run_subprocess_patch()
        with self._runtime(), patch, mock.patch(
            'continuity.native_fork_executor.session_jsonl_path', return_value=self.child_path,
        ):
            execute_continuity_native_fork(
                self.plan, prompt_body='SUMMARIZE', authority=self.authority, cwd=self.cwd,
                fork_session_fn=self._fork_fn(), token_getter=lambda: 'tok',
                on_provider_start=lambda: starts.append(1),
            )
        self.assertEqual(len(starts), 1)


if __name__ == '__main__':
    unittest.main()
