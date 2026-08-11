"""LIGHT-LEAN-R1: Wake read-only lights, no auto state injection, power-only status."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

ROOT = '/agent/repos/hayagarden-frontend'
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from wake.cc_tools import (
    CC_WAKE_CAPABILITY_TEXT,
    CC_WAKE_WRITE_MCP,
    cc_wake_allowed_tools,
    cc_wake_nudge_text,
)


class WakeLightAllowlistTests(unittest.TestCase):
    def test_wake_allowlist_read_only_lights(self):
        csv = cc_wake_allowed_tools(None)
        parts = set(csv.split(','))
        self.assertIn('mcp__home__get_light_status', parts)
        self.assertNotIn('mcp__home__light_on', parts)
        self.assertNotIn('mcp__home__light_off', parts)
        self.assertNotIn('mcp__home__light_bedside_warm', parts)
        self.assertNotIn('mcp__home__light_bedside_neutral', parts)
        for mcp in ('mcp__home__light_on', 'mcp__home__light_off'):
            self.assertNotIn(mcp, CC_WAKE_WRITE_MCP)

    def test_wake_capability_text_no_write_lights(self):
        self.assertIn('get_light_status', CC_WAKE_CAPABILITY_TEXT)
        self.assertNotIn('可写可用：light_on', CC_WAKE_CAPABILITY_TEXT)
        self.assertNotIn('light_bedside_warm', CC_WAKE_CAPABILITY_TEXT.split('可写')[0])
        self.assertIn('不得调用 light_on', CC_WAKE_CAPABILITY_TEXT)

    def test_wake_nudge_no_write_lights(self):
        text = cc_wake_nudge_text(0.5, ['get_light_status'])
        self.assertIn('get_light_status', text)
        self.assertIn('不可改灯', text)
        self.assertNotIn('灯控（on/off', text)


class RelayWakeToolsLightContractTests(unittest.TestCase):
    def test_wake_tools_read_only_light_status_contract(self):
        from gateway import WAKE_TOOLS

        names = [str(t.get('name') or '') for t in WAKE_TOOLS]
        self.assertIn('get_light_status', names)
        for forbidden in ('light_on', 'light_off', 'light_warm', 'light_neutral'):
            self.assertNotIn(forbidden, names)

        desc = next(t['description'] for t in WAKE_TOOLS if t['name'] == 'get_light_status')
        self.assertIn('只读', desc)
        self.assertIn('power', desc)
        self.assertIn('不能修改灯', desc)
        self.assertIn('不读取亮度或色温', desc)
        self.assertNotIn('暖光', desc)
        self.assertNotIn('中性光', desc)
        self.assertNotIn('远程帮她调', desc)
        self.assertNotIn('决定要不要', desc)
        self.assertNotIn('色温档位', desc)


class NoAutoLightInjectionTests(unittest.TestCase):
    def test_build_system_does_not_fetch_light_status(self):
        from chat import system_builder

        shared = types.SimpleNamespace(
            persona='PERSONA',
            relationship_context='',
            relationship_fingerprint='x',
        )

        def get_bool(key, default=False):
            return False

        gateway_stub = types.ModuleType('gateway')
        gateway_stub.get_db = lambda: mock.MagicMock(
            execute=mock.MagicMock(return_value=mock.MagicMock(fetchall=lambda: [])),
            close=mock.MagicMock(),
        )

        with mock.patch.dict(sys.modules, {'gateway': gateway_stub}), \
             mock.patch.object(system_builder, 'build_shared_context', return_value=shared), \
             mock.patch.object(system_builder, 'read_persona', return_value='PERSONA'), \
             mock.patch.object(system_builder, '_ombre_handoff_sync', return_value=''), \
             mock.patch.object(system_builder.config_store, 'get_bool', side_effect=get_bool), \
             mock.patch('urllib.request.urlopen') as urlopen_mock:
            blocks = system_builder.build_system(
                wake=True,
                include_relationship_context=False,
                allow_side_effects=False,
                capability_profile='cc_wake',
            )
            urlopen_mock.assert_not_called()
        flat = '\n'.join(b.get('text', '') for b in blocks if isinstance(b, dict))
        self.assertNotIn('灯·当前状态', flat)

    def test_cc_collect_state_does_not_query_lights(self):
        from chat.system_builder import _cc_collect_state

        with mock.patch('urllib.request.urlopen') as urlopen_mock, \
             mock.patch('config_store.get_bool', return_value=False):
            state = _cc_collect_state(lambda: mock.MagicMock(), lean=True)
        urlopen_mock.assert_not_called()
        self.assertEqual(state.get('lights'), '')
        self.assertNotIn('time_bucket', state)


class PowerOnlyStatusTests(unittest.TestCase):
    def _import_light_control(self):
        import sys
        sys.modules['mijiaAPI'] = mock.MagicMock()
        if 'tools.light_control' in sys.modules:
            del sys.modules['tools.light_control']
        import tools.light_control as lc
        return lc

    def test_light_status_queries_power_only_per_zone(self):
        lc = self._import_light_control()

        captured_queries: list[list[dict]] = []

        class FakeAPI:
            def get_devices_prop(self, query):
                captured_queries.append(list(query))
                return [{"value": True} for _ in query]

        with mock.patch.object(lc, '_api', return_value=FakeAPI()), \
             mock.patch.object(lc, '_load_config', return_value={
                 'prop_map': {
                     'power': {'siid': 2, 'piid': 1},
                     'brightness': {'siid': 2, 'piid': 2},
                     'color_temp': {'siid': 2, 'piid': 3},
                 },
                 'zones': {
                     'main': {'supported_query_props': ['power']},
                     'bedside': {'supported_query_props': ['power']},
                 },
             }):
            lc.light_status('did-main', zone='main')
            lc.light_status('did-bed', zone='bedside')

        self.assertEqual(len(captured_queries), 2)
        for query in captured_queries:
            self.assertEqual(len(query), 1)
            self.assertEqual(query[0]['siid'], 2)
            self.assertEqual(query[0]['piid'], 1)
        prop_names = set()
        for query in captured_queries:
            for item in query:
                prop_names.add((item['siid'], item['piid']))
        self.assertEqual(prop_names, {(2, 1)})

    def test_control_functions_still_use_brightness_and_color_temp(self):
        lc = self._import_light_control()

        calls: list[tuple] = []

        class FakeAPI:
            def set_devices_prop(self, payload):
                calls.append((payload['siid'], payload['piid'], payload['value']))
                return payload

        with mock.patch.object(lc, '_api', return_value=FakeAPI()), \
             mock.patch.object(lc, '_prop', side_effect=lambda name: {
                 'power': {'siid': 2, 'piid': 1},
                 'brightness': {'siid': 2, 'piid': 2},
                 'color_temp': {'siid': 2, 'piid': 3},
             }[name]):
            lc.set_brightness('did', 40)
            lc.set_color_temp('did', 4000)

        self.assertEqual(calls, [(2, 2, 40), (2, 3, 4000)])


if __name__ == '__main__':
    unittest.main()
