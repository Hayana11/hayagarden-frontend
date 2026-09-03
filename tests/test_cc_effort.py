import sys
import builtins
import io
import os
import sqlite3
import tempfile
import types
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault(
    'HAYAGARDEN_CONFIG_DB_PATH',
    os.path.join(tempfile.gettempdir(), 'hayagarden-test-cc-effort.db'),
)
os.environ.setdefault(
    'HAYAGARDEN_ENV_PATH',
    os.path.join(tempfile.gettempdir(), 'hayagarden-test-cc-effort.env'),
)

import config_store

_ROOT = Path(__file__).resolve().parents[1]
_chat_pkg = types.ModuleType('chat')
_chat_pkg.__path__ = [str(_ROOT / 'chat')]
sys.modules.setdefault('chat', _chat_pkg)


def _load_chat_module(name):
    spec = importlib.util.spec_from_file_location(name, _ROOT / 'chat' / (name.rsplit('.', 1)[1] + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cc_effort = _load_chat_module('chat.cc_effort')
_load_chat_module('chat.cc_model')


class CcEffortHelperTests(unittest.TestCase):
    def test_default_snapshot_has_no_cli_argument(self):
        with patch.object(config_store, 'get', return_value=''):
            self.assertEqual(cc_effort.cc_effort_snapshot(), ('', 'default', []))

    def test_explicit_snapshot(self):
        with patch.object(config_store, 'get', return_value='HIGH'):
            self.assertEqual(
                cc_effort.cc_effort_snapshot(),
                ('high', 'explicit:high', ['--effort', 'high']),
            )

    def test_invalid_values_are_rejected(self):
        for value in ('ultra', 'foo', 'HIGH!'):
            with self.subTest(value=value), patch.object(config_store, 'set') as save:
                result = cc_effort.set_cc_chat_effort(value)
                self.assertFalse(result['ok'])
                save.assert_not_called()

    def test_write_failure_is_not_success(self):
        with patch.object(config_store, 'set', side_effect=OSError('db unavailable')):
            with self.assertRaises(OSError):
                cc_effort.set_cc_chat_effort('high')


class ResidentEffortTests(unittest.TestCase):
    def _runtime_modules(self):
        runtime = types.ModuleType('chat.cc_runtime')
        runtime.ClaudeRuntimeError = RuntimeError
        runtime.claude_cmd = lambda *args: ['claude', *args]
        runtime.require_pinned_claude_version = lambda **kwargs: None
        history = types.ModuleType('chat.cc_history_rewrite')
        history.current_history_rewrite_epoch = lambda: ''
        history.sanitize_bound_epoch = lambda value: str(value or '')
        history.is_unreadable_epoch = lambda value: False
        return runtime, history

    def _resident(self, effort='high'):
        import cc_resident

        runtime, history = self._runtime_modules()
        modules = {
            'chat.cc_runtime': runtime,
            'chat.cc_history_rewrite': history,
        }
        values = {'CC_CHAT_MODEL': '', 'CC_CHAT_EFFORT': effort}
        config_get = lambda key, default=None: values.get(key, default)
        return cc_resident, modules, config_get

    def test_all_spawn_paths_forward_effort(self):
        import cc_resident

        resident, modules, config_get = self._resident()

        class Proc:
            pid = 123
            stdin = None
            stdout = None
            stderr = None

            def poll(self):
                return None

        with patch.dict(sys.modules, modules), \
                patch.object(cc_resident.config_store, 'get', side_effect=config_get), \
                patch.object(cc_resident.subprocess, 'Popen', return_value=Proc()) as popen:
            main = cc_resident.ResidentSession('/tmp', '', '')
            main._spawn('system', None)
            self.assertIn(['--effort', 'high'], [popen.call_args.args[0][i:i + 2] for i in range(len(popen.call_args.args[0]) - 1)])

            staged = cc_resident.ResidentSession('/tmp', '', '')
            staged.spawn_resumable('system', None, resume_session_id='session-1')
            fresh = cc_resident.ResidentSession('/tmp', '', '')
            fresh.spawn_fresh_named('system', None, session_id='12345678-1234-5678-1234-567812345678')
            self.assertEqual(popen.call_count, 3)
            for call in popen.call_args_list:
                args = call.args[0]
                self.assertIn('--effort', args)
                self.assertIn('high', args)

    def test_default_spawn_omits_effort(self):
        import cc_resident

        resident, modules, config_get = self._resident('')

        class Proc:
            pid = 123
            stdin = None
            stdout = None
            stderr = None

            def poll(self):
                return None

        with patch.dict(sys.modules, modules), \
                patch.object(cc_resident.config_store, 'get', side_effect=config_get), \
                patch.object(cc_resident.subprocess, 'Popen', return_value=Proc()) as popen:
            resident.ResidentSession('/tmp', '', '')._spawn('system', None)
            args = popen.call_args.args[0]
            self.assertNotIn('--effort', args)

    def test_effort_change_is_lazy_and_identity_aware(self):
        import cc_resident

        resident, modules, config_get = self._resident('medium')

        class Alive:
            def poll(self):
                return None

        with patch.dict(sys.modules, modules), \
                patch.object(cc_resident.config_store, 'get', side_effect=config_get):
            item = resident.ResidentSession('/tmp', '', '')
            item._proc = Alive()
            item._system_text = 'system'
            item._model_identity = 'default'
            item._effort_identity = 'explicit:high'
            self.assertEqual(item._decide_respawn_reason('system'), 'effort_changed')


class UsageEffortTests(unittest.TestCase):
    def test_runtime_preserves_explicit_and_default_semantics(self):
        from tools.cc_usage_observability import build_runtime

        explicit = build_runtime(
            resident_generation=1, resident_pid=1, resident_turn_count=1,
            respawn_reason=None, idle_seconds_before_turn=0, is_cold=False,
            static_system='', mcp_config_text='', effort='high',
            thinking_config={'thinking_display': 'summarized', 'effort': 'high'},
        )
        default = build_runtime(
            resident_generation=1, resident_pid=1, resident_turn_count=1,
            respawn_reason=None, idle_seconds_before_turn=0, is_cold=False,
            static_system='', mcp_config_text='', effort=None,
            thinking_config={'thinking_display': 'summarized', 'effort': None},
        )
        self.assertEqual(explicit['effort'], 'high')
        self.assertEqual(default['effort'], None)
        self.assertIsNotNone(explicit['thinking_sha256'])
        self.assertIsNotNone(default['thinking_sha256'])
        self.assertNotEqual(explicit['thinking_sha256'], default['thinking_sha256'])



class EffortRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config_db_path = str(Path(cls.tmp.name) / 'config.db')
        cls.memory_db_path = str(Path(cls.tmp.name) / 'memories.db')
        cls.config_patch = patch.object(config_store, 'DB_PATH', cls.config_db_path)
        cls.env_patch = patch.object(config_store, 'ENV_PATH', str(Path(cls.tmp.name) / '.env'))
        cls.config_patch.start()
        cls.env_patch.start()
        config_store._init_table()

        def redirect_connect(path, *args, **kwargs):
            raw = os.fspath(path)
            if raw.startswith('/opt/frontend/'):
                path = str(Path(cls.tmp.name) / Path(raw).name)
            return cls.real_connect(path, *args, **kwargs)

        def redirect_open(file, *args, **kwargs):
            try:
                raw = os.fspath(file)
            except TypeError:
                raw = ''
            if raw == '/opt/frontend/.env':
                return io.StringIO('')
            return cls.real_open(file, *args, **kwargs)

        cls.real_connect = sqlite3.connect
        cls.real_open = builtins.open
        with patch.object(sqlite3, 'connect', side_effect=redirect_connect), \
                patch.object(builtins, 'open', side_effect=redirect_open):
            sys.modules.setdefault('moments_cover', types.ModuleType('moments_cover'))
            if 'account_balance_routes' not in sys.modules:
                from flask import Blueprint
                account_routes = types.ModuleType('account_balance_routes')
                account_routes.create_relay_account_blueprint = lambda **_kwargs: Blueprint(
                    'cc_effort_account_stub', __name__,
                )
                sys.modules['account_balance_routes'] = account_routes
            import app as app_module
        cls.app_module = app_module
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.env_patch.stop()
        cls.config_patch.stop()
        cls.tmp.cleanup()

    def setUp(self):
        conn = sqlite3.connect(config_store.DB_PATH)
        conn.execute('DELETE FROM runtime_config')
        conn.commit()
        conn.close()
        config_store.set('CHAT_PROVIDER', 'claude_code')
        config_store.set('CC_CHAT_EFFORT', '')

    def test_get_claude_default(self):
        response = self.client.get('/api/config/effort')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'provider': 'claude_code',
            'configured_effort': None,
            'effort_mode': 'default',
            'allowed_efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
        })

    def test_get_explicit_high(self):
        config_store.set('CC_CHAT_EFFORT', 'high')
        response = self.client.get('/api/config/effort')
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['configured_effort'], 'high')
        self.assertEqual(body['effort_mode'], 'explicit')

    def test_post_high_persists(self):
        response = self.client.post('/api/config/effort', json={'effort': 'high'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), 'high')

    def test_post_null_restores_default(self):
        config_store.set('CC_CHAT_EFFORT', 'high')
        response = self.client.post('/api/config/effort', json={'effort': None})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), '')

    def test_post_invalid_preserves_previous_config(self):
        config_store.set('CC_CHAT_EFFORT', 'high')
        response = self.client.post('/api/config/effort', json={'effort': 'ultra'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), 'high')

    def test_non_claude_provider_is_unavailable_and_not_mutated(self):
        config_store.set('CC_CHAT_EFFORT', 'high')
        config_store.set('CHAT_PROVIDER', 'api_relay')
        get_response = self.client.get('/api/config/effort')
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.get_json(), {
            'provider': 'api_relay',
            'configured_effort': None,
            'effort_mode': 'unavailable',
            'allowed_efforts': [],
        })
        post_response = self.client.post('/api/config/effort', json={'effort': 'low'})
        self.assertEqual(post_response.status_code, 409)
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), 'high')


if __name__ == '__main__':
    unittest.main()
