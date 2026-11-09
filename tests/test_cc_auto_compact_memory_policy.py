import os
import subprocess
import sys
import types
import unittest
from unittest import mock

import cc_resident
from chat import cc_effort, cc_model
from chat import daily_runtime

_AUTO_COMPACT = 'DISABLE_AUTO_COMPACT'
_AUTO_MEMORY = 'CLAUDE_CODE_DISABLE_AUTO_MEMORY'


def _runtime_module():
    runtime = types.ModuleType('chat.cc_runtime')
    runtime.ClaudeRuntimeError = RuntimeError
    runtime.MINIMUM_CLAUDE_CODE_VERSION = '2.1.280'
    runtime.version_tuple = lambda value: tuple(int(part) for part in str(value).split('.'))
    runtime.claude_cmd_for_version = (
        lambda version, *args, **kwargs: ['/managed/versions/' + version, *args]
    )
    runtime.require_managed_claude_runtime = lambda **kwargs: '2.1.280'
    runtime.service_home = lambda env=None: '/tmp'
    return runtime


class FormalChatAutoCompactMemoryPolicyTests(unittest.TestCase):
    def test_formal_chat_spawn_families_set_policy_on_each_child(self):
        import config_store

        class Process:
            pid = 123
            stdin = None
            stdout = None
            stderr = None

            def poll(self):
                return None

        parent_env = {
            'HOME': '/tmp',
            _AUTO_COMPACT: '0',
            _AUTO_MEMORY: '0',
        }
        popen = mock.Mock(side_effect=lambda *args, **kwargs: Process())
        with (
            mock.patch.dict(sys.modules, {'chat.cc_runtime': _runtime_module()}),
            mock.patch.object(cc_model, 'cc_model_snapshot', return_value=('', 'default', [])),
            mock.patch.object(cc_effort, 'cc_effort_snapshot', return_value=('', 'default', [])),
            mock.patch.object(config_store, 'get_int', return_value=0),
            mock.patch.object(cc_resident.time, 'sleep'),
            mock.patch.object(cc_resident.subprocess, 'Popen', popen),
        ):
            cc_resident.ResidentSession(
                '/tmp', '', '', formal_chat_continuity=True,
            )._spawn('system', dict(parent_env), tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY)
            cc_resident.ResidentSession(
                '/tmp', '', '', formal_chat_continuity=True,
            ).spawn_resumable(
                'system',
                dict(parent_env),
                resume_session_id='resume-sid',
                tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY,
            )
            cc_resident.ResidentSession(
                '/tmp', '', '', formal_chat_continuity=True,
            ).spawn_fresh_named(
                'system',
                dict(parent_env),
                session_id='12345678-1234-5678-1234-567812345678',
                tool_profile=cc_resident.TOOL_PROFILE_TEXT_ONLY,
            )

        self.assertEqual(popen.call_count, 3)
        for call in popen.call_args_list:
            env = call.kwargs['env']
            self.assertEqual(env[_AUTO_COMPACT], '1')
            self.assertEqual(env[_AUTO_MEMORY], '1')
            self.assertNotIn('DISABLE_COMPACT', env)
        resume_args = popen.call_args_list[1].args[0]
        self.assertEqual(resume_args.count('--system-prompt-snapshot'), 1)
        self.assertEqual(
            resume_args[resume_args.index('--system-prompt-snapshot') + 1],
            'off',
        )
        self.assertEqual(parent_env[_AUTO_COMPACT], '0')
        self.assertEqual(parent_env[_AUTO_MEMORY], '0')

    def test_default_resident_does_not_receive_formal_chat_policy(self):
        env = cc_resident.ResidentSession('/tmp', '', '')._prepare_spawn_env(
            {'HOME': '/tmp'},
        )
        self.assertNotIn(_AUTO_COMPACT, env)
        self.assertNotIn(_AUTO_MEMORY, env)
        self.assertNotIn('DISABLE_COMPACT', env)

    def test_real_child_sees_only_the_two_policy_values(self):
        parent_env = {
            'PATH': os.environ.get('PATH') or '/usr/bin:/bin',
            'HOME': os.environ.get('HOME') or '/tmp',
            _AUTO_COMPACT: '0',
            _AUTO_MEMORY: '0',
        }
        child_env = cc_resident.ResidentSession(
            '/tmp', '', '', formal_chat_continuity=True,
        )._prepare_spawn_env(parent_env)
        program = (
            'import os; '
            'print(os.environ["DISABLE_AUTO_COMPACT"]); '
            'print(os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"])'
        )
        result = subprocess.run(
            [sys.executable, '-c', program],
            check=True,
            capture_output=True,
            text=True,
            env=child_env,
        )
        self.assertEqual(result.stdout.splitlines(), ['1', '1'])
        self.assertEqual(parent_env[_AUTO_COMPACT], '0')
        self.assertEqual(parent_env[_AUTO_MEMORY], '0')

    def test_capacity_staging_inherits_formal_chat_policy_from_live_resident(self):
        import config_store

        class Process:
            pid = 456
            stdin = None
            stdout = None
            stderr = None

            def poll(self):
                return None

        live = cc_resident.ResidentSession(
            '/tmp', '', '', formal_chat_continuity=True,
        )
        seen = {}
        def capture_popen(*args, **kwargs):
            seen.update(kwargs)
            seen['args'] = args[0]
            return Process()

        with (
            mock.patch.dict(sys.modules, {'chat.cc_runtime': _runtime_module()}),
            mock.patch.object(cc_model, 'cc_model_snapshot', return_value=('', 'default', [])),
            mock.patch.object(cc_effort, 'cc_effort_snapshot', return_value=('', 'default', [])),
            mock.patch.object(config_store, 'get_int', return_value=0),
            mock.patch.object(cc_resident.time, 'sleep'),
            mock.patch.object(cc_resident.subprocess, 'Popen', side_effect=capture_popen),
            mock.patch.object(
                cc_resident.ResidentSession, '_build_spawn_tool_flags',
                return_value={
                    'tools': '', 'extra': [], 'surface_allowed': '',
                    'mcp_path': None, 'surface_fingerprint': None,
                },
            ),
            mock.patch.object(
                cc_resident.ResidentSession, '_require_spawn_surface_fingerprint',
                return_value=None,
            ),
            mock.patch.object(
                cc_resident.ResidentSession, 'wait_staged_health',
            ),
        ):
            hooks = daily_runtime._default_capacity_swap_staged_hooks(live)
            staged = hooks.spawn_and_health(
                'system',
                {'HOME': '/tmp', _AUTO_COMPACT: '0', _AUTO_MEMORY: '0'},
                resume_session_id='capacity-sid',
                jsonl_path=None,
                expected_sha256=None,
            )

        self.assertTrue(staged._formal_chat_continuity)
        self.assertEqual(seen['env'][_AUTO_COMPACT], '1')
        self.assertEqual(seen['env'][_AUTO_MEMORY], '1')
        self.assertEqual(
            seen['args'][seen['args'].index('--system-prompt-snapshot') + 1],
            'off',
        )


if __name__ == '__main__':
    unittest.main()
