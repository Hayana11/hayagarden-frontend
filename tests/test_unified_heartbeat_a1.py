import json
import sys
import types
import unittest
from unittest import mock

from flask import Flask

from chat import behavior_authority_b3 as b3


class _FakeResident:
    def __init__(self, events=None):
        self.events = list(events or [
            ('done', ('{"rendered_content":"我在。"}', '', {
                'resident_turn_count': 7,
                'respawn_reason': '',
                'cache_read': 123,
            }, {})),
        ])
        self.sent = []
        self.ensure_calls = 0
        self._model_identity = 'model:frozen'

    def ensure_alive(self, *args, **kwargs):
        self.ensure_calls += 1
        raise AssertionError('UH-A1 shared renderer must never ensure/respawn')

    def send_turn(self, content, **kwargs):
        self.sent.append((content, kwargs))
        yield from self.events


class UnifiedHeartbeatA1Tests(unittest.TestCase):
    def renderer_input(self):
        return b3.RendererInput(
            selected_intent='想主动告诉她自己还在这里',
            selected_action='message',
            content_target='wake_message',
            persona_context='VERY LARGE PERSONA SHOULD NOT BE RESENT',
            continuity_facts='VERY LARGE RELATIONSHIP DUMP SHOULD NOT BE RESENT',
            decision_identity={
                'wake_run_id': 'wake-1',
                'decision_attempt_id': 'attempt-1',
                'primary_drive': 'attachment',
            },
        )

    def test_shared_payload_is_minimal_and_does_not_reinject_private_state(self):
        payload = b3.build_shared_renderer_user_payload(self.renderer_input())
        self.assertIn('normal Wake', payload)
        self.assertIn('想主动告诉她自己还在这里', payload)
        self.assertNotIn('VERY LARGE PERSONA', payload)
        self.assertNotIn('VERY LARGE RELATIONSHIP', payload)
        self.assertNotIn('primary_drive', payload)
        self.assertNotIn('attachment', payload)
        self.assertNotIn('Drive', payload)
        self.assertNotIn('Affect', payload)
        self.assertNotIn('Thought Pool', payload)

    def test_hot_renderer_uses_exact_resident_without_ensure_or_profile_mutation(self):
        resident = _FakeResident()
        lease = {
            'lease_version': 1,
            'turn_id': 'uh-a1-render:attempt-1',
            'turn_mode': 'wake',
            'issued_from': 'default_policy',
            'allowed_capabilities': (),
            'approval_ids': (),
            'task_contract_id': None,
            'issued_at': '2026-08-16T00:00:00Z',
        }
        result = b3.invoke_renderer_cc_hot(
            renderer_input=self.renderer_input(),
            resident=resident,
            turn_lease=lease,
        )
        self.assertEqual(resident.ensure_calls, 0)
        self.assertEqual(len(resident.sent), 1)
        sent_text, sent_kwargs = resident.sent[0]
        self.assertIn('内部 normal Wake 表达轮', sent_text)
        self.assertEqual(sent_kwargs['turn_lease']['turn_id'], lease['turn_id'])
        self.assertTrue(result['shared_resident'])
        self.assertEqual(result['provider'], 'claude_code')
        self.assertEqual(result['cache_info']['cache_read'], 123)
        self.assertEqual(result['cache_info']['resident_turn_count'], 7)
        self.assertEqual(result['cache_info']['respawn_reason'], '')
        self.assertEqual(
            b3.validate_rendered_content(result['text']),
            '我在。',
        )

    def test_shared_renderer_drains_then_rejects_tool_use(self):
        resident = _FakeResident(events=[
            ('tool_use', {'name': 'mcp__home__search_memories'}),
            ('tool_result', {'ok': True}),
            ('done', ('{"rendered_content":"我在。"}', '', {}, {})),
        ])
        with self.assertRaisesRegex(RuntimeError, 'uh_a1_shared_renderer_tool_use'):
            b3.invoke_renderer_cc_hot(
                renderer_input=self.renderer_input(),
                resident=resident,
                turn_lease={'turn_id': 'x'},
            )
        self.assertEqual(len(resident.sent), 1)

    def test_flag_defaults_off_and_preserves_existing_invoker(self):
        with mock.patch.object(b3.config_store, 'get_bool', return_value=False):
            self.assertFalse(b3.unified_normal_wake_enabled())
            called = []

            def invoker(*, renderer_input, timeout_sec):
                called.append((renderer_input, timeout_sec))
                return {
                    'text': '{"rendered_content":"旧路"}',
                    'provider': 'api_relay',
                    'model_identity': 'relay:test',
                }

            result = b3.invoke_renderer(
                renderer_input=self.renderer_input(),
                invoke_fn=invoker,
            )
            self.assertEqual(result['provider'], 'api_relay')
            self.assertEqual(len(called), 1)

    def test_normal_request_can_use_shared_hot_route_with_zero_wait_lock(self):
        app = Flask(__name__)
        resident = _FakeResident()
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_release=lambda result: released.append(result),
        )
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}):
                out = b3._try_invoke_shared_renderer(
                    renderer_input=self.renderer_input(),
                )
        self.assertIsNotNone(out)
        self.assertTrue(out['shared_resident'])
        self.assertEqual(len(resident.sent), 1)
        self.assertEqual(released, [None])
        turn_lease = resident.sent[0][1]['turn_lease']
        self.assertEqual(turn_lease['turn_mode'], 'wake')
        self.assertEqual(turn_lease['issued_from'], 'default_policy')

    def test_other_wake_modes_never_enter_shared_route_in_a1(self):
        app = Flask(__name__)
        with app.test_request_context('/wake', method='POST', json={'mode': 'morning'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True):
                self.assertIsNone(
                    b3._try_invoke_shared_renderer(
                        renderer_input=self.renderer_input(),
                    )
                )


if __name__ == '__main__':
    unittest.main()
