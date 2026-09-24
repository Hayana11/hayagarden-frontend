from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tools.xiaomi_health.client import API_BASE, AGGREGATED_PATH, XiaomiHealthClient, XiaomiProviderError
from tools.xiaomi_health.crypto import rc4_drop
from tools.xiaomi_health.internal_adapter import run
from tools.xiaomi_health.parser import MalformedHealthResponse, latest_date, parse_series_response
from tools.xiaomi_health.qr import qr_matrix, render_qr_svg
from tools.xiaomi_health.store import SOURCE, XiaomiCredentialStore


SECRET_VALUES = {
    "user_id": "7400000012345678",
    "c_user_id": "c-user-private",
    "service_token": "service-token-private",
    "ssecurity": "c3NlY3VyaXR5LXByaXZhdGU=",
    "pass_token": "pass-token-private",
    "device_id": "device-id-private",
    "auth_state": "valid",
    "updated_at": "2026-09-24T01:00:00Z",
    "last_checked_at": None,
    "last_success_at": None,
    "last_error": None,
}


class XiaomiHealthProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="xiaomi-health-test-")
        self.path = Path(self.temp.name) / ".xiaomi-health.env"
        self.store = XiaomiCredentialStore(str(self.path), require_root=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_store_uses_atomic_replace_and_owner_only_mode(self) -> None:
        real_replace = os.replace
        with mock.patch("tools.xiaomi_health.store.os.replace", wraps=real_replace) as replace:
            self.store.save(SECRET_VALUES)
        replace.assert_called_once()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.assertEqual(self.path.stat().st_uid, 0)
            self.assertEqual(self.path.stat().st_gid, 0)
        self.assertEqual(self.store.load(), SECRET_VALUES)
        self.assertEqual(list(Path(self.temp.name).glob(".*.tmp")), [])

    def test_store_refuses_group_readable_credential_file(self) -> None:
        self.store.save(SECRET_VALUES)
        self.path.chmod(0o640)
        with self.assertRaisesRegex(Exception, "permissions invalid"):
            self.store.load()

    def test_status_redacts_all_credential_fields(self) -> None:
        self.store.save(SECRET_VALUES)
        public = json.dumps(self.store.status(), sort_keys=True)
        self.assertEqual(self.store.status(), {
            "connected": True,
            "provider": SOURCE,
            "auth_state": "valid",
            "last_success_at": None,
            "last_error": None,
        })
        for secret in (SECRET_VALUES[k] for k in ("user_id", "c_user_id", "service_token", "ssecurity", "pass_token", "device_id")):
            self.assertNotIn(secret, public)

    def test_status_reports_expired_without_secret_reflection(self) -> None:
        self.store.save({**SECRET_VALUES, "auth_state": "auth_expired"})
        result = run("health_status", store=self.store)
        public = json.dumps(result, sort_keys=True)
        self.assertFalse(result["connected"])
        self.assertEqual(result["auth_state"], "auth_expired")
        self.assertNotIn(SECRET_VALUES["user_id"], public)
        self.assertNotIn(SECRET_VALUES["service_token"], public)

    def test_steps_parser_normalizes_only_safe_fields(self) -> None:
        timestamp = int(datetime(2026, 9, 24, 1, tzinfo=timezone.utc).timestamp())
        response = {"result": {"data_list": [{
            "time": timestamp,
            "value": json.dumps({"steps": 8432, "user_id": SECRET_VALUES["user_id"], "service_token": SECRET_VALUES["service_token"]}),
        }]}}
        rows = parse_series_response(response, "steps", days=2)
        self.assertEqual(rows[0]["value"], 8432)
        self.assertEqual(rows[0]["unit"], "steps")
        self.assertEqual(rows[0]["dataDate"], "2026-09-24")
        self.assertNotIn(SECRET_VALUES["service_token"], json.dumps(rows))

    def test_sleep_parser_normalizes_allowlisted_details(self) -> None:
        timestamp = int(datetime(2026, 9, 23, 17, tzinfo=timezone.utc).timestamp())
        response = {"result": {"data_list": [{"time": timestamp, "value": {
            "sleep_duration": 432,
            "deep_sleep": 101,
            "light_sleep": 250,
            "pass_token": SECRET_VALUES["pass_token"],
        }}]}}
        rows = parse_series_response(response, "sleep", days=2)
        self.assertEqual(rows[0]["value"], 432)
        self.assertEqual(rows[0]["unit"], "minutes")
        self.assertEqual(rows[0]["details"], {"sleep_duration": 432, "deep_sleep": 101, "light_sleep": 250})
        self.assertNotIn(SECRET_VALUES["pass_token"], json.dumps(rows))

    def test_heart_rate_parser_normalizes_allowlisted_details(self) -> None:
        timestamp = int(datetime(2026, 9, 24, 2, tzinfo=timezone.utc).timestamp())
        response = {"result": {"data_list": [{"time": timestamp, "value": {
            "avg_heart_rate": 72,
            "min_heart_rate": 55,
            "max_heart_rate": 121,
            "cookie": "cookie-private",
        }}]}}
        rows = parse_series_response(response, "heart_rate", days=2)
        self.assertEqual(rows[0]["value"], 72)
        self.assertEqual(rows[0]["unit"], "bpm")
        self.assertNotIn("cookie-private", json.dumps(rows))

    def test_empty_and_malformed_responses(self) -> None:
        self.assertEqual(parse_series_response({"result": {"data_list": []}}, "steps", days=2), [])
        self.assertIsNone(latest_date([]))
        for response in ({}, {"result": {}}, {"result": {"data_list": [None]}}):
            with self.assertRaises(MalformedHealthResponse):
                parse_series_response(response, "steps", days=2)

    def test_rc4_known_vector(self) -> None:
        self.assertEqual(rc4_drop(b"Key", b"Plaintext", drop=0).hex(), "bbf316e8d940af0ad3")

    def test_qr_matrix_matches_independently_decoded_vector(self) -> None:
        matrix = qr_matrix("https://example.com/")
        packed = bytes(1 if cell else 0 for row in matrix for cell in row)
        self.assertEqual(
            hashlib.sha256(packed).hexdigest(),
            "a3ec45b2f6bb914a0ba62a49783b3d88648b7eed75f6b760ca8bb7ca53a4477c",
        )

    def test_qr_matrix_preserves_alignment_patterns_on_timing_axes(self) -> None:
        matrix = qr_matrix("https://example.com/")
        for center_x, center_y in ((28, 6), (6, 28)):
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    expected = max(abs(dx), abs(dy)) != 1
                    self.assertEqual(matrix[center_y + dy][center_x + dx], expected)

    def test_qr_svg_does_not_print_or_embed_login_url(self) -> None:
        login_url = "https://account.xiaomi.com/longPolling/login?ticket=private-qr-ticket"
        svg = render_qr_svg(login_url)
        self.assertEqual(len(qr_matrix(login_url)), 57)
        self.assertTrue(all(len(row) == 57 for row in qr_matrix(login_url)))
        self.assertTrue(svg.startswith("<svg "))
        self.assertNotIn(login_url, svg)
        self.assertNotIn("private-qr-ticket", svg)

    def test_query_uses_own_uid_and_aggregated_endpoint(self) -> None:
        self.store.save(SECRET_VALUES)
        client = XiaomiHealthClient(self.store)
        captured: dict[str, object] = {}
        def encrypted(method, path, security, params):
            captured.update({"method": method, "path": path, "params": params})
            return {"_nonce": "nonce", "data": "encrypted"}
        with mock.patch("tools.xiaomi_health.client.build_encrypted_params", side_effect=encrypted):
            with mock.patch("tools.xiaomi_health.client.decrypt_response", return_value={"code": 0, "result": {"data_list": []}}):
                client._http = mock.Mock(return_value=(200, {}, b"encrypted"))
                rows = client._request_health(SECRET_VALUES, "steps", 2)
        self.assertEqual(rows, [])
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], AGGREGATED_PATH)
        self.assertTrue(client._http.call_args.args[0].startswith(f"{API_BASE}{AGGREGATED_PATH}?"))
        headers = client._http.call_args.kwargs["headers"]
        self.assertEqual(headers["region_tag"], "cn")
        self.assertEqual(headers["handleparams"], "true")
        self.assertEqual(headers["Cookie"], f"cUserId={SECRET_VALUES['c_user_id']}; serviceToken={SECRET_VALUES['service_token']}")
        self.assertEqual(captured["params"]["relative_uid"], SECRET_VALUES["user_id"])
        self.assertEqual(captured["params"]["key"], "steps")
        self.assertEqual(captured["params"]["tag"], "daily_report")
        self.assertEqual(captured["params"]["limit"], 2)

    def test_steps_request_and_safe_diagnostic_contract(self) -> None:
        self.store.save(SECRET_VALUES)
        client = XiaomiHealthClient(self.store)
        captured: dict[str, object] = {}
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        def encrypted(method, path, security, params):
            captured.update({"method": method, "path": path, "params": params})
            return {"_nonce": "nonce", "data": "encrypted"}
        upstream = {
            "code": 3001,
            "result": {"data_list": [{"user_id": SECRET_VALUES["user_id"], "service_token": SECRET_VALUES["service_token"]}]},
            "pass_token": SECRET_VALUES["pass_token"],
        }
        with mock.patch("tools.xiaomi_health.client.build_encrypted_params", side_effect=encrypted):
            with mock.patch("tools.xiaomi_health.client.decrypt_response", return_value=upstream):
                client._http = mock.Mock(return_value=(200, {}, b"encrypted"))
                with self.assertRaises(XiaomiProviderError) as raised:
                    client._request_health(SECRET_VALUES, "steps", 2, now=now)
        params = captured["params"]
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], AGGREGATED_PATH)
        self.assertEqual(params["relative_uid"], SECRET_VALUES["user_id"])
        self.assertEqual(params["key"], "steps")
        self.assertEqual(params["tag"], "daily_report")
        self.assertEqual(params["start_time"], int(datetime(2026, 9, 22, 16, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(params["end_time"], int(datetime(2026, 9, 24, 15, 59, 59, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(params["limit"], 2)
        diagnostic = client.last_diagnostic
        self.assertEqual(raised.exception.code, "api_error")
        self.assertEqual(diagnostic["metric"], "steps")
        self.assertEqual(diagnostic["endpoint_path"], AGGREGATED_PATH)
        self.assertEqual(diagnostic["http_status"], 200)
        self.assertEqual(diagnostic["xiaomi_response_code"], 3001)
        self.assertEqual(diagnostic["response_top_level_keys"], ["code", "pass_token", "result"])
        self.assertTrue(diagnostic["data_list_present"])
        self.assertEqual(diagnostic["row_count"], 1)
        serialized = json.dumps(diagnostic)
        for field in ("user_id", "service_token", "pass_token"):
            self.assertNotIn(SECRET_VALUES[field], serialized)

    def test_latest_preserves_first_metric_dependency_failure(self) -> None:
        self.store.save(SECRET_VALUES)
        client = XiaomiHealthClient(self.store)
        with mock.patch.object(client, "get_series", side_effect=XiaomiProviderError("api_error")) as get_series:
            with self.assertRaises(XiaomiProviderError):
                client.get_latest()
        get_series.assert_called_once_with("steps", 2)

    def test_days_boundaries_and_invalid_values(self) -> None:
        self.store.save(SECRET_VALUES)
        client = XiaomiHealthClient(self.store)
        with mock.patch.object(client, "_request_health", return_value=[]):
            self.assertEqual(client.get_series("steps", 1)["status"], "EMPTY")
            self.assertEqual(client.get_series("steps", 30)["status"], "EMPTY")
        for bad in (0, 31, -1, True, 1.5, "2"):
            with self.assertRaises(XiaomiProviderError):
                client.get_series("steps", bad)  # type: ignore[arg-type]

    def test_timeout_is_mapped_and_never_reflected(self) -> None:
        self.store.save(SECRET_VALUES)
        opener = mock.Mock()
        opener.open.side_effect = TimeoutError("private-token-must-not-escape")
        client = XiaomiHealthClient(self.store, opener=opener)
        with self.assertRaises(XiaomiProviderError) as raised:
            client.get_series("steps", 2)
        self.assertEqual(raised.exception.code, "timeout")
        self.assertNotIn("private-token-must-not-escape", str(raised.exception))
        self.assertEqual(self.store.status()["last_error"], "timeout")

    def test_adapter_redacts_unexpected_provider_exception(self) -> None:
        class BrokenClient:
            def get_series(self, metric, days):
                raise RuntimeError(SECRET_VALUES["service_token"])

        result = run("health_steps", days=2, store=self.store, client=BrokenClient())
        self.assertEqual(result["error_code"], "unavailable")
        self.assertNotIn(SECRET_VALUES["service_token"], json.dumps(result))

    def test_auth_expired_state_persisted_without_reflection(self) -> None:
        self.store.save(SECRET_VALUES)
        client = XiaomiHealthClient(self.store)
        with mock.patch.object(client, "_request_health", side_effect=XiaomiProviderError("auth_expired")):
            with self.assertRaises(XiaomiProviderError):
                client.get_series("heart_rate", 2)
        status = self.store.status()
        self.assertEqual(status["auth_state"], "auth_expired")
        self.assertEqual(status["last_error"], "auth_expired")
        self.assertNotIn(SECRET_VALUES["user_id"], json.dumps(status))


if __name__ == "__main__":
    unittest.main()
