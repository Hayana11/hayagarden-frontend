import ast
import contextlib
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

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)
loaded_gateway = sys.modules.get('gateway')
if loaded_gateway is not None:
    loaded_path = Path(getattr(loaded_gateway, '__file__', '')).resolve()
    if loaded_path != Path(ROOT, 'gateway.py').resolve():
        del sys.modules['gateway']

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
        with mock.patch.object(dr, 'get_local_binding', return_value=binding), \
             mock.patch.object(dc, 'get_daily_context_by_id', return_value=context), \
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

        self.assertTrue(result['shared_unavailable'])
        self.assertEqual(
            result['shared_unavailable_reason'],
            'resident_stale:history_rewrite',
        )
        self.assertEqual(result['text'], '')
        self.assertEqual(len(resident.sent), 0)
        self.assertEqual(resident.ensure_calls, 0)
        close.assert_not_called()
        relay.assert_not_called()
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
            with mock.patch.object(b3, 'unified_normal_wake_enabled', return_value=True), \
                 mock.patch.object(b3, '_hot_chat_resident_ready', return_value=(True, 'ok')), \
                 mock.patch.object(uh, 'prepare_shared_transcript_watermark', return_value=(self.watermark(), 'ok')), \
                 mock.patch('config_store.get_bool', return_value=True), \
                 mock.patch.object(uh.dr, 'get_local_binding', return_value=self.binding()), \
                 mock.patch.object(uh.dr, 'close_local_resident_if_bound', return_value=True) as close, \
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}), \
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
                 mock.patch.dict(sys.modules, {'gateway': fake_gateway}), \
                 mock.patch.object(b3, 'invoke_renderer_relay') as relay:
                out = b3.invoke_renderer(renderer_input=self.renderer_input())
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
        relay.assert_not_called()

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


    def test_current_main_wake_multitool_late_flush_success_keeps_resident_reusable(self):
        import gateway
        from cc_resident import ResidentSession
        from chat import cc_history_rewrite
        from chat import display_thinking
        from tools import cc_jsonl_usage as replay
        from tools import lease_signer

        class IntegrationResident(_FakeResident):
            def __init__(self):
                super().__init__(events=[])
                self._model_identity = 'model:integration'
                self.followup_calls = 0
                self.jsonl_helper = ResidentSession(
                    '/tmp/cc-test', '', '/tmp/mcp.json',
                )
                self.jsonl_helper._session_id = 'wake-integration'
                self._replay_calls = 0

            def send_turn(self, content, **kwargs):
                self.followup_calls += 1
                yield ('done', ('follow-up', '', {
                    'jsonl_usage': {'stream_totals_match': True},
                }, {}))

        resident = IntegrationResident()
        replay_partial = replay.replay_jsonl_lines([
            json.dumps({
                'type': 'assistant',
                'requestId': 'req-1',
                'message': {'usage': {
                    'input_tokens': 1,
                    'output_tokens': 2,
                    'cache_creation_input_tokens': 10,
                }},
            }),
        ])
        replay_complete = replay.replay_jsonl_lines([
            json.dumps({
                'type': 'assistant',
                'requestId': 'req-1',
                'message': {'usage': {
                    'input_tokens': 1,
                    'output_tokens': 2,
                    'cache_creation_input_tokens': 10,
                }},
            }),
            json.dumps({
                'type': 'assistant',
                'requestId': 'req-2',
                'message': {'usage': {
                    'input_tokens': 1,
                    'output_tokens': 2,
                    'cache_creation_input_tokens': 10,
                }},
            }),
            json.dumps({
                'type': 'assistant',
                'requestId': 'req-3',
                'message': {'usage': {
                    'input_tokens': 1,
                    'output_tokens': 2,
                    'cache_creation_input_tokens': 10,
                }},
            }),
        ])
        stream_usage = {
            'input_tokens': 3,
            'output_tokens': 6,
            'cache_read': 0,
            'cache_creation': 30,
            'rounds': [
                {'index': 1, 'input_tokens': 1, 'output_tokens': 2,
                 'cache_read': 0, 'cache_creation': 10},
                {'index': 2, 'input_tokens': 1, 'output_tokens': 2,
                 'cache_read': 0, 'cache_creation': 10},
                {'index': 3, 'input_tokens': 1, 'output_tokens': 2,
                 'cache_read': 0, 'cache_creation': 10},
            ],
        }

        commit_calls = []
        release_calls = []
        def main_stream(*args, **kwargs):
            self.assertEqual(
                kwargs.get('jsonl_finality_profile'),
                'unified_normal_wake',
            )
            yield ('tool_use', {
                'id': 'tool-1', 'name': 'lookup', 'args': {'q': 'one'},
            })
            yield ('tool_result', {
                'tool_use_id': 'tool-1', 'result': 'one', 'is_error': False,
            })
            yield ('tool_use', {
                'id': 'tool-2', 'name': 'lookup', 'args': {'q': 'two'},
            })
            yield ('tool_result', {
                'tool_use_id': 'tool-2', 'result': 'two', 'is_error': False,
            })
            yield ('tool_use', {
                'id': 'tool-3', 'name': 'lookup', 'args': {'q': 'three'},
            })
            yield ('tool_result', {
                'tool_use_id': 'tool-3', 'result': 'three', 'is_error': False,
            })
            with (
                mock.patch.object(
                    replay,
                    'snapshot_session_jsonl',
                    return_value={'path': '/tmp/wake.jsonl', 'offset': 40},
                ),
                mock.patch.object(
                    replay,
                    'replay_session_jsonl',
                    side_effect=[replay_partial, replay_partial, replay_complete],
                ) as replay_mock,
                mock.patch('cc_resident.time.sleep'),
            ):
                attached = resident.jsonl_helper._attach_jsonl_usage_with_retry(
                    stream_usage,
                    finality_profile='unified_normal_wake',
                )
            resident._replay_calls = replay_mock.call_count
            yield ('text', 'wake text')
            yield ('done', ('wake text', '', attached, {}))

        with (
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch.object(
                gateway, '_gen_acquire_or_wait', return_value=('own', None),
            ),
            mock.patch.object(
                gateway, '_gen_mark_pending_delivery',
                side_effect=lambda token: None,
            ),
            mock.patch.object(
                gateway, '_gen_release',
                side_effect=lambda *args, **kwargs: release_calls.append(True),
            ),
            mock.patch.object(
                uh, 'retire_shared_resident_after_failed_delivery',
            ) as retire,
            mock.patch.object(
                gateway, '_cc_resident_stream_gen', side_effect=main_stream,
            ),
            mock.patch.object(
                b3, '_hot_chat_resident_ready', return_value=(True, 'ok'),
            ),
            mock.patch.object(
                uh, 'prepare_shared_transcript_watermark',
                return_value=(self.watermark(), 'ok'),
            ),
            mock.patch.object(
                uh, 'commit_shared_transcript_watermark',
                side_effect=lambda *args, **kwargs: (
                    commit_calls.append(kwargs.get('jsonl_finality'))
                    or {'committed': True}
                ),
            ),
            mock.patch.object(
                cc_history_rewrite, 'guard_cc_generation',
                side_effect=lambda events: events,
            ),
            mock.patch.object(
                display_thinking, 'get_display_thinking_snapshot',
                return_value=(False, ''),
            ),
            mock.patch.object(
                lease_signer, 'issue_turn_lease', return_value={'turn_id': 'wake'},
            ),
        ):
            result = gateway._run_unified_normal_main_chat_turn(
                wake_run_id='wake-integration',
                now=__import__('datetime').datetime.now(),
                t2_hours=1.0,
                t_hours=2.0,
            )
            fence = result['_shared_delivery_fence']
            fence.finish(True, cache_info=result['cache_info'])
            self.assertEqual(len(release_calls), 1)
            retire.assert_not_called()

        self.assertEqual(resident._replay_calls, 3)
        self.assertTrue(result['cache_info']['jsonl_usage']['stream_totals_match'])
        self.assertEqual(len(commit_calls), 1)
        self.assertEqual(resident.followup_calls, 0)
        list(resident.send_turn('follow-up'))
        self.assertEqual(resident.followup_calls, 1)

    def test_current_main_wake_never_final_failure_finishes_delivery_false(self):
        import gateway
        from chat import cc_history_rewrite
        from chat import display_thinking
        from tools import lease_signer

        resident = _FakeResident(events=[
            ('tool_use', {'id': 'tool-1', 'name': 'lookup', 'args': {}}),
            ('tool_result', {
                'tool_use_id': 'tool-1', 'result': 'done', 'is_error': False,
            }),
            ('done', ('wake text', '', {
                'jsonl_usage': {
                    'stream_totals_match': False,
                    'finality_state': 'FINALITY_PENDING',
                },
            }, {})),
        ])
        commit = mock.Mock()
        def main_stream(*args, **kwargs):
            yield from resident.events

        with (
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch.object(
                gateway, '_gen_acquire_or_wait', return_value=('own', None),
            ),
            mock.patch.object(gateway, '_gen_release'),
            mock.patch.object(
                gateway, '_cc_resident_stream_gen', side_effect=main_stream,
            ),
            mock.patch.object(
                b3, '_hot_chat_resident_ready', return_value=(True, 'ok'),
            ),
            mock.patch.object(
                uh, 'prepare_shared_transcript_watermark',
                return_value=(self.watermark(), 'ok'),
            ),
            mock.patch.object(
                gateway, '_gen_mark_pending_delivery',
                side_effect=lambda token: None,
            ),
            mock.patch.object(gateway, '_gen_release'),
            mock.patch.object(
                uh, 'retire_shared_resident_after_failed_delivery',
            ) as retire,
            mock.patch.object(
                uh, 'commit_shared_transcript_watermark', commit,
            ),
            mock.patch.object(
                cc_history_rewrite, 'guard_cc_generation',
                side_effect=lambda events: events,
            ),
            mock.patch.object(
                display_thinking, 'get_display_thinking_snapshot',
                return_value=(False, ''),
            ),
            mock.patch.object(
                lease_signer, 'issue_turn_lease', return_value={'turn_id': 'wake'},
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, 'normal_wake_main_chat_jsonl_not_final',
            ):
                gateway._run_unified_normal_main_chat_turn(
                    wake_run_id='wake-never-final',
                    now=__import__('datetime').datetime.now(),
                    t2_hours=1.0,
                    t_hours=2.0,
                )

        commit.assert_not_called()
        retire.assert_called_once()


    def _import_worktree_gateway(self):
        root = str(Path(__file__).resolve().parents[1])
        if root in sys.path:
            sys.path.remove(root)
        sys.path.insert(0, root)
        for module_name in ('gateway', 'cc_resident'):
            loaded = sys.modules.get(module_name)
            if loaded is None:
                continue
            loaded_path = Path(getattr(loaded, '__file__', '')).resolve()
            if loaded_path != Path(root, f'{module_name}.py').resolve():
                del sys.modules[module_name]
        import gateway
        return gateway

    def _run_wake_live_trace_fixture(self, *, finality_match):
        gateway = self._import_worktree_gateway()
        from chat import cc_history_rewrite
        from chat import display_thinking
        from tools import lease_signer

        resident = _FakeResident()
        finality = {
            'stream_totals': {
                'input_tokens': 10,
                'output_tokens': 20,
                'cache_read': 30,
                'cache_creation': 40,
            },
            'jsonl_totals': {
                'input_tokens': 10,
                'output_tokens': 20,
                'cache_read': 30 if finality_match else 31,
                'cache_creation': 40,
            },
            'stream_totals_match': finality_match,
            'request_count': 2,
            'duplicate_rows_ignored': 1,
            'conflicting_duplicate_rows': 0,
            'finality_state': 'FINAL' if finality_match else 'MISMATCH',
        }

        def main_stream(_messages, **kwargs):
            self.assertTrue(callable(kwargs.get('on_stdin_begin')))
            self.assertTrue(callable(kwargs.get('on_stdin_flushed')))
            self.assertTrue(callable(kwargs.get('on_provider_done')))
            kwargs['on_stdin_begin']()
            kwargs['on_stdin_flushed']()
            yield ('tool_use', {
                'id': 'tool-1',
                'name': 'get_location',
                'args': {'private': 'PRIVATE-ARGS'},
                'lease_decision': 'ALLOW',
            })
            yield ('tool_result', {
                'tool_use_id': 'tool-1',
                'result': 'PRIVATE-RESULT',
                'is_error': False,
            })
            kwargs['on_provider_done']({
                'num_rounds': 2,
                'request_count': 2,
                '_obs_result_seen': True,
                '_obs_terminal_reason': 'result',
            })
            yield ('done', (
                'wake text',
                '',
                {
                    'num_rounds': 2,
                    'request_count': 2,
                    'jsonl_usage': finality,
                },
                {},
            ))

        commit_result = {
            'context_id': 12,
            'resident_generation': 2,
            'start_offset': 100,
            'end_offset': 250,
            'skipped_provider_round': True,
        }
        patches = (
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch.object(
                gateway, '_gen_acquire_or_wait', return_value=('own', None),
            ),
            mock.patch.object(
                gateway, '_gen_mark_pending_delivery',
                side_effect=lambda token: None,
            ),
            mock.patch.object(gateway, '_gen_release'),
            mock.patch.object(
                gateway, '_cc_resident_stream_gen', side_effect=main_stream,
            ),
            mock.patch.object(
                b3, '_hot_chat_resident_ready', return_value=(True, 'ok'),
            ),
            mock.patch.object(
                uh, 'prepare_shared_transcript_watermark',
                return_value=(self.watermark(), 'ok'),
            ),
            mock.patch.object(
                uh, 'commit_shared_transcript_watermark',
                return_value=commit_result,
            ),
            mock.patch.object(
                uh, 'retire_shared_resident_after_failed_delivery',
                return_value=False,
            ),
            mock.patch.object(
                cc_history_rewrite, 'guard_cc_generation',
                side_effect=lambda events: events,
            ),
            mock.patch.object(
                display_thinking, 'get_display_thinking_snapshot',
                return_value=(False, ''),
            ),
            mock.patch.object(
                lease_signer, 'issue_turn_lease',
                return_value={'turn_id': 'wake-live'},
            ),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertLogs(gateway.app.logger.name, level='INFO') as captured:
                try:
                    result = gateway._run_unified_normal_main_chat_turn(
                        wake_run_id='wake-live-test',
                        now=__import__('datetime').datetime.now(),
                        t2_hours=1.0,
                        t_hours=2.0,
                    )
                except Exception as exc:
                    result = exc
                else:
                    result['_shared_delivery_fence'].finish(
                        True,
                        cache_info=result['cache_info'],
                    )
        return result, captured.output

    def test_wake_live_trace_shared_unavailable_has_stage_and_reason(self):
        gateway = self._import_worktree_gateway()
        from chat import display_thinking

        resident = _FakeResident()
        with (
            mock.patch.object(gateway, '_CC_RESIDENT', resident),
            mock.patch.object(
                b3, '_hot_chat_resident_ready',
                return_value=(False, 'resident_stale:soft_context'),
            ),
            mock.patch.object(
                display_thinking, 'get_display_thinking_snapshot',
                return_value=(False, ''),
            ),
        ):
            with self.assertLogs(gateway.app.logger.name, level='INFO') as captured:
                with self.assertRaises(b3.UnifiedNormalWakeSharedUnavailable):
                    gateway._run_unified_normal_main_chat_turn(
                        wake_run_id='wake-live-unavailable',
                        now=__import__('datetime').datetime.now(),
                        t2_hours=1.0,
                        t_hours=2.0,
                    )

        events = [
            json.loads(line.split('[WAKE-LIVE] ', 1)[1])
            for line in captured.output
            if '[WAKE-LIVE] ' in line
        ]
        self.assertEqual(
            [event['stage'] for event in events],
            ['START', 'RESIDENT_CHECK_1', 'FAILED'],
        )
        self.assertEqual(events[1]['ready'], False)
        self.assertEqual(events[1]['reason'], 'resident_stale:soft_context')
        self.assertEqual(events[2]['error_code'], 'NORMAL_WAKE_SHARED_UNAVAILABLE_SKIP')

    def test_wake_live_trace_success_records_safe_tool_and_finality(self):
        result, captured = self._run_wake_live_trace_fixture(
            finality_match=True,
        )
        self.assertIsInstance(result, dict)
        self.assertEqual(result['text'], 'wake text')
        self.assertEqual(result['tool_calls'][0]['result'], 'PRIVATE-RESULT')
        self.assertNotIn('PRIVATE-RESULT', '\n'.join(captured))

        events = [
            json.loads(line.split('[WAKE-LIVE] ', 1)[1])
            for line in captured
            if '[WAKE-LIVE] ' in line
        ]
        stages = [event['stage'] for event in events]
        self.assertEqual(
            stages,
            [
                'START', 'RESIDENT_CHECK_1', 'GEN_LOCK_ACQUIRED',
                'RESIDENT_CHECK_2', 'WATERMARK_PREPARED',
                'DELIVERY_FENCE_STARTED', 'STDIN_BEGIN', 'STDIN_FLUSHED',
                'TOOL_USE', 'TOOL_RESULT', 'PROVIDER_DONE',
                'JSONL_FINALITY', 'WATERMARK_COMMIT',
            ],
        )
        tool_use = next(event for event in events if event['stage'] == 'TOOL_USE')
        tool_result = next(event for event in events if event['stage'] == 'TOOL_RESULT')
        self.assertEqual(tool_use['tool_name'], 'get_location')
        self.assertEqual(tool_use['tool_decision'], 'ALLOW')
        self.assertEqual(tool_result['tool_name'], 'get_location')
        self.assertTrue(tool_result['tool_success'])
        self.assertEqual(
            next(event for event in events if event['stage'] == 'JSONL_FINALITY')[
                'stream_totals'
            ],
            {
                'input_tokens': 10,
                'output_tokens': 20,
                'cache_read': 30,
                'cache_creation': 40,
            },
        )

    def test_wake_live_trace_jsonl_mismatch_records_both_totals(self):
        result, captured = self._run_wake_live_trace_fixture(
            finality_match=False,
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn('normal_wake_main_chat_jsonl_not_final', str(result))
        events = [
            json.loads(line.split('[WAKE-LIVE] ', 1)[1])
            for line in captured
            if '[WAKE-LIVE] ' in line
        ]
        finality = next(
            event for event in events if event['stage'] == 'JSONL_FINALITY'
        )
        self.assertFalse(finality['stream_totals_match'])
        self.assertEqual(finality['stream_totals']['cache_read'], 30)
        self.assertEqual(finality['jsonl_totals']['cache_read'], 31)
        self.assertEqual(finality['finality_state'], 'MISMATCH')
        self.assertTrue(
            any(event['stage'] == 'FAILED' for event in events)
        )

    def test_wake_live_route_success_emits_success(self):
        gateway = self._import_worktree_gateway()
        from chat import interaction_state
        from wake import executor

        class Fence:
            def __init__(self):
                self.finished = []

            def finish(self, delivered, **kwargs):
                self.finished.append(bool(delivered))

        clock = types.SimpleNamespace(
            reliable=True,
            user_idle_hours=1.0,
            effective_idle_hours=1.0,
        )
        main_turn = {
            'text': 'wake text',
            'thinking': '',
            'tool_calls': [],
            'model_identity': 'claude-code:test',
            'cache_info': {
                'provider': 'claude_code',
                'source': 'wake',
                'b3_authority': True,
                'unified_chat_resident': True,
                'unified_main_chat_proactive': True,
            },
            '_shared_delivery_fence': Fence(),
        }
        persisted_ids = {
            'wake_log_id': 701,
            'assistant_message_id': 9001,
        }
        runner = types.SimpleNamespace(
            select_wake_provider=lambda _mode: 'claude_code',
        )
        data = {'mode': 'normal', 'wake_run_id': 'wake-route-success'}

        with gateway.app.test_request_context(
            '/wake', method='POST', json=data,
        ):
            with (
                mock.patch.object(
                    interaction_state, 'read_interaction_clock',
                    return_value=clock,
                ),
                mock.patch.object(
                    interaction_state, 'wake_guard_reason',
                    return_value=None,
                ),
                mock.patch.object(
                    gateway, '_chat_is_generating', return_value=False,
                ),
                mock.patch.object(
                    gateway, '_ensure_wake_runners', return_value=runner,
                ),
                mock.patch.object(
                    gateway, '_wake_run_id_seen', return_value=False,
                ),
                mock.patch.object(
                    gateway, '_run_unified_normal_main_chat_turn',
                    return_value=main_turn,
                ),
                mock.patch.object(
                    executor, 'execute', return_value={'delivered': True},
                ),
                mock.patch.object(
                    gateway, '_wake_live_persisted_ids',
                    return_value=persisted_ids,
                ),
                mock.patch.object(gateway, '_wake_run_id_mark'),
                mock.patch('config_store.get_float', return_value=30),
                mock.patch(
                    'chat.window_identity.soft_window_enabled',
                    return_value=False,
                ),
            ):
                with self.assertLogs(gateway.app.logger.name, level='INFO') as captured:
                    response = gateway._wake_decide_locked(
                        data, 'normal', '', '',
                    )

        payload = response.get_json()
        self.assertEqual(payload['action'], 'message')
        events = [
            json.loads(line.split('[WAKE-LIVE] ', 1)[1])
            for line in captured.output
            if '[WAKE-LIVE] ' in line
        ]
        self.assertEqual(
            [event['stage'] for event in events],
            ['DELIVERY_COMMIT', 'SUCCESS'],
        )
        self.assertEqual(events[0]['assistant_message_id'], 9001)
        self.assertEqual(events[0]['wake_log_id'], 701)
        self.assertTrue(events[1]['frontend_visible'])


if __name__ == '__main__':
    unittest.main()
