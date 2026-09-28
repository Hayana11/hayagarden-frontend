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
    WAKE_DRY_RUN_CAPABILITY_TEXT,
)
from wake.runners import (
    ApiRelayWakeRunner,
    DISABLED_CC_WAKE_MODES,
    WAKE_MODE_DISABLED_REASON,
    UnsupportedWakeModeError,
    WakeRequest,
    get_wake_runner,
    inspect_wake_plan,
    prepare_tools_for_provider,
    register_wake_runners,
    select_wake_provider,
    split_wake_system,
    wake_mode_disabled,
    wake_mode_disabled_payload,
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
            for mode in DISABLED_CC_WAKE_MODES:
                with self.subTest(mode=mode):
                    with self.assertRaisesRegex(
                        UnsupportedWakeModeError, WAKE_MODE_DISABLED_REASON,
                    ):
                        select_wake_provider(mode)

    def test_dream_is_surface_owned_and_summarize_stays_background(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'inherit',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            with self.assertRaisesRegex(
                UnsupportedWakeModeError,
                'surface-owned Background Generation Adapter',
            ):
                select_wake_provider('dream')
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
    def test_legacy_tool_compat_symbols_are_gone(self):
        import wake.cc_tools as cc_tools
        for name in (
            'WAKE_TO_CC_MCP',
            'cc_wake_allowed_tools',
            'cc_wake_tool_names',
            'cc_wake_nudge_text',
            'filter_wake_tools_for_cc',
            'is_cc_wake_tool',
        ):
            self.assertFalse(hasattr(cc_tools, name), name)

    def test_capability_brochure_still_matches_inspect_profile(self):
        self.assertIn('add_todo', CC_WAKE_CAPABILITY_TEXT)
        self.assertIn('位置', CC_WAKE_CAPABILITY_TEXT)
        self.assertIn('不可用：codebase patch/create_file', CC_WAKE_CAPABILITY_TEXT)
        self.assertIn('不得调用 light_on', CC_WAKE_CAPABILITY_TEXT)
        self.assertNotIn('explain_history', CC_WAKE_CAPABILITY_TEXT.split('不可用')[0])
        self.assertIn('explain_history', CC_WAKE_CAPABILITY_TEXT)
        self.assertIn('THOUGHTS/ACTION/CONTENT', WAKE_DRY_RUN_CAPABILITY_TEXT)


class WakeRunnerContractTests(unittest.TestCase):
    def test_api_relay_runner_preserves_loop_output(self):
        def loop_fn(system, messages, max_rounds=6, tools=None, t_hours=0.0, mode='normal', dry_run=False):
            self.assertEqual(mode, 'normal')
            self.assertEqual(t_hours, 1.5)
            self.assertFalse(dry_run)
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

    def test_api_relay_dry_run_forwards_flag_and_empty_tools(self):
        seen = {}

        def loop_fn(system, messages, max_rounds=6, tools=None, t_hours=0.0, mode='normal', dry_run=False):
            seen['tools'] = list(tools or [])
            seen['dry_run'] = dry_run
            return 'THOUGHTS: t\nACTION: none\nCONTENT: ', {'provider': 'api_relay', 'model': 'm'}

        runner = ApiRelayWakeRunner(loop_fn)
        runner.run(WakeRequest(
            mode='normal', system='s',
            messages=[{'role': 'user', 'content': '[唤醒检查]'}],
            tools=[], t_hours=2.0, dry_run=True,
        ))
        self.assertEqual(seen['tools'], [])
        self.assertTrue(seen['dry_run'])

    def test_register_and_get_runners(self):
        a = ApiRelayWakeRunner(lambda *a, **k: ('', {'provider': 'api_relay'}))
        register_wake_runners(api_relay=a)
        self.assertIs(get_wake_runner('api_relay'), a)
        with self.assertRaisesRegex(RuntimeError, '没有可用的 Wake runner: provider=claude_code'):
            get_wake_runner('claude_code')

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
        self.assertEqual(
            plan['tool_names'],
            ['search_memories', 'get_location', 'get_light_status'],
        )
        self.assertEqual(plan['relay_only_removed'], [])
        self.assertEqual(plan['cc_allowed_tools'], [])

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


class WakeResidentRetirementTests(unittest.TestCase):
    def _production_py_files(self):
        root = Path(ROOT)
        skip_dirs = {'.git', 'tests', 'node_modules', 'app', '__pycache__'}
        files = []
        for path in root.rglob('*.py'):
            if any(part in skip_dirs for part in path.parts):
                continue
            files.append(path)
        return files

    def _production_src(self):
        return '\n'.join(
            path.read_text(encoding='utf-8') for path in self._production_py_files()
        )

    def test_retired_symbols_have_zero_production_refs(self):
        src = self._production_src()
        self.assertEqual(src.count('_CC_WAKE_RESIDENT'), 0)
        self.assertEqual(src.count('ClaudeCodeWakeRunner'), 0)
        self.assertEqual(src.count('SharedResidentWakeRunner'), 0)
        self.assertEqual(src.count('_run_shared_claude_wake'), 0)
        self.assertEqual(src.count('WAKE_CONTRACT'), 0)
        self.assertEqual(src.count('FORMAT_NUDGE'), 0)
        self.assertEqual(src.count('WAKE_TO_CC_MCP'), 0)
        self.assertEqual(src.count('cc_wake_allowed_tools'), 0)
        self.assertEqual(src.count('cc_wake_tool_names'), 0)
        self.assertEqual(src.count('cc_wake_nudge_text'), 0)
        self.assertEqual(src.count('filter_wake_tools_for_cc'), 0)

    def test_single_persistent_conversational_resident(self):
        gateway = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        self.assertEqual(
            gateway.count(
                'cc_resident.ResidentSession(CC_CWD, CC_ALLOWED_TOOLS'
            ),
            1,
        )
        self.assertIn('_CC_RESIDENT = _SwappableResident(', gateway)
        self.assertEqual(gateway.count('_CC_WAKE_RESIDENT'), 0)

    def test_canonical_normal_wake_keeps_unified_main_chat_route(self):
        gateway = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        decision_start = gateway.index('def _wake_decide_locked')
        basic_start = gateway.index('if basic_normal:', decision_start)
        basic_end = gateway.index('    planner_view = None', basic_start)
        normal_route = gateway[basic_start:basic_end]
        self.assertIn('_run_unified_normal_main_chat_turn(', normal_route)
        self.assertNotIn('SharedResidentWakeRunner', normal_route)
        self.assertNotIn('_run_shared_claude_wake', normal_route)
        unified = gateway[
            gateway.index('def _run_unified_normal_main_chat_turn'):
            gateway.index('def _cross_surface_recap_from_solo_chat')
        ]
        self.assertIn("turn_mode='wake'", unified)
        self.assertIn('prepare_shared_transcript_watermark', unified)
        self.assertIn('begin_shared_wake_delivery_fence', unified)
        self.assertNotIn('WAKE_CONTRACT', unified)
        self.assertNotIn('THOUGHTS:', unified)
        self.assertIn("turn_mode='chat'", gateway)
        runners = (Path(ROOT) / 'wake' / 'runners.py').read_text(encoding='utf-8')
        self.assertNotIn('ClaudeCodeWakeRunner', runners)
        self.assertNotIn('ResidentSession(', runners)
        self.assertNotIn('shared_delivery_fence', runners)

    def test_disabled_modes_are_fail_closed_in_runners(self):
        for mode in ('morning', 'nightwatch', 'ritual', 'self_trigger'):
            self.assertTrue(wake_mode_disabled(mode), mode)
            payload = wake_mode_disabled_payload(mode)
            self.assertEqual(payload['reason'], WAKE_MODE_DISABLED_REASON)
            self.assertEqual(payload['mode'], mode)
            self.assertEqual(payload['detail'], 'mode=%s' % mode)
            self.assertTrue(payload['skipped'])
        self.assertFalse(wake_mode_disabled('normal'))
        self.assertFalse(wake_mode_disabled('summarize'))
        self.assertFalse(wake_mode_disabled('dream'))
        self.assertIsNone(wake_mode_disabled_payload('normal'))


class DisabledWakeModeHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gateway
        cls.gateway = gateway
        cls.client = gateway.app.test_client()

    def _assert_disabled(self, mode, extra=None):
        payload = {'mode': mode, 'wake_run_id': 'disabled-%s' % mode}
        if extra:
            payload.update(extra)
        with mock.patch.object(self.gateway, '_ensure_wake_runners') as ensure, \
             mock.patch.object(self.gateway, '_wake_agent_loop') as loop, \
             mock.patch.object(
                 self.gateway, '_run_unified_normal_main_chat_turn',
             ) as unified, \
             mock.patch('wake.executor.execute') as execute, \
             mock.patch('relay.manager.relay') as relay:
            relay.chat = mock.Mock(side_effect=AssertionError('relay fallback'))
            resp = self.client.post('/wake', json=payload)
        self.assertEqual(resp.status_code, 200, mode)
        body = resp.get_json()
        self.assertEqual(body.get('ok'), True, body)
        self.assertEqual(body.get('skipped'), True, body)
        self.assertEqual(body.get('reason'), WAKE_MODE_DISABLED_REASON, body)
        self.assertEqual(body.get('mode'), mode, body)
        self.assertEqual(body.get('detail'), 'mode=%s' % mode, body)
        ensure.assert_not_called()
        loop.assert_not_called()
        unified.assert_not_called()
        execute.assert_not_called()
        return body

    def test_morning_disabled(self):
        self._assert_disabled('morning')

    def test_nightwatch_disabled(self):
        self._assert_disabled('nightwatch')

    def test_ritual_disabled(self):
        self._assert_disabled('ritual')

    def test_self_trigger_disabled(self):
        self._assert_disabled('self_trigger')

    def test_disabled_modes_ignore_inspect_dry_run_and_relay_provider(self):
        for mode in DISABLED_CC_WAKE_MODES:
            with self.subTest(mode=mode, extra='inspect_only'):
                self._assert_disabled(mode, extra={'inspect_only': True})
            with self.subTest(mode=mode, extra='dry_run'):
                self._assert_disabled(mode, extra={'dry_run': True})

    def test_summarize_still_selects_api_relay(self):
        with mock.patch.object(config_store, 'get', side_effect=fake_get({
            'CHAT_PROVIDER': 'claude_code',
            'WAKE_PROVIDER': 'claude_code',
            'BACKGROUND_PROVIDER': 'api_relay',
        })):
            self.assertEqual(select_wake_provider('summarize'), 'api_relay')

    def test_dream_still_surface_owned(self):
        with self.assertRaisesRegex(
            UnsupportedWakeModeError,
            'surface-owned Background Generation Adapter',
        ):
            select_wake_provider('dream')


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
        self.assertIn('位置', flat)
        self.assertIn('不可用：codebase patch/create_file', flat)
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

    def test_inspect_only_bypasses_lock_and_guards_in_source(self):
        src = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        # inspect_only returns before acquiring the wake exec lock.
        inspect_idx = src.find("if bool(data.get('inspect_only')):")
        lock_idx = src.find('_wake_exec_lock.acquire', inspect_idx)
        self.assertGreater(inspect_idx, 0)
        self.assertGreater(lock_idx, inspect_idx)
        self.assertIn('def _wake_inspect_only', src)
        self.assertIn('bypass wake lock', src)


class WakePreflightNoSideEffectTests(unittest.TestCase):
    def test_prompt_sizes_does_not_consume_dream_pool(self):
        from tools import wake_preflight as wp
        from chat import system_builder

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'sys.db')
        conn = sqlite3.connect(db_path)
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
            CREATE TABLE period_records (id INTEGER PRIMARY KEY, type TEXT, date TEXT, note TEXT);
            CREATE TABLE ledger (id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT);
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL, month TEXT);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, consumed INTEGER DEFAULT 0,
                woke_at TEXT, thoughts TEXT
            );
            CREATE TABLE countdowns (
                id INTEGER PRIMARY KEY, title TEXT, target_date TEXT, emoji TEXT, type TEXT
            );
            INSERT INTO dream_pool(id, content, tone, surfaced, surface_count, created_at)
            VALUES (1, 'preflight 不该吃掉的梦', 'drifting', 0, 0, '2026-07-01 00:00:00');
            """
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        def snap():
            c = get_db()
            row = c.execute(
                'SELECT surfaced, surface_count, content FROM dream_pool WHERE id=1'
            ).fetchone()
            c.close()
            return (row['surfaced'], row['surface_count'], row['content'])

        shared = types.SimpleNamespace(
            persona='PERSONA', relationship_context='', relationship_fingerprint='x',
        )
        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = get_db
        before = snap()
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder.config_store, 'get_bool', return_value=False), \
             mock.patch('random.random', return_value=0.0):
            for _ in range(15):
                wp._prompt_sizes()
        self.assertEqual(snap(), before)


class DryRunCapabilityTests(unittest.TestCase):
    def test_dry_run_profile_has_no_normal_tool_brochure(self):
        from chat import system_builder

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = str(Path(tmp.name) / 'sys.db')
        conn = sqlite3.connect(db_path)
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
            CREATE TABLE period_records (id INTEGER PRIMARY KEY, type TEXT, date TEXT, note TEXT);
            CREATE TABLE ledger (id INTEGER PRIMARY KEY, amount REAL, category TEXT, date TEXT);
            CREATE TABLE ledger_budget (id INTEGER PRIMARY KEY, amount REAL, month TEXT);
            CREATE TABLE wake_log (
                id INTEGER PRIMARY KEY, action TEXT, content TEXT, consumed INTEGER DEFAULT 0,
                woke_at TEXT, thoughts TEXT
            );
            CREATE TABLE countdowns (
                id INTEGER PRIMARY KEY, title TEXT, target_date TEXT, emoji TEXT, type TEXT
            );
            """
        )
        conn.commit()
        conn.close()

        def get_db():
            c = sqlite3.connect(db_path)
            c.row_factory = sqlite3.Row
            return c

        shared = types.SimpleNamespace(
            persona='PERSONA', relationship_context='', relationship_fingerprint='x',
        )
        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = get_db
        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder.config_store, 'get_bool', return_value=False):
            blocks = system_builder.build_system(
                wake=True,
                include_relationship_context=False,
                allow_side_effects=False,
                capability_profile='wake_dry_run',
            )
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertIn('演习模式', flat)
        self.assertIn('无任何工具', flat)
        self.assertNotIn('Wake·Claude Code 工具面', flat)
        self.assertNotIn('查看与发布留言板', flat)
        self.assertNotIn('随心所欲', flat)
        self.assertNotIn('add_todo', flat)
        self.assertIn('本轮无任何工具', flat)


class RelayDryRunNudgeTests(unittest.TestCase):
    def test_empty_tools_or_dry_run_skips_tool_nudge(self):
        from wake.runners import should_prompt_readonly_tools

        # t_hours>=1 would previously force a tool nudge even with empty tools.
        self.assertFalse(should_prompt_readonly_tools(
            dry_run=True, tools=[], generative=False, tools_called=False,
            t_hours=3.0, round_i=0, max_rounds=6,
        ))
        self.assertFalse(should_prompt_readonly_tools(
            dry_run=False, tools=[], generative=False, tools_called=False,
            t_hours=3.0, round_i=0, max_rounds=6,
        ))
        self.assertTrue(should_prompt_readonly_tools(
            dry_run=False, tools=[{'name': 'get_location'}], generative=False,
            tools_called=False, t_hours=3.0, round_i=0, max_rounds=6,
        ))

    def test_gateway_loop_uses_should_prompt_helper(self):
        src = (Path(ROOT) / 'gateway.py').read_text(encoding='utf-8')
        self.assertIn('should_prompt_readonly_tools', src)
        self.assertIn('dry_run=bool(dry_run)', src)


if __name__ == '__main__':
    unittest.main()
