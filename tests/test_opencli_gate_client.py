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
