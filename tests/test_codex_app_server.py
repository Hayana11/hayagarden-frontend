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

    def test_inherit_env_unchanged_for_group_chat(self):
        os.environ['BOARD_TOKEN_TEST_MARKER'] = 'legacy-secret'
        env = self.client._environment()
        self.assertEqual(env.get('BOARD_TOKEN_TEST_MARKER'), 'legacy-secret')
        self.assertEqual(self.client.env_mode, 'inherit')
        self.assertFalse(self.client.ephemeral_threads)

    def test_nexus_allowlist_env_and_ephemeral_defaults(self):
        os.environ['BOARD_TOKEN_TEST_MARKER'] = 'must-not-leak'
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, 'codex-home')
            os.makedirs(home)
            nexus = codex_app_server.CodexAppServer(
                cwd=tmp,
                db_path=os.devnull,
                sandbox='workspace-write',
                env_mode='nexus_allowlist',
                codex_home=home,
                ephemeral_threads=True,
                service_name='hayagarden_nexus',
            )
            env = nexus._environment()
            self.assertNotIn('BOARD_TOKEN_TEST_MARKER', env)
            self.assertEqual(env['CODEX_HOME'], home)
            self.assertTrue(nexus.ephemeral_threads)
            captured = {}

            def request(method, params, **kwargs):
                captured['method'] = method
                captured['params'] = dict(params)
                return {'thread': {'id': 't-ephem', 'path': None}}

            with mock.patch.object(nexus, '_request_locked', side_effect=request):
                tid = nexus._ensure_bound_thread_locked(None, 'dev')
            self.assertEqual(tid, 't-ephem')
            self.assertEqual(captured['method'], 'thread/start')
            self.assertTrue(captured['params'].get('ephemeral') is True)

            # Same process may resume an ephemeral thread id (in-process only).
            captured.clear()

            def resume_request(method, params, **kwargs):
                captured['method'] = method
                captured['params'] = dict(params)
                return {}

            with mock.patch.object(nexus, '_request_locked', side_effect=resume_request):
                tid2 = nexus._ensure_bound_thread_locked('t-ephem', 'dev')
            self.assertEqual(tid2, 't-ephem')
            self.assertEqual(captured['method'], 'thread/resume')
            self.assertTrue(captured['params'].get('ephemeral') is True)


if __name__ == '__main__':
    unittest.main()
