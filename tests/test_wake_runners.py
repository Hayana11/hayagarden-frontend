"""B1 Wake provider routing + runner contract (no live model calls)."""

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
    str(Path(tempfile.gettempdir()) / 'hayagarden-test-b1-wake-config.db'),
)

import config_store
from wake.cc_tools import (
    cc_wake_allowed_tools,
    cc_wake_nudge_text,
    filter_wake_tools_for_cc,
    is_cc_wake_tool,
)
from wake.runners import (
    ApiRelayWakeRunner,
    ClaudeCodeWakeRunner,
    WakeRequest,
    get_wake_runner,
    inspect_wake_plan,
    prepare_tools_for_provider,
    register_wake_runners,
    select_wake_provider,
    split_wake_system,
)
from wake.usage import build_wake_cache_info


def fake_get(values):
    def _get(key, default=None):
        return values.get(key, default)
    return _get


class WakeProviderSelectTests(unittest.TestCase):
    def test_inherit_follows_chat(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(select_wake_provider('normal'), 'claude_code')
            self.assertEqual(select_wake_provider('nightwatch'), 'claude_code')
            self.assertEqual(select_wake_provider('ritual'), 'claude_code')
            self.assertEqual(select_wake_provider('self_trigger'), 'claude_code')

    def test_dream_summarize_use_background(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(select_wake_provider('dream'), 'api_relay')
            self.assertEqual(select_wake_provider('summarize'), 'api_relay')

    def test_explicit_force_overrides_chat(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'api_relay',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(select_wake_provider('normal'), 'api_relay')
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'api_relay',
            'WAKE_PROVIDER': 'claude_code',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(select_wake_provider('normal'), 'claude_code')


class WakeCcToolsTests(unittest.TestCase):
    def test_filters_relay_only_tools(self):
        tools = [
            {'name': 'search_memories'},
            {'name': 'get_location'},
            {'name': 'get_light_status'},
            {'name': 'recall_photo'},
            {'name': 'codebase_read_file'},
        ]
        filtered = filter_wake_tools_for_cc(tools)
        names = [t['name'] for t in filtered]
        self.assertEqual(names, ['search_memories', 'get_light_status', 'codebase_read_file'])
        self.assertFalse(is_cc_wake_tool('get_location'))
        self.assertIn('mcp__home__search_memories', cc_wake_allowed_tools(filtered))

    def test_nudge_never_mentions_missing_tools(self):
        text = cc_wake_nudge_text(2.0, ['search_memories', 'get_light_status'])
        self.assertIn('search_memories', text)
        self.assertNotIn('get_location', text)
        self.assertNotIn('recall_photo', text)
        self.assertIn('不要假装', text)


class WakeRunnerContractTests(unittest.TestCase):
    def test_api_relay_runner_preserves_loop_output(self):
        def loop_fn(system, messages, max_rounds=6, tools=None, t_hours=0.0, mode='normal'):
            self.assertEqual(mode, 'normal')
            self.assertEqual(t_hours, 1.5)
            return 'THOUGHTS: x\nACTION: none\nCONTENT: ', {
                'v': 2, 'provider': 'api_relay', 'mode': 'normal', 'model': 'm1',
            }

        runner = ApiRelayWakeRunner(loop_fn)
        result = runner.run(WakeRequest(
            mode='normal', system='s', messages=[{'role': 'user', 'content': '[唤醒检查]'}],
            tools=[], t_hours=1.5,
        ))
        self.assertEqual(result.provider, 'api_relay')
        self.assertEqual(result.model, 'm1')
        self.assertIn('ACTION: none', result.raw_text)
        self.assertEqual(result.cache_info['provider'], 'api_relay')

    def test_register_and_get_runners(self):
        a = ApiRelayWakeRunner(lambda *a, **k: ('', {'provider': 'api_relay'}))
        class _FakeResident:
            _allowed_tools = ''
            def ensure_alive(self, *a, **k):
                return True
            def send_turn(self, content):
                yield ('done', ('THOUGHTS: t\nACTION: none\nCONTENT: ', '', {
                    'rounds': [{'index': 1, 'input_tokens': 1, 'output_tokens': 1,
                                'cache_read': 0, 'cache_creation': 0, 'context_tokens': 1}],
                    'resident_turn_count': 1,
                    'respawn_reason': '',
                }, {}))

        c = ClaudeCodeWakeRunner(
            _FakeResident(),
            token='tok',
            cwd=tempfile.mkdtemp(),
            payload_builder=lambda **kw: dict(kw),
        )
        register_wake_runners(api_relay=a, claude_code=c)
        self.assertIs(get_wake_runner('api_relay'), a)
        self.assertIs(get_wake_runner('claude_code'), c)
        result = c.run(WakeRequest(
            mode='normal',
            system=[{'type': 'text', 'text': 'persona', 'cache_control': {'type': 'ephemeral'}},
                    {'type': 'text', 'text': 'dynamic a1'}],
            messages=[{'role': 'user', 'content': '[唤醒检查]'}],
            tools=[{'name': 'search_memories'}],
            t_hours=2.0,
        ))
        self.assertEqual(result.provider, 'claude_code')
        self.assertEqual(result.cache_info['provider'], 'claude_code')
        self.assertEqual(result.cache_info['source'], 'wake')
        self.assertEqual(result.cache_info['resident_turn_count'], 1)

    def test_cc_runner_rejects_dream_mode(self):
        class _R:
            _allowed_tools = ''
            def ensure_alive(self, *a, **k):
                return True
            def send_turn(self, content):
                if False:
                    yield None

        runner = ClaudeCodeWakeRunner(
            _R(), token='t', cwd=tempfile.mkdtemp(),
            payload_builder=lambda **kw: dict(kw),
        )
        with self.assertRaises(RuntimeError):
            runner.run(WakeRequest(
                mode='dream', system='s', messages=[], tools=[], t_hours=0,
            ))

    def test_split_system_keeps_cache_blocks_stable(self):
        stable, dynamic = split_wake_system([
            {'type': 'text', 'text': 'persona', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'a1 block'},
            {'type': 'text', 'text': 'drive'},
        ])
        self.assertEqual(stable, 'persona')
        self.assertIn('a1 block', dynamic)
        self.assertIn('drive', dynamic)

    def test_inspect_plan_for_cc(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            plan = inspect_wake_plan(
                mode='normal',
                system=[{'type': 'text', 'text': 'p', 'cache_control': {'type': 'ephemeral'}},
                        {'type': 'text', 'text': 'dyn'}],
                messages=[{'role': 'user', 'content': '[唤醒检查]'}],
                tools=[
                    {'name': 'search_memories'},
                    {'name': 'get_location'},
                    {'name': 'get_light_status'},
                ],
                t_hours=1.0,
            )
        self.assertEqual(plan['provider'], 'claude_code')
        self.assertEqual(plan['tool_names'], ['search_memories', 'get_light_status'])
        self.assertIn('get_location', plan['relay_only_removed'])

    def test_prepare_tools_relay_unchanged(self):
        tools = [{'name': 'get_location'}, {'name': 'search_memories'}]
        self.assertEqual(
            prepare_tools_for_provider('api_relay', tools, 'normal'),
            tools,
        )


class WakeUsageProviderTests(unittest.TestCase):
    def test_build_cache_info_records_actual_provider(self):
        payload = build_wake_cache_info(
            [{'index': 1, 'cache_read': 10, 'cache_creation': 0,
              'cache_creation_5m': 0, 'cache_creation_1h': 0,
              'input_tokens': 1, 'output_tokens': 2, 'context_tokens': 11}],
            elapsed_sec=1,
            cache_supported=True,
            mode='normal',
            model='claude-code',
            payload_builder=lambda **kw: {'cost_usd': 0},
            provider='claude_code',
            resident_turn_count=2,
            respawn_reason='',
        )
        self.assertEqual(payload['provider'], 'claude_code')
        self.assertEqual(payload['resident_turn_count'], 2)
        self.assertEqual(payload['source'], 'wake')


class WakeResidentSeparationTests(unittest.TestCase):
    def test_gateway_defines_separate_cc_wake_resident(self):
        src = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('_CC_WAKE_RESIDENT', src)
        self.assertIn('_CC_RESIDENT = cc_resident.ResidentSession', src)
        self.assertIn('_CC_WAKE_RESIDENT = cc_resident.ResidentSession', src)
        # Two separate constructions — wake must not alias the chat resident.
        self.assertNotIn('_CC_WAKE_RESIDENT = _CC_RESIDENT', src)

    def test_two_resident_sessions_are_independent_objects(self):
        import cc_resident
        cwd = tempfile.mkdtemp()
        a = cc_resident.ResidentSession(cwd, 'mcp__home__get_todos', cwd + '/cc-tools.json')
        b = cc_resident.ResidentSession(cwd, 'mcp__home__search_memories', cwd + '/cc-tools.json')
        self.assertIsNot(a, b)
        self.assertNotEqual(a.allowed_tools, b.allowed_tools)


if __name__ == '__main__':
    unittest.main()
