import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "relay" / "channel_intelligence.py"
SPEC = importlib.util.spec_from_file_location("channel_intelligence", MODULE_PATH)
channel_intelligence = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(channel_intelligence)

_pick_status = channel_intelligence._pick_status
inspect_channel = channel_intelligence.inspect_channel
models_url_from_api_url = channel_intelligence.models_url_from_api_url
origin_from_url = channel_intelligence.origin_from_url
query_channel_balance = channel_intelligence.query_channel_balance


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


if __name__ == "__main__":
    unittest.main()
