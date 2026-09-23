import os
import tempfile
import unittest
from unittest import mock

import codex_app_server
import group_chat_store


class CodexAppServerTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(handle)
        group_chat_store.ensure_schema(self.db_path)
        self.client = codex_app_server.CodexAppServer(db_path=self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)

    def test_status_requires_install_and_login(self):
        with mock.patch.object(codex_app_server, 'find_codex', return_value=None):
            status = codex_app_server.runtime_status(force=True)
        self.assertFalse(status['installed'])
        self.assertFalse(status['ready'])

        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            codex_app_server, 'find_codex', return_value='/tmp/codex'
        ), mock.patch.object(codex_app_server.subprocess, 'run', return_value=completed):
            status = codex_app_server.runtime_status(force=True)
        self.assertTrue(status['installed'])
        self.assertTrue(status['authenticated'])
        self.assertTrue(status['ready'])

    def test_existing_thread_is_resumed(self):
        group_chat_store.save_thread_binding(
            'group', 'codex', 'thread-existing', db_path=self.db_path
        )
        with mock.patch.object(self.client, '_request_locked', return_value={}) as request:
            thread_id = self.client._ensure_thread_locked('group', 'persona')

        self.assertEqual(thread_id, 'thread-existing')
        self.assertEqual(request.call_args.args[0], 'thread/resume')
        self.assertEqual(request.call_args.args[1]['sandbox'], 'read-only')
        self.assertEqual(request.call_args.args[1]['approvalPolicy'], 'never')

    def test_missing_persisted_thread_is_replaced(self):
        group_chat_store.save_thread_binding(
            'codex', 'codex', 'thread-stale', db_path=self.db_path
        )
        responses = [
            codex_app_server.CodexAppServerError('not found'),
            {'thread': {'id': 'thread-fresh'}},
        ]

        def request(*_args, **_kwargs):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch.object(self.client, '_request_locked', side_effect=request):
            thread_id = self.client._ensure_thread_locked('codex', 'persona')

        self.assertEqual(thread_id, 'thread-fresh')
        binding = group_chat_store.get_thread_binding(
            'codex', 'codex', db_path=self.db_path
        )
        self.assertEqual(binding['thread_id'], 'thread-fresh')

    def test_agent_message_text_is_the_only_item_rendered(self):
        self.assertEqual(
            self.client._agent_text_from_item(
                {'type': 'agentMessage', 'text': 'hello'}
            ),
            'hello',
        )
        self.assertEqual(
            self.client._agent_text_from_item(
                {'type': 'commandExecution', 'text': 'secret'}
            ),
            '',
        )

    def test_permission_request_is_answered_with_no_grants(self):
        request = {
            'id': 77,
            'method': 'item/permissions/requestApproval',
            'params': {'threadId': 'thread-one'},
        }
        with mock.patch.object(self.client, '_send_locked') as send:
            handled = self.client._answer_server_request_locked(request)

        self.assertTrue(handled)
        self.assertEqual(send.call_args.args[0], {
            'id': 77,
            'result': {'permissions': {}, 'scope': 'turn'},
        })

    def test_model_list_exposes_default_and_efforts(self):
        payload = {
            'data': [{
                'id': 'gpt-5.6-sol',
                'displayName': 'GPT-5.6-Sol',
                'isDefault': True,
                'defaultReasoningEffort': 'low',
                'supportedReasoningEfforts': [
                    {'reasoningEffort': 'low'},
                    {'reasoningEffort': 'high'},
                ],
                'inputModalities': ['text', 'image'],
            }],
            'nextCursor': None,
        }
        with mock.patch.object(self.client, '_start_locked'), \
             mock.patch.object(self.client, '_request_locked', return_value=payload):
            models = self.client.list_models(force=True)

        self.assertEqual(models[0]['id'], 'gpt-5.6-sol')
        self.assertEqual(models[0]['label'], 'GPT-5.6-Sol')
        self.assertTrue(models[0]['is_default'])
        self.assertEqual(models[0]['efforts'], ['low', 'high'])
        with mock.patch.object(codex_app_server.config_store, 'get', return_value=''):
            self.assertEqual(self.client.resolved_model(), ('gpt-5.6-sol', 'default'))

    def test_explicit_configured_model_is_sent_with_turn(self):
        with mock.patch.object(
            codex_app_server.config_store, 'get', return_value='gpt-5.6-sol'
        ):
            params, model, mode = self.client._turn_params('thread-one', 'hello')

        self.assertEqual(model, 'gpt-5.6-sol')
        self.assertEqual(mode, 'explicit')
        self.assertEqual(params['model'], 'gpt-5.6-sol')
        self.assertEqual(params['threadId'], 'thread-one')


if __name__ == '__main__':
    unittest.main()
