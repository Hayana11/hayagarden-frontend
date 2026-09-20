import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
                return {
                    'thread': {
                        'id': 't-ephem',
                        'path': None,
                        'activePermissionProfile': {
                            'id': codex_app_server.NEXUS_PERMISSION_PROFILE,
                        },
                        'runtimeWorkspaceRoots': [tmp],
                    }
                }

            with mock.patch.object(nexus, '_request_locked', side_effect=request):
                tid = nexus._ensure_bound_thread_locked(None, 'dev')
            self.assertEqual(tid, 't-ephem')
            self.assertEqual(captured['method'], 'thread/start')
            self.assertTrue(captured['params'].get('ephemeral') is True)
            self.assertEqual(
                captured['params'].get('permissions'),
                codex_app_server.NEXUS_PERMISSION_PROFILE,
            )
            self.assertEqual(captured['params'].get('runtimeWorkspaceRoots'), [tmp])
            self.assertNotIn('sandbox', captured['params'])

            # Same process may resume an ephemeral thread id (in-process only).
            captured.clear()

            def resume_request(method, params, **kwargs):
                captured['method'] = method
                captured['params'] = dict(params)
                return {
                    'activePermissionProfile': {
                        'id': codex_app_server.NEXUS_PERMISSION_PROFILE,
                    },
                    'runtimeWorkspaceRoots': [tmp],
                }

            with mock.patch.object(nexus, '_request_locked', side_effect=resume_request):
                tid2 = nexus._ensure_bound_thread_locked('t-ephem', 'dev')
            self.assertEqual(tid2, 't-ephem')
            self.assertEqual(captured['method'], 'thread/resume')
            self.assertNotIn('ephemeral', captured['params'])
            self.assertNotIn('sandbox', captured['params'])
            self.assertNotIn('serviceName', captured['params'])
            self.assertTrue(nexus._session_resumed)

    def test_nexus_resume_failure_starts_new_thread_without_claiming_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, 'home')
            os.makedirs(home)
            nexus = codex_app_server.CodexAppServer(
                cwd=tmp,
                db_path=os.devnull,
                env_mode='nexus_allowlist',
                codex_home=home,
                ephemeral_threads=True,
            )
            calls = []

            def request(method, params, **kwargs):
                calls.append((method, dict(params)))
                if method == 'thread/resume':
                    raise codex_app_server.CodexAppServerError('stale thread')
                return {
                    'thread': {
                        'id': 'thread-new',
                        'path': None,
                        'activePermissionProfile': {
                            'id': codex_app_server.NEXUS_PERMISSION_PROFILE,
                        },
                        'runtimeWorkspaceRoots': [tmp],
                    }
                }

            with mock.patch.object(nexus, '_request_locked', side_effect=request):
                thread_id = nexus._ensure_bound_thread_locked('thread-old', 'dev')

            self.assertEqual(thread_id, 'thread-new')
            self.assertEqual([call[0] for call in calls], ['thread/resume', 'thread/start'])
            self.assertFalse(nexus._session_resumed)
            self.assertIn('stale thread', nexus._last_resume_error)

    def test_nexus_thread_fails_closed_without_active_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            nexus = codex_app_server.CodexAppServer(
                cwd=tmp,
                db_path=os.devnull,
                env_mode='nexus_allowlist',
                codex_home=tmp,
                ephemeral_threads=True,
            )
            with mock.patch.object(
                nexus,
                '_request_locked',
                return_value={'thread': {'id': 'unsafe', 'path': None}},
            ):
                with self.assertRaises(codex_app_server.CodexAppServerError):
                    nexus._ensure_bound_thread_locked(None, 'dev')

    def test_nexus_start_uses_new_session_and_records_pgid(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as home:
            process = mock.Mock(pid=4242, stdout=[], stderr=[])
            process.poll.return_value = None
            process.stdin = mock.Mock()
            with mock.patch.object(
                codex_app_server, 'find_codex', return_value='/tmp/fake-codex'
            ), mock.patch.object(
                codex_app_server.CodexAppServer,
                '_nexus_auth_status',
                return_value={'authenticated': True},
            ), mock.patch.object(
                codex_app_server.subprocess, 'Popen', return_value=process
            ) as popen, mock.patch.object(
                codex_app_server.os, 'getpgid', return_value=4242
            ), mock.patch.object(
                codex_app_server.CodexAppServer, '_request_locked', return_value={}
            ), mock.patch.object(
                codex_app_server.CodexAppServer, '_send_locked'
            ):
                nexus = codex_app_server.CodexAppServer(
                    cwd=workspace,
                    db_path=os.devnull,
                    env_mode='nexus_allowlist',
                    codex_home=home,
                    ephemeral_threads=True,
                )
                nexus._start_locked()

            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(nexus._process_pgid, 4242)
            self.assertFalse(nexus._stop_confirmed)

    def test_nexus_start_never_creates_missing_codex_home(self):
        with tempfile.TemporaryDirectory() as workspace:
            missing = os.path.join(workspace, 'missing-home')
            nexus = codex_app_server.CodexAppServer(
                cwd=workspace,
                db_path=os.devnull,
                env_mode='nexus_allowlist',
                codex_home=missing,
                ephemeral_threads=True,
            )
            with self.assertRaises(codex_app_server.CodexAppServerError) as ctx:
                nexus._start_locked()
            self.assertIn('must already exist', str(ctx.exception))
            self.assertFalse(os.path.exists(missing))

    def test_nexus_permission_profile_preserves_unrelated_config_and_auth(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home) / 'config.toml'
            config.write_text('model = "gpt-test"\n[unrelated]\nvalue = 7\n', encoding='utf-8')
            auth = Path(home) / 'auth.json'
            auth.write_text('{"token":"keep"}', encoding='utf-8')
            nexus = codex_app_server.CodexAppServer(
                cwd='/tmp',
                db_path=os.devnull,
                env_mode='nexus_allowlist',
                codex_home=home,
                ephemeral_threads=True,
            )
            nexus._ensure_nexus_permission_profile()
            text = config.read_text(encoding='utf-8')

            self.assertIn('model = "gpt-test"', text)
            self.assertIn('[unrelated]', text)
            self.assertIn('default_permissions = "hayagarden_nexus"', text)
            self.assertIn(f'{home!r}'.replace("'", '"') + ' = "deny"', text)
            self.assertIn('enabled = false', text)
            self.assertEqual(auth.read_text(encoding='utf-8'), '{"token":"keep"}')

    def test_stop_process_tree_kills_forked_sleeper(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / 'sleeper.pid'
            script = (
                'import os,time\n'
                'child=os.fork()\n'
                f'path={str(pid_file)!r}\n'
                'if child == 0:\n'
                ' open(path,"w").write(str(os.getpid()))\n'
                ' time.sleep(60)\n'
                'else:\n'
                ' time.sleep(60)\n'
            )
            process = subprocess.Popen(
                [sys.executable, '-c', script],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            server = codex_app_server.CodexAppServer(
                cwd=tmp,
                db_path=os.devnull,
                env_mode='nexus_allowlist',
                codex_home=tmp,
                ephemeral_threads=True,
            )
            server._process = process
            server._process_pgid = os.getpgid(process.pid)
            server._stop_confirmed = False
            server.clean_background_terminals = mock.Mock(return_value=True)
            deadline = time.time() + 3
            while not pid_file.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            sleeper_pid = int(pid_file.read_text(encoding='utf-8'))
            try:
                confirmed = server.stop_process_tree(soft_timeout=0.2, hard_timeout=1)
                self.assertTrue(confirmed)
                self.assertIsNotNone(process.poll())
                deadline = time.time() + 2
                alive = True
                while alive and time.time() < deadline:
                    stat = Path(f'/proc/{sleeper_pid}/stat')
                    alive = stat.exists() and ' Z ' not in stat.read_text(
                        encoding='utf-8', errors='replace'
                    )
                    if alive:
                        time.sleep(0.02)
                self.assertFalse(alive, f'forked sleeper {sleeper_pid} survived')
            finally:
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main()
