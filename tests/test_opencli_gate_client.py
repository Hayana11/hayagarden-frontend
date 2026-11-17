import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import opencli_gate_client as client  # noqa: E402

TOKEN = 't' * 40


class _Handler(BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def log_message(self, *a):
        pass

    def _do(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n).decode() if n else ''
        type(self).seen.append((self.command, self.path, self.headers.get('Authorization'), body))
        if self.headers.get('Authorization') != 'Bearer ' + TOKEN:
            code, payload = 401, {'ok': False, 'error': 'unauthorized'}
        else:
            code, payload = type(self).routes.get((self.command, self.path), (404, {'ok': False, 'error': 'not_found'}))
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _do


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(('127.0.0.1', 0), _Handler)
        cls.url = 'http://127.0.0.1:%d' % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        _Handler.routes = {}
        _Handler.seen = []
        self.env = mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': self.url, 'OPENCLI_GATE_TOKEN': TOKEN})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_config_from_env_file(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.env', delete=False) as f:
            f.write('OTHER=1\nOPENCLI_GATE_URL="%s"\nOPENCLI_GATE_TOKEN=\'%s\'\n' % (self.url, TOKEN))
            path = f.name
        try:
            with mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': '', 'OPENCLI_GATE_TOKEN': ''}), \
                    mock.patch.object(client, 'ENV_PATH', path):
                _Handler.routes[('GET', '/status')] = (200, {'ok': True, 'doctor': 'fine'})
                self.assertIn('在线', client.status())
        finally:
            os.unlink(path)

    def test_misconfigured(self):
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': '', 'OPENCLI_GATE_TOKEN': ''}), \
                mock.patch.object(client, 'ENV_PATH', '/nonexistent/.env'):
            self.assertTrue(client.status().startswith('opencli_gate_misconfigured'))
            self.assertTrue(client.xhs('feed').startswith('opencli_gate_misconfigured'))

    def test_offline(self):
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': 'http://127.0.0.1:1'}):
            self.assertEqual(client.status(), client.OFFLINE_MSG)
            self.assertTrue(client.xhs('feed').startswith('computer_not_connected'))

    def test_auth_failed(self):
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_TOKEN': 'wrong'}):
            self.assertTrue(client.xhs('feed').startswith('opencli_gate_auth_failed'))
            self.assertTrue(client.status().startswith('opencli_gate_auth_failed'))

    def test_success_and_params(self):
        _Handler.routes[('POST', '/xhs')] = (200, {'ok': True, 'command': 'search', 'data': {'items': [{'title': '咖啡'}]}})
        out = client.xhs('search', query='咖啡', limit=3, url=None)
        self.assertIn('咖啡', out)
        self.assertNotIn('\\u', out)
        method, path, auth, body = _Handler.seen[-1]
        self.assertEqual(json.loads(body), {'command': 'search', 'query': '咖啡', 'limit': 3})
        self.assertEqual(auth, 'Bearer ' + TOKEN)

    def test_truncation(self):
        _Handler.routes[('POST', '/xhs')] = (200, {'ok': True, 'command': 'feed', 'data': {'t': 'x' * 40000}})
        out = client.xhs('feed')
        self.assertLessEqual(len(out), client.MAX_CHARS + 60)
        self.assertIn('已截断', out)

    def test_failure_mapping(self):
        cases = [
            ((200, {'ok': False, 'exit_code': 77, 'error': 'auth_required'}), '重新登录'),
            ((200, {'ok': False, 'exit_code': 69, 'error': 'bridge_down'}), '浏览器扩展没连上'),
            ((429, {'ok': False, 'error': 'busy'}), '稍后重试'),
            ((504, {'ok': False, 'error': 'timeout'}), '超时'),
            ((400, {'ok': False, 'error': 'query 不能以 - 开头'}), 'query'),
        ]
        for resp, text in cases:
            _Handler.routes[('POST', '/xhs')] = resp
            self.assertIn(text, client.xhs('feed'))

    def test_x_success_params_and_state(self):
        _Handler.routes[('POST', '/x')] = (200, {'ok': True, 'command': 'post', 'data': [{'url': 'https://x.com/a/status/1'}]})
        out = client.x('reply', text='你好', url='https://x.com/a/status/12345', limit=None)
        self.assertIn('https://x.com/a/status/1', out)
        method, path, auth, body = _Handler.seen[-1]
        self.assertEqual((method, path), ('POST', '/x'))
        self.assertEqual(json.loads(body), {'command': 'reply', 'text': '你好', 'url': 'https://x.com/a/status/12345'})
        _Handler.routes[('GET', '/x/state')] = (200, {'ok': True, 'day': '2026-01-01', 'posts': {'count': 1, 'limit': 5}, 'recent': [{'text': '嗨'}]})
        out = client.x_state()
        data = json.loads(out)
        self.assertEqual(data['posts'], {'count': 1, 'limit': 5})
        self.assertNotIn('ok', data)
        self.assertIn('嗨', out)

    def test_x_failure_mapping(self):
        cases = [
            ((429, {'ok': False, 'error': 'x_daily_limit'}), '今天的发推/回复额度用完了'),
            ((429, {'ok': False, 'error': 'x_write_too_soon', 'retry_after_ms': 30000}), '两次发言间隔太短，稍后再发'),
            ((409, {'ok': False, 'error': 'x_duplicate'}), '这句话 24 小时内发过了'),
            ((200, {'ok': False, 'exit_code': 77, 'error': 'auth_required'}), '推特账号需要重新登录'),
            ((504, {'ok': False, 'error': 'timeout'}), '推特命令超时'),
            ((400, {'ok': False, 'error': 'text 不能为空'}), 'text'),
        ]
        for resp, text in cases:
            _Handler.routes[('POST', '/x')] = resp
            self.assertIn(text, client.x('post', text='t'))
        _Handler.routes[('POST', '/x')] = cases[1][0]
        self.assertIn('31 秒', client.x('post', text='t'))
        # the xhs channel keeps its own wording
        _Handler.routes[('POST', '/xhs')] = (200, {'ok': False, 'exit_code': 77, 'error': 'auth_required'})
        self.assertIn('小红书账号需要重新登录', client.xhs('feed'))

    def test_x_offline_misconfigured_auth_truncation(self):
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': 'http://127.0.0.1:1'}):
            self.assertEqual(client.x('timeline'), client.OFFLINE_MSG)
            self.assertEqual(client.x_state(), client.OFFLINE_MSG)
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_TOKEN': 'wrong'}):
            self.assertTrue(client.x('timeline').startswith('opencli_gate_auth_failed'))
            self.assertTrue(client.x_state().startswith('opencli_gate_auth_failed'))
        with mock.patch.dict(os.environ, {'OPENCLI_GATE_URL': '', 'OPENCLI_GATE_TOKEN': ''}), \
                mock.patch.object(client, 'ENV_PATH', '/nonexistent/.env'):
            self.assertTrue(client.x('timeline').startswith('opencli_gate_misconfigured'))
            self.assertTrue(client.x_state().startswith('opencli_gate_misconfigured'))
        _Handler.routes[('POST', '/x')] = (200, {'ok': True, 'command': 'timeline', 'data': {'t': 'x' * 40000}})
        out = client.x('timeline')
        self.assertLessEqual(len(out), client.MAX_CHARS + 60)
        self.assertIn('已截断', out)

    def test_status_unhealthy(self):
        _Handler.routes[('GET', '/status')] = (200, {'ok': False, 'doctor': 'extension missing'})
        out = client.status()
        self.assertIn('opencli_gate_unhealthy', out)
        self.assertIn('extension missing', out)


try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:
    HAVE_FLASK = False


class IntegrationTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_FLASK, 'gateway.py needs flask')
    def test_gateway_dispatch(self):
        import gateway
        with mock.patch.object(client, 'status', return_value='STATUS') as st, \
                mock.patch.object(client, 'xhs', return_value='XHS') as xh:
            self.assertEqual(gateway.run_tool('xhs_status', {}), 'STATUS')
            self.assertEqual(
                gateway.run_tool('xhs_read', {'command': 'search', 'query': 'q', 'limit': 2}), 'XHS')
        st.assert_called_once()
        xh.assert_called_once_with('search', query='q', limit=2)

    @unittest.skipUnless(HAVE_FLASK, 'gateway.py needs flask')
    def test_gateway_schemas_and_cc_allowed(self):
        import gateway
        names = {t['name'] for t in gateway.TOOLS}
        self.assertTrue({'xhs_status', 'xhs_read'} <= names)
        schema = next(t for t in gateway.TOOLS if t['name'] == 'xhs_read')['input_schema']
        self.assertEqual(schema['properties']['command']['enum'], ['feed', 'search', 'note', 'comments', 'user'])
        allowed = gateway.CC_ALLOWED_TOOLS.split(',')
        self.assertIn('mcp__home__xhs_status', allowed)
        self.assertIn('mcp__home__xhs_read', allowed)

    @unittest.skipUnless(HAVE_FLASK, 'gateway.py needs flask')
    def test_gateway_x_dispatch(self):
        import gateway
        with mock.patch.object(client, 'x', return_value='X') as xx, \
                mock.patch.object(client, 'x_state', return_value='XS') as xs:
            self.assertEqual(gateway.run_tool('x_read', {'command': 'search', 'query': 'q', 'from': '@a', 'product': 'top', 'limit': 2}), 'X')
            xx.assert_called_once_with('search', query='q', **{'from': '@a'}, product='top', limit=2)
            xx.reset_mock()
            self.assertEqual(gateway.run_tool('x_post', {'command': 'reply', 'text': 't', 'url': 'u'}), 'X')
            xx.assert_called_once_with('reply', text='t', url='u')
            xx.reset_mock()
            self.assertIn('只支持', gateway.run_tool('x_post', {'command': 'like', 'text': 't'}))
            xx.assert_not_called()
            self.assertEqual(gateway.run_tool('x_state', {}), 'XS')

    @unittest.skipUnless(HAVE_FLASK, 'gateway.py needs flask')
    def test_gateway_x_schemas_match_cc_surface_and_mcp_server(self):
        import gateway
        from tools import cc_tool_surface
        import re
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mcp-http-server.js'), encoding='utf-8') as f:
            mcp_src = f.read()
        for name in ('x_state', 'x_read', 'x_post'):
            gw = next(t for t in gateway.TOOLS if t['name'] == name)['input_schema']
            cc = cc_tool_surface._HOME_TOOL_SCHEMAS['mcp__home__' + name]
            self.assertEqual(set(gw.get('properties', {})), set(cc.get('properties', {})), name)
            self.assertEqual(gw.get('required', []), cc.get('required', []), name)
            for key, prop in gw.get('properties', {}).items():
                self.assertEqual(prop.get('enum'), cc['properties'][key].get('enum'), (name, key))
            block = re.search(r"'%s',\s*\{(.*?)\n    \},?\n" % name, mcp_src, re.S)
            if gw.get('properties'):
                self.assertIsNotNone(block, name)
                for key in gw['properties']:
                    self.assertRegex(block.group(1), r'\b%s:' % key, (name, key))
        allowed = gateway.CC_ALLOWED_TOOLS.split(',')
        for t in ('mcp__home__x_read', 'mcp__home__x_post', 'mcp__home__x_state'):
            self.assertIn(t, allowed)
        for t in ('mcp__home__x_like', 'mcp__home__x_delete'):
            self.assertNotIn(t, allowed)
        x_post = next(t for t in gateway.TOOLS if t['name'] == 'x_post')
        self.assertEqual(x_post['input_schema']['properties']['command']['enum'], ['post', 'reply'])
        self.assertNotIn('image', json.dumps(x_post['input_schema']))

    def test_x_drawer_and_inventory(self):
        import tool_drawers
        from tools import tool_inventory
        self.assertEqual(tool_drawers.DRAWERS['x']['label'], '推特')
        self.assertEqual(tool_drawers.DRAWERS['x']['tools'], ['x_state', 'x_read', 'x_post'])

        def drawers_for(text):
            hit = set()
            for rx, ids in tool_drawers.FORCE_RULES:
                if rx.search(text):
                    hit.update(ids)
            return hit
        for text in ('帮我看看推特', '去 Twitter 上逛逛', '发推吧', '那条推文', '在 X 上发一条', 'X上有什么'):
            self.assertIn('x', drawers_for(text), text)
        for text in ('Xbox 好玩', 'next example', 'tax', 'x86', '今天天气不错', 'boxy'):
            self.assertNotIn('x', drawers_for(text), text)
        grp = next(g for g in tool_inventory.GROUP_DEFS if g[0] == 'x')
        self.assertEqual(grp[1], '推特')
        self.assertEqual(grp[2], ['x_state', 'x_read', 'x_post'])
        for t in grp[2]:
            self.assertIn(t, tool_inventory.DISPLAY_LABELS)
        self.assertEqual(tool_inventory._LEGACY_CAPABILITY_ALIASES['x.write'], 'x_post')

    def test_drawer_and_inventory(self):
        import tool_drawers
        from tools import tool_inventory
        self.assertEqual(tool_drawers.DRAWERS['xhs']['tools'], ['xhs_status', 'xhs_read'])
        rule = next(ids for rx, ids in tool_drawers.FORCE_RULES if rx.search('小红书'))
        self.assertIn('xhs', rule)
        self.assertIn('pocket', rule)
        grp = next(g for g in tool_inventory.GROUP_DEFS if g[0] == 'xhs')
        self.assertEqual(grp[2], ['xhs_status', 'xhs_read'])
        self.assertIn('xhs_read', tool_inventory.DISPLAY_LABELS)


if __name__ == '__main__':
    unittest.main()
