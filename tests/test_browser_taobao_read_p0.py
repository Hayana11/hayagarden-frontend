from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from tools.execution_fence import capability_for_tool, evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.shopping_taobao_read_adapter import read_taobao_page


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class BrowserTaobaoReadP0Tests(unittest.TestCase):
    def test_browser_tool_is_web_read_and_chat_auto_allowed(self):
        tool = "mcp__browser__taobao_read"
        self.assertEqual(capability_for_tool(tool), "web.read")
        lease = issue_turn_lease(
            turn_id="browser-read-1",
            turn_mode="chat",
            issued_from="default_policy",
        )
        result = evaluate_tool_call(tool, {"url": "https://www.taobao.com/"}, lease)
        self.assertEqual(result["lease_decision"], "ALLOW")
        self.assertEqual(result["capability_id"], "web.read")

    def test_adapter_accepts_only_taobao_and_tmall(self):
        for url in (
            "https://www.taobao.com/",
            "https://detail.tmall.com/item.htm?id=1",
        ):
            with mock.patch(
                "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
                return_value=_Response({"ok": True, "url": url, "text": "商品正文"}),
            ) as mocked:
                result = read_taobao_page(url)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["text"], "商品正文")
            request = mocked.call_args.args[0]
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body, {"site": "taobao", "url": url})
            self.assertTrue(request.full_url.endswith("/browse"))

        for url in (
            "https://example.com/",
            "javascript:alert(1)",
            "https://taobao.com.evil.example/",
        ):
            with self.assertRaises(ValueError):
                read_taobao_page(url)

    def test_daemon_failure_stays_visible_and_never_falls_back(self):
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "shop daemon unavailable"):
                read_taobao_page("https://www.taobao.com/")


if __name__ == "__main__":
    unittest.main()
