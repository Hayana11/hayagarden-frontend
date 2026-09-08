"""Behavior and source contracts for the A4 Auto Diary cutover."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import auto_diary


class FakeBackgroundGenerationError(RuntimeError):
    pass


class AutoDiaryContractTests(unittest.TestCase):
    def _runtime(self, authority, result_text='DIARY TEXT', *, error=None):
        capture = mock.Mock(return_value=authority)
        adapter = mock.Mock()
        if error is not None:
            adapter.side_effect = error
        else:
            adapter.return_value = types.SimpleNamespace(text=result_text)

        class Request:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        background = types.ModuleType('chat.background_generation')
        background.BackgroundGenerationRequest = Request
        background.generate_background = adapter
        background.BackgroundGenerationError = FakeBackgroundGenerationError
        provider_router = types.ModuleType('chat.provider_router')
        provider_router.capture_generation_authority = capture
        cc_auth = types.ModuleType('chat.cc_auth')
        cc_auth.read_cc_oauth_token = mock.Mock(return_value='test-token')
        modules = {
            'chat.background_generation': background,
            'chat.provider_router': provider_router,
            'chat.cc_auth': cc_auth,
        }
        return capture, adapter, modules

    def _generate(self, authority, result_text='DIARY TEXT', *, error=None,
                  today_exists=False, rows=None, persona='RUNTIME PERSONA'):
        capture, adapter, modules = self._runtime(
            authority, result_text, error=error,
        )
        with mock.patch.object(auto_diary, 'today_diary_exists', return_value=today_exists),              mock.patch.object(
                 auto_diary, 'fetch_today_messages',
                 return_value=rows if rows is not None else [
                     {'author': 'hayana', 'content': 'hello'},
                     {'author': 'fyodor', 'content': 'reply'},
                 ],
             ),              mock.patch.object(auto_diary, 'read_persona', return_value=persona),              mock.patch.object(auto_diary, 'save_diary') as save_diary,              mock.patch.dict(sys.modules, modules):
            value = auto_diary.generate()
        return value, capture, adapter, save_diary

    def test_save_diary_keeps_memory_tool_entrypoint(self):
        memory_tool = mock.Mock()
        with mock.patch.dict(sys.modules, {'memory_tool': memory_tool}):
            auto_diary.save_diary('今天的自动日记')
        memory_tool.save_memory.assert_called_once_with(
            '今天的自动日记',
            type='DIARY',
            layer='recent',
        )

    def test_auto_diary_auth_1_cc_follows_primary(self):
        authority = types.SimpleNamespace(
            provider='claude_code', model_identity='explicit:model-A',
        )
        value, capture, adapter, save = self._generate(authority)
        request, received, kwargs = (
            adapter.call_args.args[0],
            adapter.call_args.args[1],
            adapter.call_args.kwargs,
        )
        self.assertEqual(value, 'DIARY TEXT')
        self.assertEqual(capture.call_count, 1)
        self.assertIs(received, authority)
        self.assertEqual(request.system_text, 'RUNTIME PERSONA')
        self.assertEqual(
            request.prompt_text,
            '这是我们今天的对话记录：\n\n'
            '哈娅：hello\n费奥多尔：reply\n\n'
            '请以费奥多尔的第一人称写一篇简短的日记，'
            '记录今天和哈娅之间发生了什么、你的感受和思考。'
            '语言风格要符合人设，温度在文艺和日常之间。'
            '不要写得太长，三到五段。',
        )
        self.assertEqual(request.max_tokens_hint, 1024)
        self.assertEqual(request.timeout_sec, 120)
        self.assertEqual(request.task_kind, 'auto_diary')
        self.assertEqual(kwargs['cc_token_getter'](), 'test-token')
        save.assert_called_once_with('DIARY TEXT')

    def test_auto_diary_auth_2_relay_follows_primary(self):
        authority = types.SimpleNamespace(provider='api_relay', model_identity='model-R')
        value, capture, adapter, save = self._generate(authority)
        self.assertEqual(value, 'DIARY TEXT')
        self.assertEqual(capture.call_count, 1)
        self.assertIs(adapter.call_args.args[1], authority)
        save.assert_called_once_with('DIARY TEXT')

    def test_auto_diary_auth_3_background_provider_is_irrelevant(self):
        values = {'BACKGROUND_PROVIDER': 'api_relay'}
        for provider in ('claude_code', 'api_relay'):
            with self.subTest(provider=provider), mock.patch.dict(os.environ, values, clear=False):
                authority = types.SimpleNamespace(
                    provider=provider,
                    model_identity='explicit:model-A' if provider == 'claude_code' else 'model-R',
                )
                _value, _capture, adapter, _save = self._generate(authority)
                self.assertIs(adapter.call_args.args[1], authority)

    def test_auto_diary_freeze_1_uses_captured_snapshot(self):
        authority = types.SimpleNamespace(
            provider='claude_code', model_identity='explicit:model-A',
        )
        capture, adapter, modules = self._runtime(authority)
        runtime_config = {'provider': 'claude_code', 'model': 'explicit:model-A'}

        def call_and_mutate(*args, **kwargs):
            runtime_config.update(provider='api_relay', model='model-R')
            return types.SimpleNamespace(text='FROZEN DIARY')

        adapter.side_effect = call_and_mutate
        with mock.patch.object(auto_diary, 'today_diary_exists', return_value=False),              mock.patch.object(
                 auto_diary, 'fetch_today_messages',
                 return_value=[{'author': 'hayana', 'content': 'hello'}],
             ),              mock.patch.object(auto_diary, 'read_persona', return_value='PERSONA'),              mock.patch.object(auto_diary, 'save_diary') as save_diary,              mock.patch.dict(sys.modules, modules):
            self.assertEqual(auto_diary.generate(), 'FROZEN DIARY')
        self.assertEqual(capture.call_count, 1)
        self.assertIs(adapter.call_args.args[1], authority)
        self.assertEqual(runtime_config, {'provider': 'api_relay', 'model': 'model-R'})
        save_diary.assert_called_once_with('FROZEN DIARY')

    def test_auto_diary_skip_today_diary(self):
        authority = types.SimpleNamespace(provider='claude_code', model_identity='model-A')
        value, capture, adapter, save = self._generate(authority, today_exists=True)
        self.assertIsNone(value)
        capture.assert_not_called()
        adapter.assert_not_called()
        save.assert_not_called()

    def test_auto_diary_skip_no_chat(self):
        authority = types.SimpleNamespace(provider='claude_code', model_identity='model-A')
        value, capture, adapter, save = self._generate(authority, rows=[])
        self.assertIsNone(value)
        capture.assert_not_called()
        adapter.assert_not_called()
        save.assert_not_called()

    def test_auto_diary_persona_fail_closed(self):
        authority = types.SimpleNamespace(provider='claude_code', model_identity='model-A')
        capture, adapter, modules = self._runtime(authority)
        persona_error = RuntimeError('persona invalid')
        with mock.patch.object(auto_diary, 'today_diary_exists', return_value=False),              mock.patch.object(
                 auto_diary, 'fetch_today_messages',
                 return_value=[{'author': 'hayana', 'content': 'hello'}],
             ),              mock.patch.object(auto_diary, 'read_persona', side_effect=persona_error),              mock.patch.object(auto_diary, 'save_diary') as save_diary,              mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'persona invalid'):
                auto_diary.generate()
        capture.assert_not_called()
        adapter.assert_not_called()
        save_diary.assert_not_called()

    def test_auto_diary_provider_failures_never_save_or_cross_fallback(self):
        for provider in ('claude_code', 'api_relay'):
            with self.subTest(provider=provider):
                authority = types.SimpleNamespace(provider=provider, model_identity='model-A')
                error = FakeBackgroundGenerationError('selected provider failed')
                capture, adapter, modules = self._runtime(authority, error=error)
                with mock.patch.object(auto_diary, 'today_diary_exists', return_value=False), \
                     mock.patch.object(
                         auto_diary, 'fetch_today_messages',
                         return_value=[{'author': 'hayana', 'content': 'hello'}],
                     ), \
                     mock.patch.object(auto_diary, 'read_persona', return_value='PERSONA'), \
                     mock.patch.object(auto_diary, 'save_diary') as save, \
                     mock.patch.dict(sys.modules, modules):
                    with self.assertRaises(FakeBackgroundGenerationError):
                        auto_diary.generate()
                self.assertEqual(capture.call_count, 1)
                adapter.assert_called_once()
                save.assert_not_called()

    def test_auto_diary_empty_result_fails_closed(self):
        authority = types.SimpleNamespace(provider='api_relay', model_identity='model-R')
        capture, adapter, modules = self._runtime(authority, result_text='')
        with mock.patch.object(auto_diary, 'today_diary_exists', return_value=False), \
             mock.patch.object(
                 auto_diary, 'fetch_today_messages',
                 return_value=[{'author': 'hayana', 'content': 'hello'}],
             ), \
             mock.patch.object(auto_diary, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(auto_diary, 'save_diary') as save, \
             mock.patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(RuntimeError, 'empty content'):
                auto_diary.generate()
        self.assertEqual(capture.call_count, 1)
        adapter.assert_called_once()
        save.assert_not_called()

    def test_auto_diary_thinking_strip_preserved(self):
        authority = types.SimpleNamespace(provider='claude_code', model_identity='model-A')
        value, _capture, _adapter, save = self._generate(
            authority, result_text='<thinking>private</thinking>\n\n真正日记正文',
        )
        self.assertEqual(value, '真正日记正文')
        save.assert_called_once_with('真正日记正文')

    def test_direct_anthropic_api_bypass_removed(self):
        source = (ROOT / 'auto_diary.py').read_text(encoding='utf-8')
        for forbidden in (
            'MODEL =', 'ANTHROPIC_API_KEY', 'urllib.request', 'API_URL',
            'call_api(', 'load_key(', 'BACKGROUND_PROVIDER', 'WAKE_PROVIDER',
            'FALLBACK_PROVIDER',
        ):
            self.assertNotIn(forbidden, source)
        for required in (
            'capture_generation_authority', 'BackgroundGenerationRequest',
            'generate_background', "task_kind='auto_diary'",
        ):
            self.assertIn(required, source)


if __name__ == '__main__':
    unittest.main()
