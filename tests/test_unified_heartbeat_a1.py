import ast
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from chat import behavior_authority_b3 as b3
from chat import unified_heartbeat_a1 as uh


class _FakeResident:
    def __init__(self, events=None, on_send=None):
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
        self._system_text = 'bound-system'
        self.generation = 4
        self.tool_profile = 'uh_a0'
        self._peek_reason = None
        self.peek_calls = []
        self.on_send = on_send

    def _alive(self):
        return True

    def peek_respawn_reason(self, system_text, *, tool_profile):
        self.peek_calls.append((system_text, tool_profile))
        return self._peek_reason

    def ensure_alive(self, *args, **kwargs):
        self.ensure_calls += 1
        raise AssertionError('UH-A1 shared renderer must never ensure/respawn')

    def send_turn(self, content, **kwargs):
        self.sent.append((content, kwargs))
        if self.on_send is not None:
            self.on_send()
        yield from self.events


def _load_generation_lock_functions():
    """Load only gateway's lock functions for cross-platform unit tests."""
    source = (Path(__file__).resolve().parents[1] / 'gateway.py').read_text(
        encoding='utf-8',
    )
    tree = ast.parse(source)
    wanted = {
        '_gen_acquire_or_wait',
        '_gen_release',
        '_gen_mark_pending_delivery',
        'chat_cancel',
    }
    namespace = {
        'time': time,
        'jsonify': lambda value=None, **kwargs: value if value is not None else kwargs,
        'request': types.SimpleNamespace(
            get_json=lambda silent=False: namespace['request_data'],
        ),
        'request_data': {},
        '_gen_cond': __import__('threading').Condition(),
        '_gen_busy': False,
        '_gen_busy_since': 0.0,
        '_gen_last_result': None,
        '_gen_pending_delivery': None,
        '_GEN_ZOMBIE_TTL': 320,
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            node.decorator_list = []
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<gateway-locks>', 'exec'), namespace)
    return namespace


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

    def test_hot_ready_rejects_stale_history_rewrite_without_send_or_ensure(self):
        from chat import daily_context as dc
        from chat import daily_runtime as dr

        resident = _FakeResident()
        resident._peek_reason = 'history_rewrite'
        binding = self.binding()
        owner = {
            'worker_id': dr.WORKER_ID,
            'resident_key': binding.resident_key,
            'process_generation': resident.generation,
        }
        context = {
            'closed_at': None,
            'context_epoch': binding.context_epoch,
            'resident_generation': binding.resident_generation,
        }
        with mock.patch.object(dr, 'get_local_binding', return_value=binding), \\
             mock.patch.object(dc, 'get_daily_context_by_id', return_value=context), \\
             mock.patch.object(dc, 'get_resident_owner', return_value=owner):
            ready, reason = b3._hot_chat_resident_ready(
                resident,
                db_path='/tmp/fake.db',
            )

        self.assertFalse(ready)
        self.assertEqual(reason, 'resident_stale:history_rewrite')
        self.assertEqual(resident.sent, [])
        self.assertEqual(resident.ensure_calls, 0)
        self.assertEqual(resident.peek_calls, [('bound-system', 'uh_a0')])

    def test_final_guarded_probe_closes_probe_to_stdin_race_without_retirement(self):
        import threading

        app = Flask(__name__)
        resident = _FakeResident()
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_pending_delivery=None,
            _gen_busy=True,
            _gen_cond=threading.Condition(),
        )
        def mark(token):
            fake_gateway._gen_pending_delivery = token
        def release(result, *, expected_pending_token=None):
            if expected_pending_token is not None and (
                fake_gateway._gen_pending_delivery is not expected_pending_token
            ):
                return False
            fake_gateway._gen_pending_delivery = None
            fake_gateway._gen_busy = False
            released.append(result)
            return True
        fake_gateway._gen_mark_pending_delivery = mark
        fake_gateway._gen_release = release
        fallback = {
            'text': '{"rendered_content":"旧路"}',
            'provider': 'api_relay',
            'model_identity': 'relay:test',
        }
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(
                     b3,
                     '_hot_chat_resident_ready',
                     side_effect=[
                         (True, 'ok'),
                         (True, 'ok'),
                         (False, 'resident_stale:history_rewrite'),
                     ],
                 ), \
                 mock.patch.object(
                     uh,
                     'prepare_shared_transcript_watermark',
                     return_value=(self.watermark(), 'ok'),
                 ), \
                 mock.patch.object(uh.dr, 'close_local_resident_if_bound', return_value=True) as close, \
                 mock.patch('config_store.get_bool', return_value=True), \
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}), \
                 mock.patch.object(b3, 'invoke_renderer_relay', return_value=fallback) as relay:
                result = b3.invoke_renderer(renderer_input=self.renderer_input())

        self.assertEqual(result['provider'], 'api_relay')
        self.assertEqual(len(resident.sent), 0)
        self.assertEqual(resident.ensure_calls, 0)
        close.assert_not_called()
        relay.assert_called_once()
        self.assertFalse(fake_gateway._gen_busy)
        self.assertEqual(released, [None])

    def test_shared_send_turn_is_wrapped_by_history_rewrite_guard(self):
        from chat import cc_history_rewrite

        resident = _FakeResident()
        with mock.patch.object(
            cc_history_rewrite,
            'guard_cc_generation',
            wraps=cc_history_rewrite.guard_cc_generation,
        ) as guard:
            b3.invoke_renderer_cc_hot(
                renderer_input=self.renderer_input(),
                resident=resident,
                turn_lease={'turn_id': 'guarded'},
            )
        guard.assert_called_once()

    def test_shared_renderer_respawn_reason_fails_closed_and_never_falls_back(self):
        import threading

        app = Flask(__name__)
        resident = _FakeResident(events=[
            ('done', ('{"rendered_content":"我在。"}', '', {
                'respawn_reason': 'history_rewrite',
                'jsonl_usage': {'stream_totals_match': True},
            }, {})),
        ])
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_pending_delivery=None,
            _gen_busy=True,
            _gen_cond=threading.Condition(),
        )
        def mark(token):
            fake_gateway._gen_pending_delivery = token
        def release(result, *, expected_pending_token=None):
            if expected_pending_token is not None and (
                fake_gateway._gen_pending_delivery is not expected_pending_token
            ):
                return False
            fake_gateway._gen_pending_delivery = None
            released.append(result)
            return True
        fake_gateway._gen_mark_pending_delivery = mark
        fake_gateway._gen_release = release
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \\
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \\
                 mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(self.watermark(), 'ok')), \\
                 mock.patch('config_store.get_bool', return_value=True), \\
                 mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \\
                 mock.patch.object(uh.dr, 'close_local_resident_if_bound', return_value=True) as close, \\
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}), \\
                 mock.patch.object(b3, 'invoke_renderer_relay') as relay:
                with self.assertRaisesRegex(
                    RuntimeError,
                    'uh_a1_shared_renderer_respawn_reason:history_rewrite',
                ):
                    b3.invoke_renderer(renderer_input=self.renderer_input())
        self.assertEqual(len(resident.sent), 1)
        self.assertTrue(close.called)
        self.assertEqual(released, [None])
        relay.assert_not_called()

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
        marker_seen_during_send = []
        resident = _FakeResident(
            on_send=lambda: marker_seen_during_send.append(
                fake_gateway._gen_pending_delivery is not None
            ),
        )
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_pending_delivery=None,
        )
        def mark(token):
            fake_gateway._gen_pending_delivery = token
        def release(result, *, expected_pending_token=None):
            if expected_pending_token is not None and (
                fake_gateway._gen_pending_delivery is not expected_pending_token
            ):
                return False
            fake_gateway._gen_pending_delivery = None
            released.append(result)
            return True
        fake_gateway._gen_release = release
        fake_gateway._gen_mark_pending_delivery = mark
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
        self.assertEqual(marker_seen_during_send, [True])
        self.assertEqual(released, [])
        out['_shared_delivery_fence'].finish(True)
        self.assertEqual(released, [None])
        self.assertIsNone(fake_gateway._gen_pending_delivery)
        turn_lease = resident.sent[0][1]['turn_lease']
        self.assertEqual(turn_lease['turn_mode'], 'wake')
        self.assertEqual(turn_lease['issued_from'], 'default_policy')

    def test_force_cancel_preserves_live_shared_wake_under_zombie_ttl(self):
        locks = _load_generation_lock_functions()
        token = object()
        locks.update({
            '_gen_busy': True,
            '_gen_busy_since': time.time() - 30,
            '_gen_pending_delivery': token,
            'request_data': {'force': True},
        })
        result = locks['chat_cancel']()
        self.assertTrue(result['skipped'])
        self.assertTrue(result['shared_wake'])
        self.assertTrue(locks['_gen_busy'])
        self.assertIs(locks['_gen_pending_delivery'], token)

    def test_shared_wake_zombie_escape_releases_and_stale_finish_cannot_kill_new_chat(self):
        locks = _load_generation_lock_functions()
        old_token = object()
        locks.update({
            '_gen_busy': True,
            '_gen_busy_since': time.time() - 321,
            '_gen_pending_delivery': old_token,
            'request_data': {'force': True},
        })
        result = locks['chat_cancel']()
        self.assertTrue(result['ok'])
        self.assertFalse(locks['_gen_busy'])
        self.assertIsNone(locks['_gen_pending_delivery'])

        mode, _ = locks['_gen_acquire_or_wait'](wait_timeout=0)
        self.assertEqual(mode, 'own')
        self.assertFalse(
            locks['_gen_release'](
                None,
                expected_pending_token=old_token,
            )
        )
        self.assertTrue(locks['_gen_busy'])

    def test_force_cancel_non_shared_generation_keeps_original_behavior(self):
        locks = _load_generation_lock_functions()
        locks.update({
            '_gen_busy': True,
            '_gen_busy_since': time.time() - 30,
            '_gen_pending_delivery': None,
            'request_data': {'force': True},
        })
        result = locks['chat_cancel']()
        self.assertTrue(result['ok'])
        self.assertFalse(result.get('skipped', False))
        self.assertFalse(locks['_gen_busy'])

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
             mock.patch.object(uh, '_read_complete_shared_transcript_end_offset', return_value=180), \
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

    def test_watermark_reader_rejects_usage_only_tail_until_canonical_assistant_arrives(self):
        user = {
            'type': 'user',
            'uuid': 'wake-user-1',
            'sessionId': 'sid-1',
            'message': {'role': 'user', 'content': 'wake'},
        }
        usage_observation = {
            'type': 'assistant',
            'requestId': 'request-1',
            'sessionId': 'sid-1',
            'message': {
                'role': 'assistant',
                'usage': {
                    'input_tokens': 1,
                    'output_tokens': 1,
                    'cache_read_input_tokens': 0,
                    'cache_creation_input_tokens': 0,
                },
            },
        }
        canonical_assistant = {
            'type': 'assistant',
            'uuid': 'wake-assistant-1',
            'parentUuid': 'wake-user-1',
            'sessionId': 'sid-1',
            'message': {
                'role': 'assistant',
                'content': [{'type': 'text', 'text': '我在。'}],
            },
        }
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as handle:
            path = handle.name
            handle.write((json.dumps(user, ensure_ascii=False) + '\n').encode('utf-8'))
            handle.write((json.dumps(usage_observation, ensure_ascii=False) + '\n').encode('utf-8'))
        try:
            watermark = uh.SharedTranscriptWatermark(
                context_id=12,
                context_epoch=3,
                resident_generation=2,
                resident_key='chat:12:3:2',
                claude_session_id='sid-1',
                transcript_path=path,
                expected_offset=0,
                process_generation=4,
            )
            updated = {'scan_offset': 0, 'last_mapped_message_id': 77}
            with mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
                 mock.patch.object(uh, 'cas_advance_scan_offset', return_value=updated) as cas, \
                 mock.patch.object(uh.time, 'sleep'):
                with self.assertRaisesRegex(RuntimeError, 'round_incomplete'):
                    uh.commit_shared_transcript_watermark(
                        watermark,
                        _FakeResident(),
                        db_path='/tmp/fake.db',
                        jsonl_finality={'stream_totals_match': True},
                    )
                cas.assert_not_called()

                with open(path, 'ab') as handle:
                    handle.write((json.dumps(canonical_assistant, ensure_ascii=False) + '\n').encode('utf-8'))
                updated['scan_offset'] = os.path.getsize(path)
                result = uh.commit_shared_transcript_watermark(
                    watermark,
                    _FakeResident(),
                    db_path='/tmp/fake.db',
                    jsonl_finality={'stream_totals_match': True},
                )
            self.assertEqual(result['end_offset'], os.path.getsize(path))
            self.assertEqual(cas.call_count, 1)
        finally:
            os.unlink(path)

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
            _gen_release=lambda result, **kwargs: released.append(result),
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
        import threading

        app = Flask(__name__)
        resident = _FakeResident()
        released = []
        fake_gateway = types.SimpleNamespace(
            _CC_RESIDENT=resident,
            DB_PATH='/tmp/fake.db',
            _gen_acquire_or_wait=lambda wait_timeout=0: ('own', None),
            _gen_pending_delivery=None,
            _gen_busy=True,
            _gen_cond=threading.Condition(),
        )
        def mark(token):
            fake_gateway._gen_pending_delivery = token
        def release(result, *, expected_pending_token=None):
            if expected_pending_token is not None and (
                fake_gateway._gen_pending_delivery is not expected_pending_token
            ):
                return False
            fake_gateway._gen_pending_delivery = None
            released.append(result)
            return True
        fake_gateway._gen_mark_pending_delivery = mark
        fake_gateway._gen_release = release
        with app.test_request_context('/wake', method='POST', json={'mode': 'normal'}):
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                 mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(self.watermark(), 'ok')), \
                 mock.patch.object(uh, 'commit_shared_transcript_watermark', side_effect=RuntimeError('cas')), \
                 mock.patch('config_store.get_bool', return_value=True), \
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
