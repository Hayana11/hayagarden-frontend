import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "relay" / "channel_intelligence.py"
SPEC = importlib.util.spec_from_file_location("channel_intelligence", MODULE_PATH)
channel_intelligence = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(channel_intelligence)

_guess_status_origins = channel_intelligence._guess_status_origins
_pick_status = channel_intelligence._pick_status
inspect_channel = channel_intelligence.inspect_channel
models_url_from_api_url = channel_intelligence.models_url_from_api_url
origin_from_url = channel_intelligence.origin_from_url
query_channel_balance = channel_intelligence.query_channel_balance
query_channel_account_balance = channel_intelligence.query_channel_account_balance
query_channel_daily_costs = channel_intelligence.query_channel_daily_costs


class ChannelIntelligenceTests(unittest.TestCase):
    def test_normalizes_origin_and_models_url(self):
        self.assertEqual(origin_from_url("relay.example.com/v1"), "https://relay.example.com")
        self.assertEqual(
            models_url_from_api_url("relay.example.com/v1/messages"),
            "https://relay.example.com/v1/models",
        )
        self.assertEqual(
            models_url_from_api_url("https://relay.example.com/v1"),
            "https://relay.example.com/v1/models",
        )

    def test_aggregates_models_and_group_adjusted_pricing_without_exposing_key(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/api/pricing"):
                return {
                    "group_ratio": {"小猫组": 0.5},
                    "data": [{
                        "model_name": "claude-sonnet-4",
                        "model_ratio": 1.5,
                        "completion_ratio": 5,
                        "enable_groups": ["小猫组"],
                    }],
                }, None
            if url.endswith("/v1/models"):
                return {"data": [{"id": "claude-sonnet-4"}]}, None
            return None, {"status": 404}

        result = inspect_channel(
            {
                "id": 7,
                "name": "小猫组",
                "base_url": "https://relay.example.com/v1/messages",
                "api_key": "secret-key",
            },
            include_status=False,
            force=True,
            request_json=fake_request,
        )

        self.assertEqual(result["models"], ["claude-sonnet-4"])
        model = result["model_options"][0]
        self.assertEqual(model["price"], "输入 $1.5/M / 输出 $7.5/M")
        self.assertEqual(model["group"], "小猫组")
        self.assertNotIn("api_key", result)
        self.assertNotIn("secret-key", repr(result))
        model_call = next(call for call in calls if call[1].endswith("/v1/models"))
        self.assertEqual(model_call[2]["headers"]["Authorization"], "Bearer secret-key")

    def test_status_matching_is_exact_and_prefers_full_routed_id(self):
        statuses = [
            {"name": "claude-sonnet-4", "group": "fallback", "status": 0},
            {"name": "[route-a]claude-sonnet-4", "group": "route-a", "status": 1},
            {"name": "claude-sonnet-4-old", "group": "wrong", "status": 1},
        ]
        selected = _pick_status(
            {"name": "route-a", "base_url": "https://relay.example.com/v1"},
            "[route-a]claude-sonnet-4",
            None,
            statuses,
        )
        self.assertEqual(selected["group"], "route-a")
        self.assertIsNone(_pick_status({}, "claude-sonnet-5", None, statuses))

    def test_bad_optional_status_url_does_not_hide_models_and_prices(self):
        def fake_request(method, url, **kwargs):
            if url.endswith("/api/pricing"):
                return {"data": []}, None
            if url.endswith("/v1/models"):
                return {"data": [{"id": "gpt-5"}]}, None
            return None, {"status": 404}

        result = inspect_channel(
            {
                "id": 8,
                "name": "demo",
                "base_url": "https://relay.example.com/v1",
                "status_url": "not a valid status url",
            },
            include_status=True,
            force=True,
            request_json=fake_request,
        )
        self.assertEqual(result["models"], ["gpt-5"])
        self.assertIsNone(result["status_source"])

    def test_guesses_status_subdomain_for_api2_and_apex_hosts(self):
        self.assertIn(
            "https://status.68886868.xyz",
            _guess_status_origins("https://api2.68886868.xyz"),
        )
        self.assertIn(
            "https://status.68886868.xyz",
            _guess_status_origins("https://68886868.xyz"),
        )
        self.assertIn(
            "https://status.treegpt.cc",
            _guess_status_origins("https://api.treegpt.cc"),
        )

    def test_retries_pricing_with_console_access_token_when_anonymous_auth_required(self):
        calls = []

        def fake_request(method, url, **kwargs):
            headers = kwargs.get("headers") or {}
            calls.append((url, headers))
            if url.endswith("/api/pricing"):
                if headers.get("New-Api-User") == "1834":
                    return {
                        "success": True,
                        "data": [{
                            "model_name": "claude-opus-4-6",
                            "quota_type": 1,
                            "model_price": 0.13,
                            "enable_groups": ["vip"],
                        }],
                    }, None
                return None, {"status": 401, "auth_required": True}
            if url.endswith("/v1/models"):
                return {"data": [{"id": "claude-opus-4-6"}]}, None
            return None, {"status": 404}

        result = inspect_channel(
            {
                "id": 2,
                "name": "tree",
                "base_url": "https://api.treegpt.cc/v1/messages",
                "api_key": "sk-relay",
            },
            include_status=False,
            force=True,
            request_json=fake_request,
            console_credential_kind="access_token",
            console_credential_secret="console-access-token",
            console_user_id="1834",
        )

        self.assertFalse(result["pricing_requires_auth"])
        self.assertEqual(result["model_options"][0]["price"], "$0.13/次")
        pricing_calls = [headers for url, headers in calls if url.endswith("/api/pricing")]
        self.assertEqual(len(pricing_calls), 2)
        self.assertNotIn("New-Api-User", pricing_calls[0])
        self.assertEqual(pricing_calls[1]["Authorization"], "console-access-token")
        self.assertEqual(pricing_calls[1]["New-Api-User"], "1834")
        self.assertNotIn("console-access-token", repr(result))

    def test_discovers_uptime_kuma_on_guessed_status_host(self):
        def fake_request(method, url, **kwargs):
            if url.endswith("/api/pricing"):
                return {"data": []}, None
            if url.endswith("/v1/models"):
                return {"data": [{"id": "claude-opus-4-6"}]}, None
            if url == "https://status.68886868.xyz/api/status-page/api":
                return {
                    "publicGroupList": [{
                        "name": "Claude",
                        "monitorList": [{"id": 1, "name": "claude-opus-4-6"}],
                    }],
                }, None
            if url == "https://status.68886868.xyz/api/status-page/heartbeat/api":
                return {
                    "heartbeatList": {
                        "1": [{"status": 1, "time": "2026-07-17", "msg": "", "ping": 42}],
                    },
                }, None
            return None, {"status": 404}

        result = inspect_channel(
            {
                "id": 5,
                "name": "小鸡农场",
                "base_url": "https://api2.68886868.xyz/v1/messages",
                "api_key": "sk-relay",
            },
            include_status=True,
            force=True,
            request_json=fake_request,
        )
        self.assertEqual(result["status_source"], "https://status.68886868.xyz/status/api")
        self.assertEqual(result["model_options"][0]["status"]["status"], 1)

    def test_queries_newapi_key_balance_and_converts_quota_to_usd(self):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update({"method": method, "url": url, "headers": kwargs.get("headers")})
            return {
                "code": 1,
                "data": {
                    "name": "daily-chat",
                    "unlimited_quota": False,
                    "total_granted": 2_500_000,
                    "total_used": 750_000,
                    "total_available": 1_750_000,
                    "expires_at": 0,
                },
            }, None

        result = query_channel_balance(
            {"base_url": "https://relay.example.com/v1", "api_key": "secret-key"},
            request_json=fake_request,
        )
        self.assertTrue(result["supported"])
        self.assertEqual(result["total_granted_usd"], 5.0)
        self.assertEqual(result["total_used_usd"], 1.5)
        self.assertEqual(result["total_available_usd"], 3.5)
        self.assertEqual(seen["url"], "https://relay.example.com/api/usage/token/")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer secret-key")
        self.assertNotIn("secret-key", repr(result))

    def test_balance_reports_unsupported_without_guessing(self):
        result = query_channel_balance(
            {"base_url": "https://relay.example.com/v1", "api_key": "secret-key"},
            request_json=lambda *args, **kwargs: (None, {"status": 404}),
        )
        self.assertFalse(result["supported"])
        self.assertEqual(result["error"], "unsupported")

    def test_queries_newapi_account_balance_with_revocable_access_token(self):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update({"method": method, "url": url, "headers": kwargs.get("headers")})
            return {
                "success": True,
                "data": {
                    "quota": 10_000_000,
                    "used_quota": 2_000_000,
                },
            }, None

        result = query_channel_account_balance(
            {"base_url": "https://relay.example.com/v1/messages"},
            credential_kind="access_token",
            credential_secret="console-access-token",
            user_id="27",
            request_json=fake_request,
        )

        self.assertTrue(result["supported"])
        self.assertEqual(result["remaining_usd"], 20.0)
        self.assertEqual(result["used_usd"], 4.0)
        self.assertEqual(result["total_usd"], 24.0)
        self.assertEqual(seen["url"], "https://relay.example.com/api/user/self")
        self.assertEqual(seen["headers"]["Authorization"], "console-access-token")
        self.assertEqual(seen["headers"]["New-Api-User"], "27")
        self.assertNotIn("console-access-token", repr(result))

    def test_account_balance_keeps_only_session_cookie_for_compatibility(self):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update(kwargs)
            return {"success": True, "data": {"quota": 500_000, "used_quota": 0}}, None

        result = query_channel_account_balance(
            {"base_url": "https://relay.example.com/v1"},
            credential_kind="session_cookie",
            credential_secret="session=console-secret; theme=dark",
            user_id="27",
            request_json=fake_request,
        )

        self.assertTrue(result["supported"])
        self.assertEqual(seen["headers"]["Cookie"], "session=console-secret")
        self.assertNotIn("theme", seen["headers"]["Cookie"])
        self.assertNotIn("console-secret", repr(result))

    def test_account_balance_rejects_invalid_user_id_before_request(self):
        with self.assertRaises(channel_intelligence.ChannelInspectionError):
            query_channel_account_balance(
                {"base_url": "https://relay.example.com/v1"},
                credential_kind="session_cookie",
                credential_secret="session=console-secret",
                user_id="not-a-number",
                request_json=lambda *args, **kwargs: self.fail("request must not run"),
            )

    def test_daily_costs_aggregate_quota_by_day(self):
        import datetime
        seen = {}
        tz = datetime.timezone(datetime.timedelta(hours=8))
        today = datetime.datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday = today - datetime.timedelta(days=1)

        def fake_request(method, url, **kwargs):
            seen["url"] = url
            seen["headers"] = kwargs.get("headers") or {}
            return {
                "success": True,
                "data": [
                    {"created_at": int(yesterday.timestamp()), "quota": 500_000, "count": 10},
                    {"created_at": int(yesterday.timestamp()), "quota": 250_000, "count": 5},
                    {"created_at": int(today.timestamp()), "quota": 100_000, "count": 2},
                ],
            }, None

        result = query_channel_daily_costs(
            {"base_url": "https://relay.example.com/v1"},
            credential_kind="access_token",
            credential_secret="console-access-token",
            user_id="27",
            days=2,
            request_json=fake_request,
        )

        self.assertTrue(result["supported"])
        self.assertEqual(result["total_cost"], 1.7)
        self.assertEqual(result["days"][-1]["cost"], 0.2)
        self.assertEqual(result["days"][-1]["count"], 2)
        self.assertIn("/api/data/self", seen["url"])
        self.assertEqual(seen["headers"]["New-Api-User"], "27")


if __name__ == "__main__":
    unittest.main()
