from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from tools.xiaomi_health.client import API_BASE, AGGREGATED_PATH, FITNESS_DATA_PATH, XiaomiHealthClient, XiaomiProviderError
from tools.xiaomi_health.crypto import rc4_drop
from tools.xiaomi_health.internal_adapter import run
from tools.xiaomi_health.parser import (
    MalformedHealthResponse,
    latest_date,
    parse_menstrual_symptoms_rows,
    parse_menstruation_rows,
    parse_series_response,
)
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
        result = run("get_health", metric="status", store=self.store)
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

    @staticmethod
    def _wall_clock(dt: datetime) -> int:
        return int(dt.timestamp()) + 8 * 3600

    @staticmethod
    def _live_heart_rate_row(outer: Any, latest: dict[str, Any]) -> dict[str, Any]:
        return {"time": outer, "value": json.dumps({
            "avg_hr": 70,
            "avg_rhr": 60,
            "max_hr": 120,
            "min_hr": 50,
            "latest_hr": latest,
        })}

    def test_heart_rate_uses_nested_latest_hr_bpm_and_time(self) -> None:
        outer = int(datetime(2026, 9, 23, 16, tzinfo=timezone.utc).timestamp())
        latest = self._wall_clock(datetime(2026, 9, 24, 3, 15, 30, tzinfo=timezone.utc))
        response = {"result": {"data_list": [self._live_heart_rate_row(outer, {
            "bpm": 82, "time": latest, "dbTime": latest - 8 * 3600, "dbKey": "synthetic",
        })]}}
        rows = parse_series_response(response, "heart_rate", days=7)
        self.assertEqual(rows, [{
            "sampledAt": "2026-09-24T03:15:30Z",
            "dataDate": "2026-09-24",
            "value": 82,
            "unit": "bpm",
        }])

    def test_heart_rate_latest_hr_time_accepts_milliseconds(self) -> None:
        outer = int(datetime(2026, 9, 23, 16, tzinfo=timezone.utc).timestamp())
        latest_ms = self._wall_clock(datetime(2026, 9, 23, 17, 30, tzinfo=timezone.utc)) * 1000
        response = {"result": {"data_list": [self._live_heart_rate_row(outer, {"bpm": 77, "time": latest_ms})]}}
        row = parse_series_response(response, "heart_rate", days=7)[0]
        self.assertEqual(row["value"], 77)
        self.assertEqual(row["sampledAt"], "2026-09-23T17:30:00Z")
        self.assertEqual(row["dataDate"], "2026-09-24")

    def test_heart_rate_latest_hr_time_is_utc_plus_8_wall_clock(self) -> None:
        outer = int(datetime(2026, 9, 24, 0, tzinfo=timezone.utc).timestamp())
        late_evening = self._wall_clock(datetime(2026, 9, 24, 15, 37, tzinfo=timezone.utc))
        row = parse_series_response({"result": {"data_list": [
            self._live_heart_rate_row(outer, {"bpm": 70, "time": late_evening}),
        ]}}, "heart_rate", days=7)[0]
        self.assertEqual(row["sampledAt"], "2026-09-24T15:37:00Z")
        self.assertEqual(row["dataDate"], "2026-09-24")
        row = parse_series_response({"result": {"data_list": [
            self._live_heart_rate_row(outer, {"bpm": 70, "time": str(late_evening)}),
        ]}}, "heart_rate", days=7)[0]
        self.assertEqual(row["sampledAt"], "2026-09-24T15:37:00Z")
        row = parse_series_response({"result": {"data_list": [
            self._live_heart_rate_row(outer, {"bpm": 70, "time": "2026-09-24T15:37:00Z"}),
        ]}}, "heart_rate", days=7)[0]
        self.assertEqual(row["sampledAt"], "2026-09-24T15:37:00Z")

    def test_heart_rate_invalid_latest_time_falls_back_to_outer_time(self) -> None:
        outer = int(datetime(2026, 9, 24, 2, tzinfo=timezone.utc).timestamp())
        for bad_time in (None, "not-a-time", -5, 0, "", True, {"nested": 1}):
            latest = {"bpm": 91} if bad_time is None else {"bpm": 91, "time": bad_time}
            response = {"result": {"data_list": [self._live_heart_rate_row(outer, latest)]}}
            row = parse_series_response(response, "heart_rate", days=7)[0]
            self.assertEqual(row["value"], 91)
            self.assertEqual(row["sampledAt"], "2026-09-24T02:00:00Z")
            self.assertEqual(row["dataDate"], "2026-09-24")

    def test_heart_rate_invalid_latest_bpm_uses_top_level_fallback(self) -> None:
        outer = int(datetime(2026, 9, 24, 2, tzinfo=timezone.utc).timestamp())
        latest = int(datetime(2026, 9, 24, 5, tzinfo=timezone.utc).timestamp())
        for bad_bpm in (None, "abc", float("nan"), True, {"x": 1}):
            response = {"result": {"data_list": [{"time": outer, "value": {
                "bpm": 66, "latest_hr": {"bpm": bad_bpm, "time": latest},
            }}]}}
            row = parse_series_response(response, "heart_rate", days=7)[0]
            self.assertEqual(row["value"], 66)
            self.assertEqual(row["sampledAt"], "2026-09-24T02:00:00Z")
        response = {"result": {"data_list": [{"time": outer, "value": {"latest_hr": "not-an-object", "heart_rate": 64}}]}}
        self.assertEqual(parse_series_response(response, "heart_rate", days=7)[0]["value"], 64)
        response = {"result": {"data_list": [{"time": outer, "value": json.dumps({"avg_hr": 70, "latest_hr": {}})}]}}
        self.assertIsNone(parse_series_response(response, "heart_rate", days=7)[0]["value"])

    def test_heart_rate_records_sort_by_normalized_sample_time(self) -> None:
        def ts(day: int, hour: int) -> int:
            return int(datetime(2026, 9, day, hour, tzinfo=timezone.utc).timestamp())
        response = {"result": {"data_list": [
            self._live_heart_rate_row(ts(22, 16), {"bpm": 81, "time": ts(24, 4) + 8 * 3600}),
            self._live_heart_rate_row(ts(23, 16), {"bpm": 72, "time": ts(23, 1) + 8 * 3600}),
            self._live_heart_rate_row(ts(21, 16), {"bpm": 68, "time": "bad"}),
            {"time": ts(24, 2), "value": {"avg_heart_rate": 75}},
        ]}}
        rows = parse_series_response(response, "heart_rate", days=7)
        self.assertEqual([row["sampledAt"] for row in rows], sorted(row["sampledAt"] for row in rows))
        self.assertEqual([row["value"] for row in rows], [68, 72, 75, 81])
        self.assertEqual(rows[-1]["sampledAt"], "2026-09-24T04:00:00Z")

    def test_heart_rate_latest_uses_most_recent_nested_sample(self) -> None:
        def ts(day: int, hour: int) -> int:
            return int(datetime(2026, 9, day, hour, tzinfo=timezone.utc).timestamp())
        records = parse_series_response({"result": {"data_list": [
            self._live_heart_rate_row(ts(23, 16), {"bpm": 72, "time": ts(23, 18) + 8 * 3600}),
            self._live_heart_rate_row(ts(22, 16), {"bpm": 84, "time": ts(24, 6) + 8 * 3600}),
        ]}}, "heart_rate", days=7)
        client = XiaomiHealthClient(self.store)
        series = {
            "steps": {"records": [{"sampledAt": "2026-09-24T00:00:00Z", "dataDate": "2026-09-24", "value": 10, "unit": "steps"}]},
            "sleep": {"records": []},
            "heart_rate": {"records": records},
        }
        with mock.patch.object(client, "get_series", side_effect=lambda metric, days: series[metric]):
            latest = client.get_latest(7)
        self.assertEqual(latest["heart_rate"]["value"], 84)
        self.assertEqual(latest["heart_rate"]["sampledAt"], "2026-09-24T06:00:00Z")
        self.assertEqual(latest["steps"]["value"], 10)
        self.assertIsNone(latest["sleep"])

    def test_heart_rate_latest_hr_sensitive_fields_are_dropped(self) -> None:
        outer = int(datetime(2026, 9, 24, 2, tzinfo=timezone.utc).timestamp())
        latest = int(datetime(2026, 9, 24, 3, tzinfo=timezone.utc).timestamp())
        response = {"result": {"data_list": [self._live_heart_rate_row(outer, {
            "bpm": 79,
            "time": latest,
            "dbTime": 1790000000123,
            "dbKey": "dbkey-synthetic-private",
            "cookie": "cookie-synthetic-private",
            "service_token": SECRET_VALUES["service_token"],
            "arbitrary_secret": "arbitrary-synthetic-secret",
        })]}}
        rows = parse_series_response(response, "heart_rate", days=7)
        public = json.dumps(rows, sort_keys=True)
        self.assertEqual(set(rows[0]), {"sampledAt", "dataDate", "value", "unit"})
        for leaked in ("dbTime", "dbKey", "1790000000123", "dbkey-synthetic-private", "cookie", "cookie-synthetic-private",
                       "service_token", SECRET_VALUES["service_token"], "arbitrary_secret", "arbitrary-synthetic-secret",
                       "avg_hr", "avg_rhr", "max_hr", "min_hr", "latest_hr"):
            self.assertNotIn(leaked, public)

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

    def _regular_latest(self, days: int = 7) -> dict[str, Any]:
        return {
            "provider": SOURCE,
            "sampledAt": "2026-09-24T01:00:00Z",
            "dataDate": "2026-09-24",
            "steps": {"sampledAt": "2026-09-24T01:00:00Z", "dataDate": "2026-09-24", "value": 1000, "unit": "steps"},
            "sleep": {"sampledAt": "2026-09-24T01:00:00Z", "dataDate": "2026-09-24", "value": 420, "unit": "minutes"},
            "heart_rate": {"sampledAt": "2026-09-24T01:00:00Z", "dataDate": "2026-09-24", "value": 72, "unit": "bpm"},
        }

    def _recorded_cycle(self, days: int = 180) -> dict[str, Any]:
        return {
            "status": "PASS",
            "provider": SOURCE,
            "source": SOURCE,
            "metric": "cycle",
            "days": days,
            "events": [{"type": "period_start", "timestamp": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:01Z"}],
            "periods": [{"start": "2026-09-01T00:00:00Z", "end": None, "open": True, "source": "recorded"}],
            "symptoms": [{"timestamp": "2026-09-01T00:00:00Z", "hp": "normal", "mood": "happy", "pain": None}],
            "predictions": None,
        }

    def test_internal_adapter_has_one_dispatch_contract(self) -> None:
        self.store.save(SECRET_VALUES)
        client = mock.Mock()
        client.get_latest.return_value = {"provider": "xiaomi_fitness_cloud"}
        client.get_cycle.return_value = {"status": "EMPTY", "days": 180, "predictions": None}
        client.get_series.return_value = {"status": "PASS", "records": []}

        self.assertEqual(run("get_health", metric="status", days=1, store=self.store, client=client)["provider"], SOURCE)
        client.get_latest.assert_not_called()
        self.assertEqual(run("get_health", metric="all", days=1, store=self.store, client=client), {
            "provider": "xiaomi_fitness_cloud",
            "cycle": {"status": "EMPTY", "days": 180, "predictions": None},
        })
        client.get_latest.assert_called_once_with(1)
        client.get_cycle.assert_called_once_with(180)
        self.assertEqual(run("get_health", metric="steps", days=2, store=self.store, client=client)["status"], "PASS")
        client.get_series.assert_called_once_with("steps", 2)
        self.assertEqual(run("health_steps", days=2, store=self.store, client=client)["error_code"], "unavailable")

    def test_all_uses_regular_days_and_fixed_cycle_window(self) -> None:
        client = mock.Mock()
        client.get_latest.return_value = self._regular_latest(7)
        client.get_cycle.return_value = self._recorded_cycle(180)
        result = run("get_health", metric="all", days=7, store=self.store, client=client)
        client.get_latest.assert_called_once_with(7)
        client.get_cycle.assert_called_once_with(180)
        self.assertEqual(result["steps"]["value"], 1000)
        self.assertEqual(result["sleep"]["value"], 420)
        self.assertEqual(result["heart_rate"]["value"], 72)
        self.assertEqual(result["cycle"]["days"], 180)
        self.assertIsNone(result["cycle"]["predictions"])

        client.reset_mock()
        client.get_latest.return_value = self._regular_latest(30)
        client.get_cycle.return_value = self._recorded_cycle(180)
        result = run("get_health", metric="all", days=30, store=self.store, client=client)
        client.get_latest.assert_called_once_with(30)
        client.get_cycle.assert_called_once_with(180)
        self.assertEqual(result["cycle"]["days"], 180)

    def test_all_keeps_regular_metrics_when_cycle_is_empty(self) -> None:
        client = mock.Mock()
        client.get_latest.return_value = self._regular_latest()
        client.get_cycle.return_value = {
            "status": "EMPTY", "provider": SOURCE, "source": SOURCE, "metric": "cycle",
            "days": 180, "events": [], "periods": [], "symptoms": [], "predictions": None,
        }
        result = run("get_health", metric="all", days=7, store=self.store, client=client)
        self.assertEqual(result["steps"]["value"], 1000)
        self.assertEqual(result["cycle"]["status"], "EMPTY")
        self.assertIsNone(result["cycle"]["predictions"])

    def test_all_keeps_regular_metrics_when_cycle_fails_safely(self) -> None:
        client = mock.Mock()
        client.get_latest.return_value = self._regular_latest()
        client.get_cycle.side_effect = XiaomiProviderError("timeout")
        result = run("get_health", metric="all", days=7, store=self.store, client=client)
        self.assertEqual(result["steps"]["value"], 1000)
        self.assertEqual(result["sleep"]["value"], 420)
        self.assertEqual(result["heart_rate"]["value"], 72)
        self.assertEqual(result["cycle"]["status"], "FAIL")
        self.assertEqual(result["cycle"]["error_code"], "timeout")
        self.assertEqual(result["cycle"]["days"], 180)
        self.assertIsNone(result["cycle"]["predictions"])
        self.assertNotIn("status", result)

        class ExplodingCycle:
            def get_latest(self, days):
                return {"steps": {"value": 9}}

            def get_cycle(self, days):
                raise RuntimeError(SECRET_VALUES["service_token"])

        result = run("get_health", metric="all", days=7, store=self.store, client=ExplodingCycle())
        self.assertEqual(result["steps"]["value"], 9)
        self.assertEqual(result["cycle"]["error_code"], "unavailable")
        self.assertNotIn(SECRET_VALUES["service_token"], json.dumps(result))

    def test_all_still_fails_when_regular_latest_fails(self) -> None:
        client = mock.Mock()
        client.get_latest.side_effect = XiaomiProviderError("api_error")
        result = run("get_health", metric="all", days=7, store=self.store, client=client)
        self.assertEqual(result, {"status": "FAIL", "provider": SOURCE, "error_code": "api_error"})
        client.get_cycle.assert_not_called()

    def test_adapter_redacts_unexpected_provider_exception(self) -> None:
        class BrokenClient:
            def get_series(self, metric, days):
                raise RuntimeError(SECRET_VALUES["service_token"])

        result = run("get_health", metric="steps", days=2, store=self.store, client=BrokenClient())
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


class XiaomiCycleParserTests(unittest.TestCase):
    START = 1_700_000_000
    UPDATED = 1_700_000_111

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="xiaomi-cycle-test-")
        self.path = Path(self.temp.name) / ".xiaomi-health.env"
        self.store = XiaomiCredentialStore(str(self.path), require_root=False)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @classmethod
    def row(cls, *, status: int = 1, timestamp: int | None = None, updated_at: int | None = None, extra: dict | None = None):
        value = {
            "date_time": cls.START if timestamp is None else timestamp,
            "status": status,
            "update_time": cls.UPDATED if updated_at is None else updated_at,
        }
        if extra:
            value.update(extra)
        return {"value": json.dumps(value)}

    @staticmethod
    def timestamp(seconds: int) -> str:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def test_status_values_normalize_to_documented_event_types(self):
        parsed = parse_menstruation_rows([self.row(status=1), self.row(status=2), self.row(status=3)])
        self.assertEqual([event["type"] for event in parsed["events"]], [
            "period_start", "period_end", "period_start_end",
        ])
        self.assertEqual(parsed["events"][0]["timestamp"], self.timestamp(self.START))
        self.assertEqual(parsed["events"][0]["updated_at"], self.timestamp(self.UPDATED))

    def test_start_end_pairing_closes_nearest_unmatched_start(self):
        parsed = parse_menstruation_rows([
            self.row(status=2, timestamp=self.START + 20),
            self.row(status=1, timestamp=self.START),
            self.row(status=2, timestamp=self.START + 30),
            self.row(status=1, timestamp=self.START + 10),
        ])
        self.assertEqual(len(parsed["events"]), 4)
        self.assertEqual(parsed["periods"], [
            {"start": self.timestamp(self.START), "end": self.timestamp(self.START + 30), "open": False, "source": "recorded"},
            {"start": self.timestamp(self.START + 10), "end": self.timestamp(self.START + 20), "open": False, "source": "recorded"},
        ])

    def test_open_start_and_orphan_end_do_not_invent_dates(self):
        parsed = parse_menstruation_rows([
            self.row(status=2, timestamp=self.START - 10),
            self.row(status=1, timestamp=self.START),
        ])
        self.assertEqual(len(parsed["events"]), 2)
        self.assertEqual(parsed["periods"], [{
            "start": self.timestamp(self.START),
            "end": None,
            "open": True,
            "source": "recorded",
        }])

    def test_start_end_is_a_same_timestamp_recorded_period(self):
        parsed = parse_menstruation_rows([self.row(status=3)])
        self.assertEqual(parsed["periods"], [{
            "start": self.timestamp(self.START),
            "end": self.timestamp(self.START),
            "open": False,
            "source": "recorded",
        }])

    def test_unordered_records_are_sorted_by_event_timestamp(self):
        parsed = parse_menstruation_rows([
            self.row(status=3, timestamp=self.START + 20),
            self.row(status=1, timestamp=self.START),
            self.row(status=2, timestamp=self.START + 10),
        ])
        self.assertEqual(
            [event["timestamp"] for event in parsed["events"]],
            [self.timestamp(self.START), self.timestamp(self.START + 10), self.timestamp(self.START + 20)],
        )

    def test_unknown_status_fails_closed(self):
        with self.assertRaises(MalformedHealthResponse):
            parse_menstruation_rows([self.row(status=4)])

    def test_malformed_value_missing_timestamp_and_invalid_epoch_fail_closed(self):
        for rows in (
            [{"value": "{not-json"}],
            [{"value": {"status": 1, "update_time": self.UPDATED}}],
            [self.row(timestamp=0)],
            [self.row(timestamp=True)],
            [self.row(timestamp=float(self.START))],
            [self.row(updated_at="1700000111")],
        ):
            with self.subTest(case=type(rows[0]["value"]).__name__):
                with self.assertRaises(MalformedHealthResponse):
                    parse_menstruation_rows(rows)

    def test_empty_cycle_and_symptom_rows_are_valid(self):
        self.assertEqual(parse_menstruation_rows([]), {"events": [], "periods": []})
        self.assertEqual(parse_menstrual_symptoms_rows([]), [])

    def test_symptom_enums_are_normalized_without_renaming_hp(self):
        rows = [{
            "value": json.dumps({
                "date_time": self.START,
                "hp": 2,
                "mood": 0,
                "pain": 1,
            }),
        }]
        self.assertEqual(parse_menstrual_symptoms_rows(rows), [{
            "timestamp": self.timestamp(self.START),
            "hp": "much",
            "mood": "happy",
            "pain": "normal",
        }])
        unknown = parse_menstrual_symptoms_rows([{
            "value": {"date_time": self.START, "hp": 8, "mood": "untrusted", "pain": None},
        }])
        self.assertEqual(unknown[0], {
            "timestamp": self.timestamp(self.START),
            "hp": None,
            "mood": None,
            "pain": None,
        })

    def test_parser_drops_unrecognized_sensitive_fields(self):
        row = self.row(extra={"note": "synthetic-private-note", "user_id": SECRET_VALUES["user_id"]})
        encoded = json.dumps(parse_menstruation_rows([row]))
        self.assertNotIn("synthetic-private-note", encoded)
        self.assertNotIn(SECRET_VALUES["user_id"], encoded)

    def test_cycle_request_uses_self_uid_and_single_record_endpoint(self):
        captured = {}
        client = XiaomiHealthClient(self.store)

        def encrypted(method, path, security, params):
            captured.update({"method": method, "path": path, "params": params})
            return {"_nonce": "synthetic-nonce", "data": "encrypted"}

        self.store.save(SECRET_VALUES)
        with mock.patch("tools.xiaomi_health.client.build_encrypted_params", side_effect=encrypted):
            with mock.patch("tools.xiaomi_health.client.decrypt_response", return_value={"code": 0, "result": {"data_list": []}}):
                client._http = mock.Mock(return_value=(200, {}, b"encrypted"))
                rows = client._request_cycle_rows(SECRET_VALUES, "menstruation", 180)
        self.assertEqual(rows, [])
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["path"], FITNESS_DATA_PATH)
        self.assertTrue(client._http.call_args.args[0].startswith(f"{API_BASE}{FITNESS_DATA_PATH}?"))
        self.assertEqual(captured["params"]["relative_uid"], SECRET_VALUES["user_id"])
        self.assertEqual(captured["params"]["key"], "menstruation")
        self.assertEqual(captured["params"]["tag"], "daily_report")
        self.assertEqual(captured["params"]["limit"], 180)
        diagnostic = json.dumps(client.last_diagnostic, sort_keys=True)
        for secret in (SECRET_VALUES["user_id"], SECRET_VALUES["service_token"], SECRET_VALUES["ssecurity"]):
            self.assertNotIn(secret, diagnostic)

    def test_cycle_client_returns_empty_and_does_not_persist_status(self):
        client = XiaomiHealthClient(self.store)
        self.store.save(SECRET_VALUES)
        with mock.patch.object(client, "_request_cycle_rows", side_effect=[[], []]) as request:
            result = client.get_cycle()
        self.assertEqual(result["status"], "EMPTY")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["periods"], [])
        self.assertEqual(result["symptoms"], [])
        self.assertIsNone(result["predictions"])
        self.assertEqual([call.args[1] for call in request.call_args_list], ["menstruation", "menstrual_symptoms"])
        self.assertEqual(self.store.status()["auth_state"], "valid")

    def test_internal_adapter_applies_cycle_specific_days_range(self):
        client = mock.Mock()
        client.get_cycle.return_value = {"status": "EMPTY"}
        client.get_series.return_value = {"status": "EMPTY"}
        store = object()
        self.assertEqual(run("get_health", metric="cycle", store=store, client=client), {"status": "EMPTY"})
        client.get_cycle.assert_called_once_with(180)
        self.assertEqual(run("get_health", metric="cycle", days=365, store=store, client=client), {"status": "EMPTY"})
        client.get_cycle.assert_called_with(365)
        self.assertEqual(run("get_health", metric="steps", days=30, store=store, client=client)["status"], "EMPTY")
        self.assertEqual(run("get_health", metric="steps", days=31, store=store, client=client)["error_code"], "malformed_response")
        self.assertEqual(run("get_health", metric="cycle", days=366, store=store, client=client)["error_code"], "malformed_response")


if __name__ == "__main__":
    unittest.main()

