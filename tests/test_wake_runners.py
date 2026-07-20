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
    CC_WAKE_CAPABILITY_TEXT,
    CC_WAKE_WRITE_MCP,
    cc_wake_allowed_tools,
    cc_wake_nudge_text,
    filter_wake_tools_for_cc,
    is_cc_wake_tool,
)
from wake.runners import (
    ApiRelayWakeRunner,
    ClaudeCodeWakeRunner,
    UnsupportedWakeModeError,
    WakeRequest,
    get_wake_runner,
    inspect_wake_plan,
    prepare_tools_for_provider,
    register_wake_runners,
    select_wake_provider,
    split_wake_system,
)
from wake.usage import build_wake_cache_info
import sqlite3
import json
import types


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

    def test_background_claude_code_rejected_at_route_time(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'claude_code',
        })):
            with self.assertRaises(UnsupportedWakeModeError):
                select_wake_provider('dream')
            with self.assertRaises(UnsupportedWakeModeError):
                select_wake_provider('summarize')
            # Normal wake still allowed via WAKE_PROVIDER.
            self.assertEqual(select_wake_provider('normal'), 'claude_code')

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
        self.assertIn('可写', text)
        self.assertIn('add_todo', text)

    def test_dry_run_nudge_disables_tools(self):
        text = cc_wake_nudge_text(2.0, ['search_memories'], dry_run=True)
        self.assertIn('dry_run', text)
        self.assertIn('禁用', text)

    def test_allowlist_has_no_brain_and_matches_capability(self):
        csv = cc_wake_allowed_tools(None)
        self.assertNotIn('brain', csv)
        for mcp in CC_WAKE_WRITE_MCP:
            self.assertIn(mcp, csv)
        self.assertIn('add_todo', CC_WAKE_CAPABILITY_TEXT)
        self.assertIn('不可用：位置', CC_WAKE_CAPABILITY_TEXT)


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
        with self.assertRaises(UnsupportedWakeModeError):
            runner.run(WakeRequest(
                mode='dream', system='s', messages=[], tools=[], t_hours=0,
            ))

    def test_dry_run_prepare_tools_empties_table(self):
        tools = [{'name': 'search_memories'}, {'name': 'add_todo'}]
        self.assertEqual(
            prepare_tools_for_provider('claude_code', tools, 'normal', dry_run=True),
            [],
        )
        self.assertEqual(
            prepare_tools_for_provider('api_relay', tools, 'normal', dry_run=True),
            [],
        )

    def test_cc_dry_run_forces_empty_allowlist(self):
        seen = {}

        class _R:
            _allowed_tools = 'mcp__home__add_todo'
            _system_text = 'old'

            def ensure_alive(self, system, env):
                seen['allowed'] = self._allowed_tools
                return True

            def send_turn(self, content):
                seen['content'] = content
                yield ('done', ('THOUGHTS: t\nACTION: none\nCONTENT: ', '', {
                    'rounds': [], 'resident_turn_count': 1, 'respawn_reason': '',
                }, {}))

        runner = ClaudeCodeWakeRunner(
            _R(), token='t', cwd=tempfile.mkdtemp(),
            payload_builder=lambda **kw: dict(kw),
        )
        runner.run(WakeRequest(
            mode='normal',
            system=[{'type': 'text', 'text': 'p', 'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': '[唤醒检查]'}],
            tools=[],
            t_hours=1.0,
            dry_run=True,
        ))
        self.assertEqual(seen['allowed'], '')
        self.assertIn('dry_run', seen['content'])

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


class BuildSystemSideEffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / 'sys.db')
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY, type TEXT, content TEXT, tags TEXT,
                layer TEXT, created_at TEXT, resolved INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 0
            );
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE board (id INTEGER PRIMARY KEY, content TEXT, status TEXT,
                                author TEXT, tag TEXT, level TEXT, category TEXT);
            CREATE TABLE todos (
                id INTEGER PRIMARY KEY, content TEXT, due_date TEXT, done INTEGER DEFAULT 0
            );
            CREATE TABLE dream_events (
                id INTEGER PRIMARY KEY, type TEXT, value TEXT, created_at TEXT,
                duration_minutes REAL
            );
            CREATE TABLE dream_pool (
                id INTEGER PRIMARY KEY, content TEXT, tone TEXT, surfaced INTEGER DEFAULT 0,
                surface_count INTEGER DEFAULT 0, created_at TEXT, surfaced_at TEXT
            );
            CREATE TABLE period_records (
                id INTEGER PRIMARY KEY, type TEXT, date TEXT, note TEXT
            );
            CREATE TABLE ledger (
                id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT
            );
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL, month TEXT);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, consumed INTEGER DEFAULT 0,
                woke_at TEXT, thoughts TEXT
            );
            CREATE TABLE countdowns (
                id INTEGER PRIMARY KEY, title TEXT, target_date TEXT, emoji TEXT, type TEXT
            );
            INSERT INTO dream_pool(id, content, tone, surfaced, surface_count, created_at)
            VALUES (1, '一段不该被 inspect 吃掉的梦', 'drifting', 0, 0, '2026-07-01 00:00:00');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def get_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _snapshot_dream(self):
        conn = self.get_db()
        row = conn.execute(
            'SELECT surfaced, surface_count, content FROM dream_pool WHERE id=1'
        ).fetchone()
        conn.close()
        return (row['surfaced'], row['surface_count'], row['content'])

    def test_allow_side_effects_false_never_consumes_dream_pool(self):
        from chat import system_builder

        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context='',
            relationship_fingerprint='x',
        )

        def get_bool(key, default=False):
            return False

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        before = self._snapshot_dream()
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder.config_store, 'get_bool', side_effect=get_bool), \
             mock.patch('random.random', return_value=0.0):
            for _ in range(20):
                system_builder.build_system(
                    wake=True,
                    include_relationship_context=False,
                    allow_side_effects=False,
                    capability_profile='cc_wake',
                )
        self.assertEqual(self._snapshot_dream(), before)

    def test_cc_wake_profile_excludes_relay_tool_brochure(self):
        from chat import system_builder

        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context='',
            relationship_fingerprint='x',
        )

        def get_bool(key, default=False):
            return False

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = self.get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder.config_store, 'get_bool', side_effect=get_bool):
            blocks = system_builder.build_system(
                wake=True,
                include_relationship_context=False,
                allow_side_effects=False,
                capability_profile='cc_wake',
            )
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertIn('Wake·Claude Code 工具面', flat)
        # Generic Relay brochure must not appear (these phrases are unique to it).
        self.assertNotIn('查看与发布留言板', flat)
        self.assertNotIn('随心所欲', flat)
        self.assertNotIn('请求手机截屏', flat)
        self.assertNotIn('查位置', flat)
        self.assertNotIn('Pocket 浏览器', flat)
        # Accurate CC profile may list unavailable tools by name.
        self.assertIn('不可用：位置', flat)
        self.assertIn('add_todo', flat)
        self.assertIn('联网搜索', flat)  # only inside the 不可用 list


class WakePreflightStatsTests(unittest.TestCase):
    def test_counts_completed_runs_and_model_rounds(self):
        from tools import wake_preflight as wp

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / 'm.db'
        conn = sqlite3.connect(db)
        conn.execute(
            'CREATE TABLE wake_log (id INTEGER PRIMARY KEY, action TEXT, woke_at TEXT, cache_info TEXT)'
        )
        # One decision with 3 model rounds; one with 1.
        conn.execute(
            "INSERT INTO wake_log(action, woke_at, cache_info) VALUES ('none', datetime('now'), ?)",
            (json.dumps({'mode': 'normal', 'num_rounds': 3}),),
        )
        conn.execute(
            "INSERT INTO wake_log(action, woke_at, cache_info) VALUES ('message', datetime('now'), ?)",
            (json.dumps({'mode': 'nightwatch', 'rounds': [{}, {}]}),),
        )
        conn.commit()
        conn.close()

        conn = sqlite3.connect(db)
        stats = wp._wake_stats(conn, days=7)
        conn.close()
        self.assertEqual(stats['completed_wake_runs'], 2)
        self.assertEqual(stats['successful_model_rounds'], 5)  # 3 + len(rounds)=2
        self.assertEqual(stats['failed_model_runs'], 'unavailable')
        self.assertNotIn('model_calls', stats)


class WakeRunIdDryRunContractTests(unittest.TestCase):
    def test_dry_run_must_not_mark_run_id_in_gateway_source(self):
        src = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        # The dry_run *return* path must not mark run_id.
        marker = "No executor, no drive/desire/dream writes, no wake_run_id mark"
        dry_idx = src.find(marker)
        self.assertGreater(dry_idx, 0)
        dry_block = src[dry_idx: dry_idx + 600]
        self.assertIn("'dry_run': True", dry_block)
        self.assertNotIn('_wake_run_id_mark', dry_block)
        # Mark only happens on the live path after executor.
        mark_idx = src.find('_wake_run_id_mark(wake_run_id)', dry_idx)
        self.assertGreater(mark_idx, dry_idx)


if __name__ == '__main__':
    unittest.main()

