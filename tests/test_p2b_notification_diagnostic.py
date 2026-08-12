import re
import unittest
from pathlib import Path


REPAIR = Path(__file__).resolve().parents[1] / 'static' / 'repair.html'
SW = Path(__file__).resolve().parents[1] / 'static' / 'sw.js'


class P2BNotificationDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = REPAIR.read_text(encoding='utf-8')
        cls.html = cls._between(
            cls.source,
            '<!-- P2B_NOTIFICATION_DIAG_BEGIN -->',
            '<!-- P2B_NOTIFICATION_DIAG_END -->',
        )
        cls.js = cls._between(
            cls.source,
            '// P2B_NOTIFICATION_DIAG_JS_BEGIN',
            '// P2B_NOTIFICATION_DIAG_JS_END',
        )

    @staticmethod
    def _between(source, begin, end):
        start = source.index(begin)
        stop = source.index(end, start)
        return source[start:stop + len(end)]

    @staticmethod
    def _function_body(source, name):
        match = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', source)
        if not match:
            raise AssertionError('missing function: ' + name)
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            if source[index] == '{':
                depth += 1
            elif source[index] == '}':
                depth -= 1
            index += 1
        if depth:
            raise AssertionError('unterminated function: ' + name)
        return source[match.start():index]

    def test_markers_and_card_placement(self):
        self.assertLess(self.source.index('Elpis Canary · NativeBridge Lite'),
                        self.source.index('<!-- P2B_NOTIFICATION_DIAG_BEGIN -->'))
        self.assertLess(self.source.index('<!-- P2B_NOTIFICATION_DIAG_END -->'),
                        self.source.index('<!-- 生成锁急救 -->'))
        self.assertIn('原生通知诊断', self.html)
        self.assertIn('Elpis Canary · P2B Native Notifications', self.html)
        self.assertIn('通知桥', self.html)
        self.assertIn('通知能力', self.html)
        self.assertIn('测试通知仅在本机显示，不访问消息服务器。', self.html)
        self.assertRegex(self.html, r'id="notification-diag-result"></div>')

    def test_bridge_contract_and_browser_fallback(self):
        self.assertIn('window.ElpisNotifications', self.js)
        for method in ('hasNotificationPermission', 'requestNotificationPermission', 'showTestNotification'):
            self.assertIn("typeof bridge." + method + " === 'function'", self.js)
        self.assertIn('仅 App 可用', self.js)
        self.assertIn("'badge-warn'", self.js)
        self.assertRegex(self.html, r'id="notification-diag-request"[^>]*disabled')
        self.assertRegex(self.html, r'id="notification-diag-test"[^>]*disabled')

    def test_only_permission_probe_is_automatic(self):
        refresh = self._function_body(self.js, 'refreshNotificationDiagnostic')
        self.assertIn('bridge.hasNotificationPermission()', refresh)
        self.assertNotIn('bridge.requestNotificationPermission()', refresh)
        self.assertNotIn('bridge.showTestNotification()', refresh)
        before_request = self.js[:self.js.index('function requestNotificationAccess')]
        self.assertNotIn('bridge.requestNotificationPermission()', before_request)
        self.assertNotIn('bridge.showTestNotification()', before_request)
        self.assertIn("window.addEventListener('focus'", self.js)
        self.assertIn("document.addEventListener('visibilitychange'", self.js)

    def test_click_handlers_are_local_native_calls(self):
        request = self._function_body(self.js, 'requestNotificationAccess')
        diagnostic = self._function_body(self.js, 'sendNotificationDiagnosticTest')
        self.assertIn('bridge.requestNotificationPermission()', request)
        self.assertIn('已请求，正在重新检查…', request)
        self.assertIn('setTimeout', request)
        self.assertIn('bridge.showTestNotification()', diagnostic)
        self.assertIn('已发送，请查看通知栏', diagnostic)
        self.assertIn('当前无法发送通知', diagnostic)
        self.assertIn('调用失败', diagnostic)
        self.assertIn('onclick="requestNotificationAccess()"', self.html)
        self.assertIn('onclick="sendNotificationDiagnosticTest()"', self.html)

    def test_p2b_block_has_no_remote_or_persistent_state(self):
        combined = self.html + self.js
        for forbidden in (
            'pending_notification', '/api/wake_log', 'fetch(', 'XMLHttpRequest',
            'sendBeacon', 'WebSocket', 'EventSource', 'localStorage',
            'sessionStorage', 'indexedDB',
        ):
            self.assertNotIn(forbidden, combined)

    def test_chrome78_compatibility_and_p2a_regression_guard(self):
        for forbidden in ('?.', '??', '??=', '&&=', '||=', '.at(', 'replaceAll',
                          'Promise.any', 'structuredClone', 'Object.hasOwn',
                          'crypto.randomUUID'):
            self.assertNotIn(forbidden, self.js)
        self.assertIn('Elpis Canary · NativeBridge Lite', self.source)
        self.assertIn('window.ElpisNative', self.source)
        self.assertTrue(SW.is_file())


if __name__ == '__main__':
    unittest.main()
