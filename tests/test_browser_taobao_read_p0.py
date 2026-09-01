from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from tools.execution_fence import capability_for_tool, evaluate_tool_call
from tools.lease_signer import issue_turn_lease
from tools.shopping_taobao_read_adapter import (
    DAEMON_RESPONSE_MAX_BYTES,
    read_taobao_page,
)


class _Response:
    def __init__(self, body, headers=None):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.body = body
        self.headers = headers or {}
        self.read_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, amount=-1):
        self.read_calls.append(amount)
        return self.body if amount < 0 else self.body[:amount]


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

    def test_https_allowlist_rejects_http_credentials_and_spoofing(self):
        for url in (
            "https://www.taobao.com/",
            "https://detail.tmall.com/item.htm?id=1",
        ):
            response = _Response({"ok": True, "url": url, "text": "商品正文"})
            with mock.patch(
                "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
                return_value=response,
            ) as mocked:
                result = read_taobao_page(url)
            self.assertEqual(result["status"], "OK")
            self.assertEqual(result["text"], "商品正文")
            request = mocked.call_args.args[0]
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body, {"site": "taobao", "url": url})
            self.assertTrue(request.full_url.endswith("/browse"))
            self.assertEqual(response.read_calls, [DAEMON_RESPONSE_MAX_BYTES + 1])

        for url in (
            "http://www.taobao.com/",
            "https://user:pass@www.taobao.com/",
            "https://example.com/",
            "https://taobao.com.evil.example/",
            "https://localhost/",
            "https://127.0.0.1/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                read_taobao_page(url)

    def test_daemon_final_url_is_revalidated_before_text_return(self):
        for final_url in (
            "https://evil.example/",
            "https://localhost/",
        ):
            with self.subTest(final_url=final_url):
                with mock.patch(
                    "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
                    return_value=_Response(
                        {
                            "ok": True,
                            "finalUrl": final_url,
                            "text": "SECRET PAGE TEXT MUST NOT ESCAPE",
                        }
                    ),
                ):
                    with self.assertRaises(ValueError):
                        read_taobao_page("https://www.taobao.com/")
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            return_value=_Response(
                {
                    "ok": True,
                    "finalUrl": "https://detail.tmall.com/item.htm?id=1",
                    "text": "商品正文",
                }
            ),
        ):
            result = read_taobao_page("https://www.taobao.com/")
        self.assertEqual(result["url"], "https://detail.tmall.com/item.htm?id=1")
        self.assertEqual(result["text"], "商品正文")

    def test_daemon_response_exact_limit_passes_and_limit_plus_one_fails(self):
        prefix = b'{"ok":true,"url":"https://www.taobao.com/","text":"ok"}'
        exact = _Response(
            prefix + b" " * (DAEMON_RESPONSE_MAX_BYTES - len(prefix)),
            {"Content-Length": str(DAEMON_RESPONSE_MAX_BYTES)},
        )
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            return_value=exact,
        ):
            result = read_taobao_page("https://www.taobao.com/")
        self.assertEqual(result["text"], "ok")
        self.assertEqual(exact.read_calls, [DAEMON_RESPONSE_MAX_BYTES + 1])

        oversized = _Response(
            prefix + b" " * (DAEMON_RESPONSE_MAX_BYTES + 1 - len(prefix)),
            {"Content-Length": str(DAEMON_RESPONSE_MAX_BYTES + 1)},
        )
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            return_value=oversized,
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                read_taobao_page("https://www.taobao.com/")
        self.assertEqual(oversized.read_calls, [])

        unknown_length = _Response(
            prefix + b" " * (DAEMON_RESPONSE_MAX_BYTES + 1 - len(prefix))
        )
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            return_value=unknown_length,
        ):
            with self.assertRaisesRegex(RuntimeError, "exceeds"):
                read_taobao_page("https://www.taobao.com/")
        self.assertEqual(unknown_length.read_calls, [DAEMON_RESPONSE_MAX_BYTES + 1])

    def test_huge_http_error_body_is_bounded(self):
        error_body = b"e" * (DAEMON_RESPONSE_MAX_BYTES + 1)
        error = __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "http://127.0.0.1:8787/browse",
            502,
            "bad gateway",
            {"Content-Length": str(len(error_body))},
            io.BytesIO(error_body),
        )
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            side_effect=error,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                read_taobao_page("https://www.taobao.com/")
        self.assertEqual(error.fp.tell(), 0)

    def test_model_text_is_bounded_and_daemon_failure_never_falls_back(self):
        response = _Response(
            {"ok": True, "url": "https://www.taobao.com/", "text": "x" * 9000}
        )
        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            return_value=response,
        ):
            result = read_taobao_page("https://www.taobao.com/")
        self.assertEqual(len(result["text"]), 8000)

        with mock.patch(
            "tools.shopping_taobao_read_adapter.urllib.request.urlopen",
            side_effect=OSError("connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "shop daemon unavailable"):
                read_taobao_page("https://www.taobao.com/")


if __name__ == "__main__":
    unittest.main()
