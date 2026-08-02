"""Focused 9A-R birth-plan tests; no DB, resident, network, or model calls."""
from __future__ import annotations

import copy
import unittest

from chat.daily_replica_ab import (
    ReplicaContractError,
    VARIANT_NATIVE_ROLES,
    VARIANT_PRODUCTION_PACKAGED,
    build_daily_replica_pair,
)


NL = '\n'


def _formal_formatter(*, assembly, user_content, is_cold, is_respawn):
    assert is_cold is True
    assert is_respawn is False
    pieces = []
    if assembly.get('day_handoff'):
        pieces.append(assembly['day_handoff'])
    carryover = assembly.get('carryover_messages') or []
    if carryover:
        rows = ['【昨日延续对话】']
        for item in carryover:
            label = '用户' if item['role'] == 'user' else '费佳'
            rows.append('[%s] %s' % (label, item['content']))
        pieces.append(NL.join(rows))
    if assembly.get('state'):
        pieces.append(assembly['state'])
    prefix = NL.join(pieces)
    history = assembly.get('current_day_history') or []
    if history:
        rows = []
        for item in history:
            label = '用户' if item['role'] == 'user' else '费佳'
            rows.append('[%s] %s' % (label, item['content']))
        body = (
            '以下是本聊天日内的正式对话记录：' + NL + NL
            + NL.join(rows) + NL + NL
            + '请回复最后一条用户消息。'
        )
        return (prefix + NL + NL if prefix else '') + body + NL + NL + user_content
    return (prefix + NL + NL if prefix else '') + user_content


def _assembly():
    return {
        'day_handoff': '【昨日交接】同一份交接',
        'carryover_messages': [
            {'role': 'user', 'content': '昨日用户', 'message_id': 10},
            {'role': 'assistant', 'content': '昨日助手', 'message_id': 11},
        ],
        'state': '【当前状态】灯关着',
        'current_day_history': [
            {'role': 'user', 'content': '拒不赔偿', 'message_id': 20},
            {'role': 'assistant', 'content': '判决内容', 'message_id': 21},
            {'role': 'user', 'content': '霸权主义', 'message_id': 22},
            {'role': 'assistant', 'content': '制裁内容', 'message_id': 23},
        ],
        'manifest': {
            'handoff_injected': True,
            'carryover_injected': True,
            'state_injected': True,
            'legacy_cold_once_injected': False,
            'auto_recall_injected': False,
            'relationship_context_injected': False,
        },
    }


class DailyReplicaPairTests(unittest.TestCase):
    def _build(self, assembly=None):
        return build_daily_replica_pair(
            assembly=assembly or _assembly(),
            user_content='咬你！',
            static_system_sha256='static-sha',
            persona_sha256='persona-sha',
            provider='claude_code',
            model='claude-opus',
            tool_profile='text_only',
            allowed_tools_sha256='tools-sha',
            mcp_config_sha256='mcp-sha',
            formal_formatter=_formal_formatter,
        )

    def test_only_history_delivery_differs(self):
        plan = self._build()
        self.assertTrue(plan.contract_ok)
        self.assertEqual(plan.manifest['allowed_differences'], ['history_delivery'])
        self.assertEqual(
            plan.production.variant,
            VARIANT_PRODUCTION_PACKAGED,
        )
        self.assertEqual(plan.experiment.variant, VARIANT_NATIVE_ROLES)
        self.assertEqual(plan.production.native_history, ())
        self.assertEqual(len(plan.experiment.native_history), 4)
        self.assertEqual(
            plan.manifest['production']['common_material_sha256'],
            plan.manifest['experiment']['common_material_sha256'],
        )

    def test_non_history_layers_and_current_user_are_identical(self):
        plan = self._build()
        for value in ('【昨日交接】同一份交接', '【昨日延续对话】', '【当前状态】灯关着', '咬你！'):
            self.assertIn(value, plan.production.prompt)
            self.assertIn(value, plan.experiment.prompt)
        self.assertIn('以下是本聊天日内的正式对话记录：', plan.production.prompt)
        self.assertNotIn('以下是本聊天日内的正式对话记录：', plan.experiment.prompt)
        self.assertNotIn('拒不赔偿', plan.experiment.prompt)
        self.assertEqual(plan.experiment.native_history[0].content, '拒不赔偿')

    def test_formal_negative_injection_claims_are_preserved(self):
        plan = self._build()
        formal = plan.manifest['formal_assembly_manifest']
        self.assertFalse(formal['legacy_cold_once_injected'])
        self.assertFalse(formal['auto_recall_injected'])
        self.assertFalse(formal['relationship_context_injected'])

    def test_source_mutation_cannot_change_frozen_plan(self):
        source = _assembly()
        original = copy.deepcopy(source)
        plan = self._build(source)
        source['state'] = '被篡改'
        source['current_day_history'][0]['content'] = '被篡改'
        self.assertNotIn('被篡改', plan.production.prompt)
        self.assertEqual(plan.experiment.native_history[0].content, original['current_day_history'][0]['content'])

    def test_empty_history_fails_closed(self):
        source = _assembly()
        source['current_day_history'] = []
        with self.assertRaises(ReplicaContractError) as ctx:
            self._build(source)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_HISTORY_EMPTY')

    def test_history_must_end_at_assistant_boundary(self):
        source = _assembly()
        source['current_day_history'] = source['current_day_history'][:-1]
        with self.assertRaises(ReplicaContractError) as ctx:
            self._build(source)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_HISTORY_BOUNDARY_INVALID')

    def test_invalid_role_fails_closed(self):
        source = _assembly()
        source['current_day_history'][0]['role'] = 'system'
        with self.assertRaises(ReplicaContractError) as ctx:
            self._build(source)
        self.assertEqual(ctx.exception.error_code, 'REPLICA_HISTORY_ROLE_INVALID')


if __name__ == '__main__':
    unittest.main()
