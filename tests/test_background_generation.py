"""A2 background adapter tests: all provider execution is mocked."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-background-generation.db'),
)

from chat.background_generation import (  # noqa: E402
    BackgroundGenerationError,
    BackgroundGenerationRequest,
    generate_background,
)
from chat.provider_router import GenerationAuthoritySnapshot, capture_generation_authority  # noqa: E402
import config_store  # noqa: E402


REQUEST = BackgroundGenerationRequest(
    system_text='SYSTEM-X',
    prompt_text='PROMPT-Y',
    max_tokens_hint=321,
    timeout_sec=12,
    task_kind='test',
)


class FakeRelayManager:
    def __init__(self, response=None, error=None):
        self.response = response or {'content': [{'type': 'text', 'text': 'relay output'}]}
        self.error = error
        self.calls = []

    def call(self, payload, **kwargs):
        self.calls.append((payload, kwargs))
        if self.error:
            raise self.error
        return self.response

    def extract_text(self, response):
        return ''.join(
            block.get('text', '') for block in response.get('content', [])
            if block.get('type') == 'text'
        )


class BackgroundGenerationTests(unittest.TestCase):
    def _cc_run(self, stdout=None):
        stdout = stdout or '\n'.join((
            '{"type":"system","subtype":"init","model":"claude-opus-5"}',
            '{"type":"result","subtype":"success","is_error":false,"result":"cc output","usage":{"input_tokens":1}}',
        ))
        proc = mock.Mock(returncode=0, stdout=stdout, stderr='')
        return mock.patch('chat.background_generation.subprocess.run', return_value=proc)

    def _cc_runtime(self):
        return mock.patch('chat.cc_runtime.require_pinned_claude_version', return_value='2.1.220')

    def test_cc_explicit_model_is_frozen_and_payload_is_transparent(self):
        authority = GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5')
        with self._cc_runtime(), self._cc_run() as run:
            result = generate_background(
                REQUEST, authority, cc_token_getter=lambda: 'fake-token',
            )
        argv = run.call_args.args[0]
        self.assertIn('--model', argv)
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-opus-5')
        self.assertEqual(argv[argv.index('--system-prompt') + 1], 'SYSTEM-X')
        self.assertEqual(argv[argv.index('-p') + 1], 'PROMPT-Y')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertEqual(argv[argv.index('--max-turns') + 1], '1')
        self.assertIn('--safe-mode', argv)
        self.assertIn('--no-session-persistence', argv)
        self.assertNotIn('--resume', argv)
        self.assertNotIn('--continue', argv)
        self.assertEqual(result.provider, 'claude_code')
        self.assertEqual(result.model_identity, authority.model_identity)
        self.assertEqual(result.actual_executor, 'claude_code_background_oneshot')
        self.assertEqual(result.text, 'cc output')

    def test_cc_default_model_omits_model_argv(self):
        with self._cc_runtime(), self._cc_run(
            '{"type":"result","subtype":"success","is_error":false,"result":"cc output"}',
        ) as run:
            result = generate_background(
                REQUEST,
                GenerationAuthoritySnapshot('claude_code', 'default'),
                cc_token_getter=lambda: 'fake-token',
            )
        self.assertNotIn('--model', run.call_args.args[0])
        self.assertIsNone(result.usage)

    def test_cc_invalid_identity_fails_before_spawn(self):
        with mock.patch('chat.background_generation.subprocess.run') as run:
            with self.assertRaisesRegex(BackgroundGenerationError, 'invalid_model_identity'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'unknown'),
                    cc_token_getter=lambda: 'fake-token',
                )
        run.assert_not_called()

    def test_cc_timeout_is_reported_without_relay_fallback(self):
        relay_factory = mock.Mock()
        with self._cc_runtime(), \
                mock.patch(
                    'chat.background_generation.subprocess.run',
                    side_effect=__import__('subprocess').TimeoutExpired('claude', 12),
                ):
            with self.assertRaisesRegex(BackgroundGenerationError, 'cc_background_timeout'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'default'),
                    cc_token_getter=lambda: 'fake-token',
                    relay_factory=relay_factory,
                )
        relay_factory.assert_not_called()

    def test_cc_error_result_fails_closed_despite_zero_exit(self):
        stdout = '\n'.join((
            '{"type":"system","subtype":"init","model":"claude-opus-5"}',
            '{"type":"result","subtype":"error_max_turns","is_error":true,"result":"partial"}',
        ))
        with self._cc_runtime(), self._cc_run(stdout):
            with self.assertRaisesRegex(BackgroundGenerationError, 'result_not_success'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5'),
                    cc_token_getter=lambda: 'fake-token',
                )

    def test_cc_missing_result_fails_closed_despite_text_deltas(self):
        stdout = (
            '{"type":"stream_event","event":{"delta":{"type":"text_delta","text":"partial"}}}'
        )
        with self._cc_runtime(), self._cc_run(stdout):
            with self.assertRaisesRegex(BackgroundGenerationError, 'result_missing'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'default'),
                    cc_token_getter=lambda: 'fake-token',
                )

    def test_cc_result_missing_is_error_fails_closed(self):
        stdout = '{"type":"result","subtype":"success","result":"partial"}'
        with self._cc_runtime(), self._cc_run(stdout):
            with self.assertRaisesRegex(BackgroundGenerationError, 'result_not_success'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'default'),
                    cc_token_getter=lambda: 'fake-token',
                )

    def test_cc_explicit_model_requires_matching_init_attestation(self):
        authority = GenerationAuthoritySnapshot('claude_code', 'explicit:claude-opus-5')
        mismatch = '\n'.join((
            '{"type":"system","subtype":"init","model":"claude-sonnet-5"}',
            '{"type":"result","subtype":"success","is_error":false,"result":"text"}',
        ))
        missing = '{"type":"result","subtype":"success","is_error":false,"result":"text"}'
        for stdout, error in ((mismatch, 'model_mismatch'), (missing, 'model_unattested')):
            with self.subTest(error=error), self._cc_runtime(), self._cc_run(stdout):
                with self.assertRaisesRegex(BackgroundGenerationError, error):
                    generate_background(REQUEST, authority, cc_token_getter=lambda: 'fake-token')

    def test_cc_failure_never_calls_relay(self):
        proc = mock.Mock(returncode=1, stdout='', stderr='failed')
        relay_factory = mock.Mock()
        with self._cc_runtime(), \
                mock.patch('chat.background_generation.subprocess.run', return_value=proc):
            with self.assertRaisesRegex(BackgroundGenerationError, 'cc_background_exit'):
                generate_background(
                    REQUEST,
                    GenerationAuthoritySnapshot('claude_code', 'default'),
                    cc_token_getter=lambda: 'fake-token',
                    relay_factory=relay_factory,
                )
        relay_factory.assert_not_called()

    def test_cc_executor_has_no_resident_or_transcript_dependency(self):
        source = Path(ROOT, 'chat', 'background_generation.py').read_text()
        self.assertNotIn('_CC_RESIDENT', source)
        self.assertNotIn('_CC_WAKE_RESIDENT', source)
        self.assertNotIn('transcript', source.lower())
        self.assertIn("'--safe-mode'", source)
        self.assertIn("'--no-session-persistence'", source)
        self.assertIn("'--tools', ''", source)
        for isolated_from in ('CLAUDE.md', 'skills', 'plugins', 'hooks', 'mcp-config', 'agents'):
            self.assertNotIn(isolated_from, source)

    def test_relay_uses_frozen_model_and_transparent_payload(self):
        manager = FakeRelayManager({'model': 'model-A', 'content': [{'type': 'text', 'text': 'ok'}]})
        generate_background(
            REQUEST,
            GenerationAuthoritySnapshot('api_relay', 'model-A'),
            relay_factory=lambda: manager,
        )
        payload, kwargs = manager.calls[0]
        self.assertEqual(payload, {
            'model': 'model-A',
            'max_tokens': 321,
            'system': 'SYSTEM-X',
            'messages': [{'role': 'user', 'content': 'PROMPT-Y'}],
        })
        self.assertEqual(kwargs, {'timeout': 12.0, 'use_ws_model': False})

    def test_relay_unknown_model_fails_before_manager_creation(self):
        factory = mock.Mock()
        with self.assertRaisesRegex(BackgroundGenerationError, 'unknown_model'):
            generate_background(
                REQUEST,
                GenerationAuthoritySnapshot('api_relay', 'unknown'),
                relay_factory=factory,
            )
        factory.assert_not_called()

    def test_relay_failure_never_calls_cc(self):
        token_getter = mock.Mock(return_value='fake-token')
        manager = FakeRelayManager(error=RuntimeError('relay down'))
        with self.assertRaisesRegex(BackgroundGenerationError, 'relay_background_failed'):
            generate_background(
                REQUEST,
                GenerationAuthoritySnapshot('api_relay', 'model-A'),
                relay_factory=lambda: manager,
                cc_token_getter=token_getter,
            )
        token_getter.assert_not_called()

    def test_relay_response_model_mismatch_fails_closed(self):
        manager = FakeRelayManager({'model': 'model-B', 'content': []})
        with self.assertRaisesRegex(BackgroundGenerationError, 'model_mismatch'):
            generate_background(
                REQUEST,
                GenerationAuthoritySnapshot('api_relay', 'model-A'),
                relay_factory=lambda: manager,
            )

    def test_relay_provenance_uses_snapshot_not_response(self):
        manager = FakeRelayManager({'content': [{'type': 'text', 'text': 'ok'}], 'usage': {'output_tokens': 2}})
        authority = GenerationAuthoritySnapshot('api_relay', 'model-A')
        result = generate_background(REQUEST, authority, relay_factory=lambda: manager)
        self.assertEqual(result.provider, 'api_relay')
        self.assertEqual(result.model_identity, 'model-A')
        self.assertEqual(result.actual_executor, 'api_relay_background')
        self.assertEqual(result.usage, {'output_tokens': 2})

    def test_old_cc_snapshot_survives_config_change(self):
        values = {'CHAT_PROVIDER': 'claude_code', 'CC_CHAT_MODEL': 'claude-opus-5'}
        with mock.patch.object(config_store, 'get', side_effect=lambda key, default=None: values.get(key, default)):
            authority = capture_generation_authority()
            values.update({'CHAT_PROVIDER': 'api_relay', 'CC_CHAT_MODEL': 'claude-sonnet-5'})
            with self._cc_runtime(), self._cc_run() as run:
                generate_background(REQUEST, authority, cc_token_getter=lambda: 'fake-token')
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('--model') + 1], 'claude-opus-5')

    def test_old_relay_snapshot_survives_config_change(self):
        manager = FakeRelayManager({'model': 'model-A', 'content': []})
        values = {'CHAT_PROVIDER': 'api_relay'}
        with mock.patch.object(config_store, 'get', side_effect=lambda key, default=None: values.get(key, default)), \
                mock.patch('relay.manager.resolve_active_relay_model_identity', return_value='model-A'):
            authority = capture_generation_authority()
            values.update({'CHAT_PROVIDER': 'claude_code', 'MODEL': 'model-B'})
            generate_background(REQUEST, authority, relay_factory=lambda: manager)
        self.assertEqual(manager.calls[0][0]['model'], 'model-A')

    def test_unknown_provider_fails_closed(self):
        with self.assertRaisesRegex(BackgroundGenerationError, 'unknown_provider'):
            generate_background(REQUEST, GenerationAuthoritySnapshot('invalid', 'model-A'))

    def test_adapter_has_no_persistence_or_surface_dependencies(self):
        source = Path(ROOT, 'chat', 'background_generation.py').read_text()
        for forbidden in (
            'chat_messages', 'memory_tool', 'dream_pool', 'auto_diary',
            'dream_generator', 'wake.runners', 'behavior_authority_b3',
        ):
            self.assertNotIn(forbidden, source)


if __name__ == '__main__':
    unittest.main()
