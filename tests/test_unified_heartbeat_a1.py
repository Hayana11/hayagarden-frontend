import json
import sys
import types
import unittest
from unittest import mock

from flask import Flask

from chat import behavior_authority_b3 as b3
from chat import unified_heartbeat_a1 as uh


class _FakeResident:
    def __init__(self, events=None):
        self.events = list(events or [
            ('done', ('{"rendered_content":"我在。"}', '', {
                'resident_turn_count': 7,
                'respawn_reason': '',
                'cache_read': 123,
                'jsonl_usage': {'stream_totals_match': True},
            }, {})),
        ])
        self.sent = []
        self.ensure_calls = 0
        self._model_identity = 'model:frozen'
        self.session_id = 'sid-1'
        self.generation = 4
        self.tool_profile = 'uh_a0'

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

    def binding(self):
        return types.SimpleNamespace(
            context_id=12,
            context_epoch=3,
            resident_generation=2,
            resident_key='chat:12:3:2',
            process_generation=4,
            tool_profile='uh_a0',
        )

    def watermark(self):
        return uh.SharedTranscriptWatermark(
            context_id=12,
            context_epoch=3,
            resident_generation=2,
            resident_key='chat:12:3:2',
            claude_session_id='sid-1',
            transcript_path='/tmp/sid-1.jsonl',
            expected_offset=100,
            process_generation=4,
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

    def test_shared_renderer_rejects_unfinalized_jsonl_tail(self):
        resident = _FakeResident(events=[
            ('done', ('{"rendered_content":"我在。"}', '', {
                'jsonl_usage': {'stream_totals_match': False},
            }, {})),
        ])
        with self.assertRaisesRegex(RuntimeError, 'jsonl_not_final'):
            b3.invoke_renderer_cc_hot(
                renderer_input=self.renderer_input(),
                resident=resident,
                turn_lease={'turn_id': 'delayed-tail'},
            )

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
            _gen_mark_pending_delivery=lambda token: None,
        )
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                 mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(self.watermark(), 'ok')), \
                 mock.patch.object(uh, 'commit_shared_transcript_watermark', return_value={
                     'start_offset': 100, 'end_offset': 180, 'skipped_provider_round': True,
                 }), \
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}):
                out = b3._try_invoke_shared_renderer(
                    renderer_input=self.renderer_input(),
                )
        self.assertIsNotNone(out)
        self.assertTrue(out['shared_resident'])
        self.assertTrue(out['transcript_skip']['skipped_provider_round'])
        self.assertEqual(len(resident.sent), 1)
        self.assertEqual(released, [])
        out['_shared_delivery_fence'].finish(True)
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

    def test_watermark_prepare_refuses_to_hide_existing_mapping_backlog(self):
        resident = _FakeResident()
        registry = {
            'scan_status': 'READY',
            'claude_session_id': 'sid-1',
            'process_generation': 4,
            'transcript_path': '/tmp/sid-1.jsonl',
            'scan_offset': 90,
        }
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
             mock.patch.object(uh, 'get_context_claude_session', return_value=registry), \
             mock.patch.object(uh.os.path, 'getsize', return_value=100):
            watermark, reason = uh.prepare_shared_transcript_watermark(
                resident, db_path='/tmp/fake.db',
            )
        self.assertIsNone(watermark)
        self.assertEqual(reason, 'registry_not_caught_up')

    def test_watermark_commit_cas_skips_only_shared_provider_range(self):
        resident = _FakeResident()
        updated = {'scan_offset': 180, 'last_mapped_message_id': 77}
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
             mock.patch.object(uh.os.path, 'getsize', return_value=180), \
             mock.patch.object(uh, 'cas_advance_scan_offset', return_value=updated) as cas:
            result = uh.commit_shared_transcript_watermark(
                self.watermark(), resident, db_path='/tmp/fake.db',
                jsonl_finality={'stream_totals_match': True},
            )
        self.assertEqual(result['start_offset'], 100)
        self.assertEqual(result['end_offset'], 180)
        self.assertTrue(result['skipped_provider_round'])
        kwargs = cas.call_args.kwargs
        self.assertEqual(kwargs['expected_offset'], 100)
        self.assertEqual(kwargs['new_offset'], 180)
        self.assertIsNone(kwargs['last_mapped_message_id'])
        self.assertEqual(kwargs['scan_status'], 'READY')

    def test_watermark_commit_rejects_missing_jsonl_finality_before_getsize(self):
        resident = _FakeResident()
        with mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
             mock.patch.object(uh.os.path, 'getsize') as getsize:
            with self.assertRaisesRegex(RuntimeError, 'jsonl_finality_missing'):
                uh.commit_shared_transcript_watermark(
                    self.watermark(), resident, db_path='/tmp/fake.db',
                    jsonl_finality=None,
                )
        getsize.assert_not_called()

    def test_delivery_fence_retires_failed_shared_wake_before_release(self):
        import threading

        resident = _FakeResident()
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            _gen_busy=True,
            _gen_pending_delivery=None,
            _gen_cond=threading.Condition(),
            _gen_release=lambda result: released.append(result),
        )

        def mark(token):
            fake_gateway._gen_pending_delivery = token

        fake_gateway._gen_mark_pending_delivery = mark
        with mock.patch('config_store.get_bool', return_value=True), \
             mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
             mock.patch.object(uh.dr, 'close_local_resident_if_bound', return_value=True) as close, \
             mock.patch.dict(sys.modules, {'gateway': fake_gateway}):
            fence = uh.begin_shared_wake_delivery_fence(
                gateway=fake_gateway,
                resident=resident,
            )
            fence.finish(
                False,
                cache_info={
                    'provider': 'claude_code',
                    'source': 'wake',
                    'b3_authority': True,
                },
                window_identity={
                    'context_id': 12,
                    'context_epoch': 3,
                    'resident_generation': 2,
                },
            )
        self.assertTrue(close.called)
        self.assertEqual(released, [None])

    def test_executor_cleanup_wrapper_is_called_for_undelivered_shared_wake(self):
        from wake import executor

        with mock.patch.object(uh, 'retire_shared_resident_after_failed_delivery') as retire:
            executor._retire_uh_a1_after_failed_delivery(
                cache_info={'provider': 'claude_code', 'source': 'wake', 'b3_authority': True},
                window_identity=self.watermark().__dict__,
            )
        self.assertTrue(retire.called)

    def test_watermark_commit_failure_closes_hot_resident_and_never_falls_back(self):
        app = Flask(__name__)
        resident = _FakeResident()
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_release=lambda result: released.append(result),
            _gen_mark_pending_delivery=lambda token: None,
        )
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                 mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(self.watermark(), 'ok')), \
                 mock.patch.object(uh, 'commit_shared_transcript_watermark', side_effect=RuntimeError('cas')), \
                 mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
                 mock.patch.object(uh.dr, 'close_local_resident_if_bound', return_value=True) as close, \
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}):
                with self.assertRaisesRegex(RuntimeError, 'uh_a1_transcript_watermark_commit_failed'):
                    b3._try_invoke_shared_renderer(
                        renderer_input=self.renderer_input(),
                    )
        self.assertEqual(len(resident.sent), 1)
        self.assertTrue(close.called)
        self.assertEqual(released, [None])


if __name__ == '__main__':
    unittest.main()
