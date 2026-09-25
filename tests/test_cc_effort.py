import sys
import builtins
import io
import json
import urllib.error
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
cc_model = _load_chat_module('chat.cc_model')


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
        runtime.MINIMUM_CLAUDE_CODE_VERSION = '2.1.280'
        runtime.version_tuple = lambda value: tuple(int(part) for part in str(value).split('.'))
        runtime.claude_cmd_for_version = lambda version, *args, **kwargs: ['/native/versions/' + version, *args]
        runtime.require_managed_claude_runtime = lambda **kwargs: '2.1.280'
        runtime.require_pinned_claude_version = runtime.require_managed_claude_runtime
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



class _AppRouteTestCase(unittest.TestCase):
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
        conn = cls.real_connect(cls.memory_db_path)
        conn.execute(
            """CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL DEFAULT 'user',
                content TEXT NOT NULL DEFAULT '',
                thinking TEXT DEFAULT '',
                tool_calls TEXT DEFAULT '',
                branches TEXT DEFAULT '',
                branch_idx INTEGER DEFAULT 0,
                cache_info TEXT DEFAULT '',
                choices TEXT DEFAULT '',
                image_url TEXT DEFAULT '',
                file_url TEXT DEFAULT '',
                file_name TEXT DEFAULT '',
                attachments TEXT DEFAULT '[]',
                display_segments TEXT DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'chat',
                created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.commit()
        conn.close()
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


class EffortRouteTests(_AppRouteTestCase):
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

    def test_provider_post_writes_canonical_chat_provider_only(self):
        config_store.set('GW_PROVIDER', 'claude_code')
        response = self.client.post('/api/config/provider', json={'provider': 'api_relay'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])
        self.assertEqual(response.get_json()['provider'], 'api_relay')
        self.assertEqual(config_store.get('CHAT_PROVIDER'), 'api_relay')
        self.assertEqual(config_store.get('GW_PROVIDER'), 'claude_code')


class OfficialCcEffortRouteTests(_AppRouteTestCase):
    def setUp(self):
        conn = sqlite3.connect(config_store.DB_PATH)
        conn.execute('DELETE FROM runtime_config')
        conn.commit()
        conn.close()
        config_store.set('CHAT_PROVIDER', 'api_relay')
        config_store.set('CC_CHAT_EFFORT', '')

    def test_get_works_when_chat_provider_is_relay(self):
        response = self.client.get('/api/config/cc-effort')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'configured_effort': None,
            'effort_mode': 'default',
            'allowed_efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
            'provider': 'claude_code',
        })

    def test_post_high_persists_without_switching_chat_provider(self):
        response = self.client.post('/api/config/cc-effort', json={'effort': 'high'})
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['configured_effort'], 'high')
        self.assertEqual(body['effort_mode'], 'explicit')
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), 'high')
        self.assertEqual(config_store.get('CHAT_PROVIDER'), 'api_relay')

    def test_post_null_restores_default(self):
        config_store.set('CC_CHAT_EFFORT', 'max')
        response = self.client.post('/api/config/cc-effort', json={'effort': None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), '')
        self.assertEqual(response.get_json()['effort_mode'], 'default')

    def test_post_invalid_preserves_previous_config(self):
        config_store.set('CC_CHAT_EFFORT', 'low')
        response = self.client.post('/api/config/cc-effort', json={'effort': 'ultra'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config_store.get('CC_CHAT_EFFORT'), 'low')


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


class ModelControlRouteTests(_AppRouteTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.codex_client = cls.app_module.codex_app_server.client

    @classmethod
    def _gateway_module(cls):
        if 'gateway' in sys.modules:
            return sys.modules['gateway']
        real_connect = sqlite3.connect
        real_open = builtins.open

        def isolated_connect(database, *args, **kwargs):
            raw = os.fspath(database)
            if raw.startswith('/opt/frontend/'):
                database = str(Path(cls.tmp.name) / Path(raw).name)
            return real_connect(database, *args, **kwargs)

        def isolated_open(file, *args, **kwargs):
            try:
                raw = os.fspath(file)
            except TypeError:
                raw = ''
            if raw == '/opt/frontend/.env':
                return io.StringIO('')
            return real_open(file, *args, **kwargs)

        with patch.object(sqlite3, 'connect', side_effect=isolated_connect), \
                patch.object(builtins, 'open', side_effect=isolated_open):
            import gateway
        return gateway

    def setUp(self):
        config_store.set('CHAT_PROVIDER', 'claude_code')
        config_store.set('CODEX_CHAT_MODEL', '')
        config_store.set('CODEX_CHAT_EFFORT', '')
        config_store.set('DEEPSEEK_CHAT_MODEL', 'deepseek-flash')

    def test_cc_catalog_reports_safe_fallback_and_preserves_default(self):
        config_store.set('CC_CHAT_MODEL', '')
        with patch.object(cc_model, '_native_model_catalog_adapter', return_value=None) as adapter:
            with patch('urllib.request.urlopen', side_effect=AssertionError('CC fallback must not make network calls')):
                response = self.client.get('/api/config/model-catalog')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['provider'], 'claude_code')
        self.assertEqual(body['model_mode'], 'default')
        self.assertIsNone(body['configured_model'])
        self.assertTrue(body['configured_model_available'])
        self.assertEqual(body['catalog_source'], 'fallback')
        self.assertTrue(body['catalog_ready'])
        self.assertIsNone(body['catalog_error'])
        self.assertIsNone(body['catalog_refreshed_at'])
        opus = next(model for model in body['models'] if model['id'] == 'claude-opus-5-5')
        self.assertEqual(opus['label'], 'Opus 5.5')
        self.assertEqual(len(body['models']), 7)
        self.assertNotIn('api_key', body)
        adapter.assert_called_once_with(force=False)
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), '')

    def test_cc_catalog_refresh_is_explicit_and_non_generative(self):
        with patch.object(cc_model, '_native_model_catalog_adapter', return_value=None) as adapter:
            with patch('urllib.request.urlopen', side_effect=AssertionError('catalog refresh must not call a model API')):
                response = self.client.get('/api/config/model-catalog?refresh=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['catalog_source'], 'fallback')
        adapter.assert_called_once_with(force=True)

    def test_cc_discovery_failure_falls_back_without_clearing_configuration(self):
        stale_model = 'claude-opus-5-5-preview'
        config_store.set('CC_CHAT_MODEL', stale_model)
        with patch.object(cc_model, '_native_model_catalog_adapter', side_effect=RuntimeError('unavailable')):
            response = self.client.get('/api/config/model-catalog?refresh=1')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['catalog_source'], 'fallback')
        self.assertTrue(body['catalog_ready'])
        self.assertEqual(body['catalog_error'], 'native_discovery_failed')
        self.assertIsNone(body['catalog_refreshed_at'])
        self.assertEqual(body['configured_model'], stale_model)
        self.assertFalse(body['configured_model_available'])
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), stale_model)

    def test_cc_stale_configured_model_is_retained_and_reported(self):
        stale_model = 'claude-opus-5-5-preview'
        config_store.set('CC_CHAT_MODEL', stale_model)
        response = self.client.get('/api/config/model-catalog')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['configured_model'], stale_model)
        self.assertEqual(body['current'], stale_model)
        self.assertEqual(body['model_mode'], 'explicit')
        self.assertFalse(body['configured_model_available'])
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), stale_model)
        # Frozen task authority stays parseable without consulting a changing catalog.
        with patch.object(cc_model, 'get_cc_model_catalog', side_effect=AssertionError('frozen identity must not refresh catalog')):
            self.assertEqual(
                cc_model.cc_model_args_from_identity('explicit:' + stale_model),
                ['--model', stale_model],
            )
        with self.assertRaises(ValueError):
            cc_model.cc_model_args_from_identity('explicit:--dangerous')

    def test_cc_opus_55_selection_and_default_semantics(self):
        with patch.object(cc_model, '_active_runtime_version_for_catalog', return_value='2.1.280'), \
             patch('urllib.request.urlopen', side_effect=AssertionError('selection must not call a model API')):
            response = self.client.post('/api/config/model', json={'model': 'claude-opus-5-5'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(config_store.get('CC_CHAT_MODEL'), 'claude-opus-5-5')
            self.assertEqual(cc_model.cc_model_args(), ['--model', 'claude-opus-5-5'])
            invalid = self.client.post('/api/config/model', json={'model': 'relay-only-model'})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(config_store.get('CC_CHAT_MODEL'), 'claude-opus-5-5')
            clear = self.client.post('/api/config/model', json={'model': None})
        self.assertEqual(clear.status_code, 200)
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), '')
        self.assertEqual(cc_model.cc_model_args(), [])

    def test_opus_55_is_rejected_below_runtime_floor_without_config_mutation(self):
        config_store.set('CC_CHAT_MODEL', 'claude-opus-5')
        with patch.object(cc_model, '_active_runtime_version_for_catalog', return_value='2.1.220'):
            response = self.client.post('/api/config/model', json={'model': 'claude-opus-5-5'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error'], 'CC_MODEL_RUNTIME_INCOMPATIBLE')
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), 'claude-opus-5')

    def test_relay_catalog_does_not_consult_cc_catalog_or_mutate_cc_model(self):
        config_store.set('CHAT_PROVIDER', 'api_relay')
        config_store.set('CC_CHAT_MODEL', 'claude-opus-5-5')
        real_open = builtins.open

        def open_models_file(path, *args, **kwargs):
            if os.fspath(path) == '/opt/frontend/models.json':
                return io.StringIO('[{"id":"relay-only","label":"Relay Only"}]')
            return real_open(path, *args, **kwargs)

        with patch.object(cc_model, 'get_cc_model_catalog') as cc_catalog:
            with patch('builtins.open', side_effect=open_models_file):
                response = self.client.get('/api/config/model-catalog?refresh=1')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['models'], [{'id': 'relay-only', 'label': 'Relay Only'}])
        self.assertEqual(body['provider'], 'api_relay')
        self.assertNotIn('catalog_source', body)
        self.assertNotIn('configured_model_available', body)
        cc_catalog.assert_not_called()
        self.assertEqual(config_store.get('CC_CHAT_MODEL'), 'claude-opus-5-5')

    def test_codex_catalog_keeps_identity_and_runtime_model_separate(self):
        server = self.app_module.codex_app_server.CodexAppServer(db_path=':memory:')
        response_payload = {
            'data': [{
                'id': 'catalog-alias',
                'model': 'gpt-5.6-sol',
                'displayName': 'GPT-5.6-Sol',
                'isDefault': True,
                'defaultReasoningEffort': 'low',
                'supportedReasoningEfforts': [{'reasoningEffort': 'low'}],
            }],
            'nextCursor': None,
        }
        with patch.object(server, '_start_locked'), \
                patch.object(server, '_request_locked', return_value=response_payload):
            models = server.list_models(force=True)
        self.assertEqual(models[0]['id'], 'catalog-alias')
        self.assertEqual(models[0]['model'], 'gpt-5.6-sol')
        with patch.object(config_store, 'get', return_value=''):
            self.assertEqual(server.resolved_model(), ('gpt-5.6-sol', 'default'))
            params, model, _mode = server._turn_params('thread-one', 'hello')
        self.assertEqual(model, 'gpt-5.6-sol')
        self.assertEqual(params['model'], 'gpt-5.6-sol')

    def test_codex_catalog_drops_rows_without_runtime_model(self):
        server = self.app_module.codex_app_server.CodexAppServer(db_path=':memory:')
        with patch.object(server, '_start_locked'), \
                patch.object(server, '_request_locked', return_value={
                    'data': [{'id': 'catalog-only'}],
                    'nextCursor': None,
                }):
            self.assertEqual(server.list_models(force=True), [])

    def test_codex_get_exposes_runtime_and_identity_fields(self):
        models = [{
            'id': 'catalog-alias',
            'model': 'gpt-5.6-sol',
            'label': 'GPT-5.6-Sol',
            'is_default': True,
            'default_effort': 'low',
            'efforts': ['low'],
            'input_modalities': ['text'],
        }]
        with patch.object(self.app_module.codex_app_server, 'runtime_status', return_value={'ready': True}), \
                patch.object(self.codex_client, 'configured_model', return_value='gpt-5.6-sol'), \
                patch.object(self.codex_client, 'list_models', return_value=models):
            response = self.client.get('/api/group-chat/codex-models')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['models'][0]['id'], 'catalog-alias')
        self.assertEqual(body['models'][0]['model'], 'gpt-5.6-sol')
        self.assertEqual(body['configured_model_id'], 'catalog-alias')
        self.assertEqual(body['configured_model'], 'gpt-5.6-sol')
        self.assertEqual(body['default_model_id'], 'catalog-alias')
        self.assertEqual(body['default_model'], 'gpt-5.6-sol')

    def test_codex_post_maps_catalog_id_to_runtime_turn_model(self):
        models = [
            {'id': 'catalog-alias', 'model': 'gpt-5.6-sol', 'is_default': False},
            {'id': 'catalog-default', 'model': 'gpt-6-default', 'is_default': True},
        ]
        with patch.object(self.codex_client, 'list_models', return_value=models):
            response = self.client.post('/api/group-chat/codex-model', json={'model_id': 'catalog-alias'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config_store.get('CODEX_CHAT_MODEL'), 'gpt-5.6-sol')
        body = response.get_json()
        self.assertEqual(body['configured_model_id'], 'catalog-alias')
        self.assertEqual(body['configured_model'], 'gpt-5.6-sol')
        params, model, mode = self.codex_client._turn_params('thread-one', 'hello')
        self.assertEqual((model, mode), ('gpt-5.6-sol', 'explicit'))
        self.assertEqual(params['model'], 'gpt-5.6-sol')

    def test_codex_invalid_identity_does_not_change_runtime_setting(self):
        config_store.set('CODEX_CHAT_MODEL', 'existing-runtime-model')
        with patch.object(self.codex_client, 'list_models', return_value=[
            {'id': 'catalog-alias', 'model': 'gpt-5.6-sol', 'is_default': True},
        ]):
            response = self.client.post('/api/group-chat/codex-model', json={'model_id': 'unknown-id'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config_store.get('CODEX_CHAT_MODEL'), 'existing-runtime-model')

    def test_deepseek_catalog_uses_server_key_and_never_returns_it(self):
        secret = 'deepseek-key-sentinel-never-return'
        api_response = _FakeHTTPResponse(json.dumps({
            'data': [{'id': 'deepseek-flash'}, {'id': 'deepseek-v4-pro'}],
        }).encode())
        with patch.object(self.app_module, '_deployment_secret', return_value=secret), \
                patch('urllib.request.urlopen', return_value=api_response) as upstream:
            response = self.client.get('/api/config/deepseek')
        request_obj = upstream.call_args.args[0]
        self.assertEqual(request_obj.headers.get('Authorization'), 'Bearer ' + secret)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['key_configured'])
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_deepseek_catalog_rejects_invalid_shape_without_leaking_key(self):
        secret = 'deepseek-key-sentinel-invalid'
        with patch.object(self.app_module, '_deployment_secret', return_value=secret), \
                patch('urllib.request.urlopen', return_value=_FakeHTTPResponse(b'[]')):
            models, error = self.app_module._deepseek_model_catalog()
        self.assertEqual(models, [])
        self.assertEqual(error, 'invalid_response')

    def test_deepseek_get_without_key_is_stable_and_secret_free(self):
        with patch.object(self.app_module, '_deployment_secret', return_value=''):
            response = self.client.get('/api/config/deepseek')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()['key_configured'])
        self.assertEqual(response.get_json()['error'], 'missing_key')
        self.assertNotIn('api_key', response.get_data(as_text=True).lower())

    def test_deepseek_catalog_auth_failure_is_generic(self):
        secret = 'deepseek-key-sentinel-unauthorized'
        error = urllib.error.HTTPError(
            'https://api.deepseek.com/models', 401, 'unauthorized', {}, None,
        )
        with patch.object(self.app_module, '_deployment_secret', return_value=secret), \
                patch('urllib.request.urlopen', side_effect=error):
            response = self.client.post('/api/config/deepseek/model', json={'model': 'deepseek-v4-pro'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['detail'], 'unauthorized')
        self.assertNotIn(secret, response.get_data(as_text=True))
        self.assertEqual(config_store.get('DEEPSEEK_CHAT_MODEL'), 'deepseek-flash')

    def test_deepseek_invalid_model_does_not_change_configuration(self):
        config_store.set('DEEPSEEK_CHAT_MODEL', 'deepseek-flash')
        with patch.object(self.app_module, '_deepseek_model_catalog', return_value=(
            [{'id': 'deepseek-flash'}], '',
        )):
            response = self.client.post('/api/config/deepseek/model', json={'model': 'unknown-model'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(config_store.get('DEEPSEEK_CHAT_MODEL'), 'deepseek-flash')
        self.assertNotIn('deepseek-key-sentinel', response.get_data(as_text=True))

    def test_all_active_deepseek_callers_use_the_shared_runtime_setting(self):
        paths = (
            'app.py',
            'emotion_engine.py',
            'gateway.py',
            'tools/llm_lite.py',
            'tools/repair_agent.py',
            'tools/rolling_summary.py',
            'tools/cleaner.py',
            'tools/summary_title.py',
        )
        for relative_path in paths:
            with self.subTest(path=relative_path):
                source = (_ROOT / relative_path).read_text(encoding='utf-8')
                self.assertNotIn('deepseek-chat', source)
                self.assertIn('get_deepseek_chat_model', source)
                self.assertIn("'thinking': {'type': 'disabled'}", source)

    def test_codex_catalog_failure_does_not_write_runtime_setting(self):
        config_store.set('CODEX_CHAT_MODEL', 'existing-runtime-model')
        with patch.object(self.app_module.codex_app_server, 'runtime_status', return_value={'ready': True}), \
                patch.object(self.codex_client, 'list_models', side_effect=RuntimeError('catalog unavailable')):
            get_response = self.client.get('/api/group-chat/codex-models')
            post_response = self.client.post('/api/group-chat/codex-model', json={'model_id': 'catalog-id'})
        self.assertEqual(get_response.status_code, 502)
        self.assertEqual(post_response.status_code, 502)
        self.assertEqual(config_store.get('CODEX_CHAT_MODEL'), 'existing-runtime-model')

    def test_deepseek_server_error_does_not_write_or_leak_key(self):
        secret = 'deepseek-key-sentinel-upstream-error'
        error = urllib.error.HTTPError(
            'https://api.deepseek.com/models', 503, 'upstream unavailable', {}, None,
        )
        with patch.object(self.app_module, '_deployment_secret', return_value=secret), \
                patch('urllib.request.urlopen', side_effect=error):
            response = self.client.post('/api/config/deepseek/model', json={'model': 'deepseek-v4-pro'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()['detail'], 'upstream_error')
        self.assertEqual(config_store.get('DEEPSEEK_CHAT_MODEL'), 'deepseek-flash')
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_codex_null_restores_runtime_default(self):
        models = [{'id': 'catalog-default', 'model': 'gpt-6-default', 'is_default': True}]
        with patch.object(self.codex_client, 'list_models', return_value=models):
            response = self.client.post('/api/group-chat/codex-model', json={'model_id': None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config_store.get('CODEX_CHAT_MODEL'), '')
        self.assertEqual(response.get_json()['current'], 'gpt-6-default')
        self.assertEqual(response.get_json()['current_model_id'], 'catalog-default')

    def test_codex_models_include_effort_state(self):
        models = [{
            'id': 'catalog-alias',
            'model': 'gpt-5.6-sol',
            'label': 'GPT-5.6-Sol',
            'is_default': True,
            'default_effort': 'low',
            'efforts': ['low', 'high'],
            'input_modalities': ['text'],
        }]
        config_store.set('CODEX_CHAT_EFFORT', 'high')
        with patch.object(self.app_module.codex_app_server, 'runtime_status', return_value={'ready': True}), \
                patch.object(self.codex_client, 'configured_model', return_value='gpt-5.6-sol'), \
                patch.object(self.codex_client, 'list_models', return_value=models):
            response = self.client.get('/api/group-chat/codex-models')
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['configured_effort'], 'high')
        self.assertEqual(body['effort_mode'], 'explicit')
        self.assertEqual(body['allowed_efforts'], ['low', 'high'])

    def test_codex_effort_post_persists_and_clears(self):
        models = [{
            'id': 'catalog-alias',
            'model': 'gpt-5.6-sol',
            'is_default': True,
            'efforts': ['low', 'high'],
        }]
        with patch.object(self.app_module.codex_app_server, 'runtime_status', return_value={'ready': True}), \
                patch.object(self.codex_client, 'list_models', return_value=models):
            set_response = self.client.post('/api/group-chat/codex-effort', json={'effort': 'high'})
            self.assertEqual(set_response.status_code, 200)
            self.assertEqual(config_store.get('CODEX_CHAT_EFFORT'), 'high')
            self.assertEqual(set_response.get_json()['configured_effort'], 'high')
            clear_response = self.client.post('/api/group-chat/codex-effort', json={'effort': None})
        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(config_store.get('CODEX_CHAT_EFFORT'), '')
        self.assertEqual(clear_response.get_json()['effort_mode'], 'default')

    def test_codex_effort_rejects_unsupported_value(self):
        models = [{
            'id': 'catalog-alias',
            'model': 'gpt-5.6-sol',
            'is_default': True,
            'efforts': ['low', 'high'],
        }]
        config_store.set('CODEX_CHAT_EFFORT', 'low')
        with patch.object(self.app_module.codex_app_server, 'runtime_status', return_value={'ready': True}), \
                patch.object(self.codex_client, 'list_models', return_value=models):
            response = self.client.post('/api/group-chat/codex-effort', json={'effort': 'max'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['error'], 'CODEX_EFFORT_NOT_ALLOWED')
        self.assertEqual(config_store.get('CODEX_CHAT_EFFORT'), 'low')

    def test_deepseek_setting_is_read_by_real_summary_fallback_payload(self):
        secret = 'deepseek-key-sentinel-fallback'
        catalog_response = _FakeHTTPResponse(json.dumps({
            'data': [{'id': 'deepseek-flash'}, {'id': 'deepseek-v4-pro'}],
        }).encode())
        with patch.object(self.app_module, '_deployment_secret', return_value=secret), \
                patch('urllib.request.urlopen', return_value=catalog_response) as catalog_request:
            save_response = self.client.post('/api/config/deepseek/model', json={'model': 'deepseek-v4-pro'})
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(catalog_request.call_args.args[0].headers.get('Authorization'), 'Bearer ' + secret)
        self.assertNotIn(secret, save_response.get_data(as_text=True))
        self.assertEqual(config_store.get_deepseek_chat_model(), 'deepseek-v4-pro')

        gateway = self._gateway_module()
        from relay.manager import relay as diary_relay

        upstream_response = _FakeHTTPResponse(json.dumps({
            'choices': [{'message': {'content': 'summary'}}],
        }).encode())
        relay_error = urllib.error.HTTPError('relay', 401, 'unauthorized', {}, None)
        with patch.object(gateway, '_get_provider', return_value='api_relay'), \
                patch.object(diary_relay, 'call', side_effect=relay_error), \
                patch.object(gateway.urllib.request, 'urlopen', return_value=upstream_response) as upstream, \
                patch.dict(os.environ, {'DEEPSEEK_API_KEY': secret}):
            with gateway.app.test_request_context(
                '/api/summarize', method='POST', json={'prompt': 'a short prompt'},
            ):
                response = gateway.api_summarize()
        request_obj = upstream.call_args.args[0]
        payload = json.loads(request_obj.data)
        self.assertEqual(payload['model'], 'deepseek-v4-pro')
        self.assertEqual(payload['thinking'], {'type': 'disabled'})
        self.assertEqual(response.get_json(), {'ok': True, 'text': 'summary'})
        self.assertNotIn(secret, response.get_data(as_text=True))



class ClaudeRuntimeRouteTests(_AppRouteTestCase):
    def setUp(self):
        conn = sqlite3.connect(config_store.DB_PATH)
        conn.execute('DELETE FROM runtime_config')
        conn.commit()
        conn.close()
        config_store.set('CHAT_PROVIDER', 'claude_code')

    def test_runtime_get_is_read_only_and_secret_free(self):
        from chat import claude_runtime_state

        safe = {
            'active_version': '2.1.280',
            'last_good_version': '2.1.280',
            'candidate_version': None,
            'channel': 'latest',
            'auto_update': True,
            'status': 'healthy',
            'last_check_at': None,
            'last_promoted_at': None,
            'last_error': None,
            'minimum_version': '2.1.280',
        }
        with patch.object(claude_runtime_state, 'runtime_public_status', return_value=safe):
            response = self.client.get('/api/config/claude-runtime')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['active_version'], '2.1.280')
        text = response.get_data(as_text=True).lower()
        for forbidden in ('oauth', 'token', 'credential', '/root', 'home'):
            self.assertNotIn(forbidden, text)

    def test_runtime_post_validates_channel_and_only_accepts_allowed_fields(self):
        import contextlib

        with patch('tools.claude_runtime_updater.update_locks', return_value=contextlib.nullcontext(True)), \
             patch('tools.claude_runtime_updater.sync_native_update_settings') as sync, \
             patch('chat.claude_runtime_state.runtime_public_status', return_value={
                 'active_version': '2.1.280', 'last_good_version': '2.1.280',
                 'candidate_version': None, 'channel': 'stable', 'auto_update': True,
                 'status': 'healthy', 'minimum_version': '2.1.280',
             }):
            response = self.client.post('/api/config/claude-runtime', json={'channel': 'stable'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(config_store.get('CC_AUTO_UPDATE_CHANNEL'), 'stable')
            sync.assert_called_once_with(enabled=True, channel='stable')
            invalid = self.client.post('/api/config/claude-runtime', json={'channel': 'preview'})
            extra = self.client.post('/api/config/claude-runtime', json={'channel': 'latest', 'token': 'secret'})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(config_store.get('CC_AUTO_UPDATE_CHANNEL'), 'stable')
        self.assertEqual(sync.call_count, 1)

    def test_manual_check_uses_detached_zero_generation_worker(self):
        import subprocess

        with patch('chat.cc_runtime.service_home', return_value=Path(self.tmp.name)), \
             patch('subprocess.Popen') as popen:
            response = self.client.post('/api/config/claude-runtime/check', json={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()['model_generation_requests'], 0)
        argv = popen.call_args.args[0]
        self.assertTrue(argv[1].endswith('claude_runtime_updater.py'))
        self.assertEqual(argv[-1], '--manual')
        self.assertIs(popen.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

if __name__ == '__main__':
    unittest.main()
