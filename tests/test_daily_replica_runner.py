"""9A-R runner contract tests with fake residents and a fake native seed."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from chat.daily_replica_ab import ReplicaContractError, build_daily_replica_pair
from chat.daily_replica_runner import (
    NativeSeed,
    run_daily_replica_experiment,
    run_daily_replica_production,
)


def _formatter(*, assembly, user_content, is_cold, is_respawn):
    prefix = str(assembly.get('state') or '')
    history = assembly.get('current_day_history') or []
    if history:
        rendered = '|'.join('%s:%s' % (m['role'], m['content']) for m in history)
        return prefix + '\nHISTORY=' + rendered + '\n' + user_content
    return prefix + '\n' + user_content


def _plan():
    return build_daily_replica_pair(
        assembly={
            'state': 'STATE=same',
            'current_day_history': [
                {'role': 'user', 'content': 'u1', 'message_id': 1},
                {'role': 'assistant', 'content': 'a1', 'message_id': 2},
            ],
            'manifest': {'state_injected': True},
        },
        user_content='current',
        static_system_sha256='s',
        persona_sha256='p',
        provider='claude_code',
        model='model',
        tool_profile='text_only',
        allowed_tools_sha256='tools',
        mcp_config_sha256='mcp',
        formal_formatter=_formatter,
    )


class FakeResident:
    def __init__(self, cwd, tools, mcp, *, request_tool=False):
        self.cwd = cwd
        self.tools = tools
        self.mcp = mcp
        self.request_tool = request_tool
        self.spawn = None
        self.prompt = None
        self.killed = False

    def spawn_fresh_named(self, system, env, *, session_id, tool_profile, reason):
        self.spawn = ('fresh', system, env, session_id, tool_profile, reason)

    def spawn_resumable(self, system, env, *, resume_session_id, tool_profile, reason):
        self.spawn = ('resume', system, env, resume_session_id, tool_profile, reason)

    def send_turn(self, prompt, commit_meta=None):
        self.prompt = prompt
        yield ('think', 'direct')
        if self.request_tool:
            yield ('tool_use', {'name': 'mcp__home__light_off'})
            return
        yield ('text', 'reply')
        yield ('done', ('reply', 'direct', {'output_tokens': 1}))

    def _kill(self, quiet=True):
        self.killed = True


class DailyReplicaRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.created = []
        self.ids = iter(['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'])

    def tearDown(self):
        self.tmp.cleanup()

    def _resident_factory(self, cwd, tools, mcp):
        obj = FakeResident(cwd, tools, mcp)
        self.created.append(obj)
        return obj

    def _seed(self, **kwargs):
        path = self.root / (kwargs['session_id'] + '.jsonl')
        path.write_text('seed', encoding='utf-8')
        return NativeSeed(kwargs['session_id'], path, 'seed-sha', 2)

    def _session_path(self, cwd, session_id, claude_home):
        path = self.root / (session_id + '.jsonl')
        if session_id.startswith('a'):
            path.write_text('fresh', encoding='utf-8')
        return path

    def test_a_runs_alone_and_stops_before_seed_or_b(self):
        seed_called = []
        result = run_daily_replica_production(
            plan=_plan(),
            full_system='same-system',
            env={'TOKEN': 'same'},
            cwd='/opt/frontend',
            claude_home=str(self.root),
            allowed_tools='same-tools',
            mcp_config_path='/opt/frontend/cc-tools.json',
            tool_profile='text_only',
            resident_factory=self._resident_factory,
            session_id_factory=lambda: next(self.ids),
            session_path_resolver=self._session_path,
        )
        self.assertEqual(self.created[0].spawn[0], 'fresh')
        self.assertEqual(self.created[0].spawn[4], 'text_only')
        self.assertIn('HISTORY=', self.created[0].prompt)
        self.assertEqual(result.result.content, 'reply')
        self.assertTrue(result.manifest['runner_contract_ok'])
        self.assertEqual(result.manifest['a_reproduction_gate'], 'AWAITING_OWNER_CONFIRMATION')
        self.assertFalse(result.manifest['experiment_b_started'])
        self.assertFalse(result.manifest['formal_db_written'])
        self.assertFalse(result.manifest['formal_resident_swapped'])
        self.assertFalse((self.root / 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl').exists())

    def test_b_gate_blocks_before_seed_or_resident_creation(self):
        seed_called = []

        def seed(**kwargs):
            seed_called.append(True)
            return self._seed(**kwargs)

        with self.assertRaises(ReplicaContractError) as ctx:
            run_daily_replica_experiment(
                conn=object(), plan=_plan(), a_reproduction_confirmed=False,
                full_system='same-system', env={}, cwd='/opt/frontend',
                claude_home=str(self.root), allowed_tools='same-tools',
                mcp_config_path='/opt/frontend/cc-tools.json',
                tool_profile='text_only',
                resident_factory=self._resident_factory, seed_builder=seed,
                session_id_factory=lambda: next(self.ids),
                session_path_resolver=self._session_path,
            )
        self.assertEqual(ctx.exception.error_code, 'REPLICA_A_GATE_REQUIRED')
        self.assertEqual(seed_called, [])
        self.assertEqual(self.created, [])

    def test_b_resumes_only_after_explicit_a_confirmation(self):
        result = run_daily_replica_experiment(
            conn=object(), plan=_plan(), a_reproduction_confirmed=True,
            full_system='same-system', env={'TOKEN': 'same'},
            cwd='/opt/frontend', claude_home=str(self.root),
            allowed_tools='same-tools', mcp_config_path='/opt/frontend/cc-tools.json',
            tool_profile='text_only',
            resident_factory=self._resident_factory, seed_builder=self._seed,
            session_id_factory=lambda: next(self.ids),
            session_path_resolver=self._session_path,
        )
        self.assertEqual(self.created[0].spawn[0], 'resume')
        self.assertEqual(self.created[0].spawn[4], 'text_only')
        self.assertNotIn('HISTORY=', self.created[0].prompt)
        self.assertEqual(result.result.content, 'reply')
        self.assertEqual(result.manifest['a_reproduction_gate'], 'CONFIRMED_BY_OWNER')
        self.assertTrue(result.manifest['experiment_b_started'])
        self.assertFalse((self.root / 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl').exists())

    def test_tool_request_aborts_pair_and_cleans_both_residents(self):
        def factory(cwd, tools, mcp):
            obj = FakeResident(cwd, tools, mcp, request_tool=True)
            self.created.append(obj)
            return obj

        with self.assertRaises(ReplicaContractError) as ctx:
            run_daily_replica_production(
                plan=_plan(), full_system='same-system',
                env={}, cwd='/opt/frontend', claude_home=str(self.root),
                allowed_tools='same-tools', mcp_config_path='/opt/frontend/cc-tools.json',
                tool_profile='text_only',
                resident_factory=factory,
                session_id_factory=lambda: next(self.ids),
                session_path_resolver=self._session_path,
            )
        self.assertEqual(ctx.exception.error_code, 'REPLICA_TOOL_CALL_BLOCKED')
        self.assertTrue(all(r.killed for r in self.created))

    def test_seed_session_id_must_not_change(self):
        def bad_seed(**kwargs):
            path = self.root / 'wrong.jsonl'
            path.write_text('seed', encoding='utf-8')
            return NativeSeed('wrong', path, 'x', 2)

        with self.assertRaises(ReplicaContractError) as ctx:
            run_daily_replica_experiment(
                conn=object(), plan=_plan(), a_reproduction_confirmed=True,
                full_system='same-system',
                env={}, cwd='/opt/frontend', claude_home=str(self.root),
                allowed_tools='same-tools', mcp_config_path='/opt/frontend/cc-tools.json',
                tool_profile='text_only',
                resident_factory=self._resident_factory, seed_builder=bad_seed,
                session_id_factory=lambda: next(self.ids),
                session_path_resolver=self._session_path,
            )
        self.assertEqual(ctx.exception.error_code, 'REPLICA_NATIVE_SESSION_MISMATCH')


if __name__ == '__main__':
    unittest.main()
