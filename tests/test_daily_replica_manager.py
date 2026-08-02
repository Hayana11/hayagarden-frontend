"""9A-R owner decision gate and snapshot lifecycle tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from chat.daily_replica_ab import ReplicaContractError, build_daily_replica_pair
from chat.daily_replica_manager import DailyReplicaManager
from chat.daily_replica_runner import ReplicaExecutionResult, ReplicaVariantResult
from chat.daily_replica_session_start import prepare_replica_session_start_isolation


def _formatter(*, assembly, user_content, is_cold, is_respawn):
    history = assembly.get('current_day_history') or []
    return '|'.join(x['content'] for x in history) + '\n' + user_content


def _plan():
    return build_daily_replica_pair(
        assembly={
            'current_day_history': [
                {'role': 'user', 'content': 'u1', 'message_id': 1},
                {'role': 'assistant', 'content': 'a1', 'message_id': 2},
            ],
            'manifest': {},
        },
        user_content='咬你！', static_system_sha256='s', persona_sha256='p',
        provider='claude_code', model='opus', tool_profile='text_only',
        allowed_tools_sha256='t', mcp_config_sha256='m',
        formal_formatter=_formatter,
    )


class FakeSnapshot:
    def __init__(self, root):
        self.temp_root = root
        self.db_path = root / 'snapshot.sqlite3'
        self.db_path.write_bytes(b'')
        self.plan = _plan()
        self.manifest = {'snapshot_contract_ok': True}
        self.session_start = prepare_replica_session_start_isolation(root / 'session-root')
        self.closed = False

    def close(self):
        self.closed = True


def _result(variant):
    return ReplicaExecutionResult(
        result=ReplicaVariantResult(
            variant=variant, content='reply-' + variant,
            thinking='thinking-' + variant, usage={},
        ),
        manifest={'runner_contract_ok': True, 'variant_run': variant},
    )


class DailyReplicaManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.snapshots = []
        self.a_calls = []
        self.b_calls = []

        def snapshot_factory(**kwargs):
            snap = FakeSnapshot(self.root)
            self.snapshots.append((snap, kwargs))
            return snap

        def run_a(**kwargs):
            self.a_calls.append(kwargs)
            return _result('production_packaged')

        def run_b(**kwargs):
            self.b_calls.append(kwargs)
            return _result('native_roles')

        self.manager = DailyReplicaManager(
            source_db_path='/formal/memories.db', cwd='/opt/cc-gw',
            claude_home=str(self.root), allowed_tools='legacy-list',
            mcp_config_path=str(self.root / 'cc-tools.json'), cc_token='token',
            get_provider=lambda: 'claude_code', get_model=lambda: 'opus',
            build_static_parts=lambda: {'full_system': 'system', 'persona': 'persona'},
            snapshot_factory=snapshot_factory, run_a=run_a, run_b=run_b,
            id_factory=lambda: 'exp-1', clock=lambda: 1.0,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stops_and_waits_without_running_b(self):
        response = self.manager.start_a(user_message_id=10)
        self.assertEqual(response['variant'], 'A')
        self.assertEqual(response['next'], 'CONFIRM_A_REPRODUCED_OR_CLOSE')
        self.assertEqual(len(self.a_calls), 1)
        self.assertEqual(self.b_calls, [])
        self.assertEqual(self.a_calls[0]['tool_profile'], 'text_only')
        self.assertFalse(self.snapshots[0][0].closed)

    def test_false_a_decision_keeps_b_locked_and_snapshot_alive(self):
        self.manager.start_a(user_message_id=10)
        with self.assertRaises(ReplicaContractError) as ctx:
            self.manager.run_experiment_b(
                experiment_id='exp-1', a_reproduction_confirmed=False,
            )
        self.assertEqual(ctx.exception.error_code, 'REPLICA_A_GATE_REQUIRED')
        self.assertEqual(self.b_calls, [])
        self.assertFalse(self.snapshots[0][0].closed)

    def test_confirmed_a_runs_b_once_then_cleans_snapshot(self):
        self.manager.start_a(user_message_id=10)
        response = self.manager.run_experiment_b(
            experiment_id='exp-1', a_reproduction_confirmed=True,
        )
        self.assertEqual(response['variant'], 'B')
        self.assertEqual(len(self.b_calls), 1)
        self.assertTrue(self.b_calls[0]['a_reproduction_confirmed'])
        self.assertEqual(self.b_calls[0]['tool_profile'], 'text_only')
        self.assertTrue(self.snapshots[0][0].closed)

    def test_second_a_is_rejected_until_first_is_closed(self):
        self.manager.start_a(user_message_id=10)
        with self.assertRaises(ReplicaContractError) as ctx:
            self.manager.start_a(user_message_id=10)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_ALREADY_ACTIVE')
        self.manager.close(experiment_id='exp-1')
        self.assertTrue(self.snapshots[0][0].closed)


if __name__ == '__main__':
    unittest.main()
