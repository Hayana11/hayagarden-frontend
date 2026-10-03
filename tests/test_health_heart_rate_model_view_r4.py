from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tools import health_store
from tools.xiaomi_health import internal_adapter
from tools.xiaomi_health.client import XiaomiProviderError


def _stamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ingest(db: str, records: list[dict], statuses=None, collected_at=None):
    collected_at = collected_at or records[0]["sampled_at"]
    payload = {
        "schemaVersion": 1,
        "collectedAt": collected_at,
        "metricStatuses": statuses or {
            "heart_rate": {"status": "PASS", "source": "health_connect"},
            "resting_heart_rate": {"status": "PASS", "source": "health_connect"},
            "steps": {"status": "EMPTY", "source": "health_connect"},
            "sleep": {"status": "PASS", "source": "health_connect"},
        },
        "records": records,
    }
    return health_store.ingest_payload(payload, db)


def _hr(sampled_at: str, value: float, record_id: str, date=None) -> dict:
    return {
        "metric": "heart_rate",
        "sampled_at": sampled_at,
        "data_date": date or sampled_at[:10],
        "value": value,
        "unit": "bpm",
        "source": "health_connect",
        "source_record_id": record_id,
        "collected_at": sampled_at,
    }


class HeartRateModelViewR4Tests(unittest.TestCase):
    def test_store_accepts_resting_heart_rate(self):
        now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
        stamp = _stamp(now)
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            _ingest(db, [{
                "metric": "resting_heart_rate",
                "sampled_at": stamp,
                "data_date": "2026-10-03",
                "value": 61,
                "unit": "bpm",
                "source": "health_connect",
                "source_record_id": "resting-1",
                "collected_at": stamp,
            }])
            latest = health_store.get_local_metric(db, "resting_heart_rate", 7, now=_stamp(now))
            self.assertEqual(latest["records"][0]["value"], 61.0)
            self.assertEqual(latest["records"][0]["unit"], "bpm")

    def test_snapshot_is_now_relative_and_uses_two_hour_freshness(self):
        now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
        fresh = _stamp(now - dt.timedelta(minutes=20))
        older_in_hour = _stamp(now - dt.timedelta(minutes=50))
        outside_hour = _stamp(now - dt.timedelta(hours=3))
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            _ingest(db, [
                _hr(outside_hour, 48, "old"),
                _hr(older_in_hour, 64, "mid"),
                _hr(fresh, 80, "new"),
            ], collected_at=fresh)
            snapshot = health_store.get_heart_rate_snapshot(db, now=_stamp(now))
            self.assertEqual(snapshot["view"], "snapshot")
            self.assertEqual(snapshot["value"], 80.0)
            self.assertEqual(snapshot["sampledAt"], fresh)
            self.assertEqual(snapshot["ageSeconds"], 20 * 60)
            self.assertFalse(snapshot["stale"])
            self.assertEqual(snapshot["lastHour"]["min"], 64.0)
            self.assertEqual(snapshot["lastHour"]["max"], 80.0)
            self.assertEqual(snapshot["lastHour"]["avg"], 72.0)
            self.assertEqual(snapshot["lastHour"]["samples"], 2)
            self.assertNotIn("records", snapshot)

            stale_now = _stamp(now + dt.timedelta(hours=3))
            aged = health_store.get_heart_rate_snapshot(db, now=stale_now)
            self.assertTrue(aged["stale"])
            self.assertGreater(aged["ageSeconds"], health_store.HEART_RATE_STALE_AFTER_SECONDS)
            self.assertEqual(aged["value"], 80.0)
            self.assertEqual(aged["lastHour"]["samples"], 0)

    def test_daily_summaries_include_resting_without_raw_samples(self):
        now = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc)
        day = now.date()
        rows = []
        for offset, values in ((0, [70, 90]), (1, [55]), (2, [60, 61, 120])):
            date = (day - dt.timedelta(days=offset)).isoformat()
            for index, value in enumerate(values):
                stamp = _stamp(now - dt.timedelta(days=offset, minutes=index))
                rows.append(_hr(stamp, value, f"hr-{offset}-{index}", date=date))
            rows.append({
                "metric": "resting_heart_rate",
                "sampled_at": _stamp(now - dt.timedelta(days=offset, hours=8)),
                "data_date": date,
                "value": 58 + offset,
                "unit": "bpm",
                "source": "health_connect",
                "source_record_id": f"rest-{offset}",
                "collected_at": _stamp(now),
            })
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            _ingest(db, rows, collected_at=_stamp(now))
            daily = health_store.get_heart_rate_daily(db, 7, now=_stamp(now))
            self.assertEqual(daily["view"], "daily")
            self.assertLessEqual(len(daily["records"]), 7)
            self.assertEqual(daily["records"][0]["dataDate"], day.isoformat())
            self.assertEqual(daily["records"][0]["min"], 70.0)
            self.assertEqual(daily["records"][0]["max"], 90.0)
            self.assertEqual(daily["records"][0]["sampleCount"], 2)
            self.assertEqual(daily["records"][0]["restingHeartRate"], 58.0)
            for row in daily["records"]:
                self.assertNotIn("value", row)
                self.assertNotIn("sampledAt", row)

    def test_sleep_heart_rate_summary_is_optional(self):
        now = dt.datetime(2026, 10, 3, 8, 0, tzinfo=dt.timezone.utc)
        start = _stamp(now - dt.timedelta(hours=7))
        end = _stamp(now)
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            _ingest(db, [
                _hr(_stamp(now - dt.timedelta(hours=6)), 52, "sleep-hr-1"),
                _hr(_stamp(now - dt.timedelta(hours=3)), 58, "sleep-hr-2"),
                _hr(_stamp(now - dt.timedelta(hours=8)), 99, "outside"),
                {
                    "metric": "sleep",
                    "sampled_at": end,
                    "data_date": end[:10],
                    "value": 420,
                    "unit": "minutes",
                    "details": {
                        "startAt": start,
                        "endAt": end,
                        "stages": [{"stage": 1, "startAt": start, "endAt": end}],
                    },
                    "source": "health_connect",
                    "source_record_id": "sleep-1",
                    "collected_at": end,
                },
            ], collected_at=end)
            summary = health_store.get_sleep_heart_rate(db, start, end)
            self.assertEqual(summary["samples"], 2)
            self.assertEqual(summary["min"], 52.0)
            self.assertEqual(summary["avg"], 55.0)
            self.assertIsNone(health_store.get_sleep_heart_rate(db, _stamp(now + dt.timedelta(hours=1)), _stamp(now + dt.timedelta(hours=2))))

    def test_adapter_omitted_days_is_snapshot_and_explicit_days_is_daily(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        start = _stamp(now - dt.timedelta(hours=6))
        end = _stamp(now)
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            _ingest(db, [
                _hr(_stamp(now - dt.timedelta(minutes=10)), 74, "latest"),
                _hr(_stamp(now - dt.timedelta(minutes=40)), 66, "hour"),
                {
                    "metric": "resting_heart_rate",
                    "sampled_at": _stamp(now - dt.timedelta(hours=2)),
                    "data_date": now.date().isoformat(),
                    "value": 60,
                    "unit": "bpm",
                    "source": "health_connect",
                    "source_record_id": "rest",
                    "collected_at": end,
                },
                {
                    "metric": "sleep",
                    "sampled_at": end,
                    "data_date": end[:10],
                    "value": 375,
                    "unit": "minutes",
                    "details": {"startAt": start, "endAt": end, "stages": [{"stage": 2, "startAt": start, "endAt": end}]},
                    "source": "health_connect",
                    "source_record_id": "sleep-latest",
                    "collected_at": end,
                },
                {
                    "metric": "steps",
                    "sampled_at": end,
                    "data_date": end[:10],
                    "value": 1200,
                    "unit": "steps",
                    "source": "health_connect",
                    "source_record_id": "steps-1",
                    "collected_at": end,
                },
            ], statuses={
                "heart_rate": {"status": "PASS", "source": "health_connect"},
                "resting_heart_rate": {"status": "PASS", "source": "health_connect"},
                "steps": {"status": "PASS", "source": "health_connect"},
                "sleep": {"status": "PASS", "source": "health_connect"},
            }, collected_at=end)
            old = internal_adapter.LOCAL_DB_PATH

            class Store:
                def status(self):
                    return {"connected": False, "auth_state": "auth_expired"}

            class Client:
                def get_latest_partial(self, days, request_timeout=None):
                    raise XiaomiProviderError("auth_expired")

                def get_cycle(self, days, request_timeout=None):
                    raise XiaomiProviderError("auth_expired")

                def get_series(self, metric, days, request_timeout=None):
                    raise XiaomiProviderError("auth_expired")

            try:
                internal_adapter.LOCAL_DB_PATH = db
                snapshot = internal_adapter.run("get_health", metric="heart_rate", store=Store(), client=Client())
                daily = internal_adapter.run("get_health", metric="heart_rate", days=7, store=Store(), client=Client())
                all_default = internal_adapter.run("get_health", metric="all", store=Store(), client=Client())
                all_days = internal_adapter.run("get_health", metric="all", days=7, store=Store(), client=Client())
            finally:
                internal_adapter.LOCAL_DB_PATH = old

        self.assertEqual(snapshot["view"], "snapshot")
        self.assertEqual(snapshot["value"], 74.0)
        self.assertIsInstance(snapshot["ageSeconds"], int)
        self.assertIn("min", snapshot["lastHour"])
        self.assertNotIn("records", snapshot)
        self.assertEqual(daily["view"], "daily")
        self.assertLessEqual(len(daily["records"]), 7)
        self.assertEqual(daily["records"][0]["restingHeartRate"], 60.0)
        self.assertNotIn("value", daily["records"][0])
        self.assertEqual(all_default["heart_rate"]["value"], 74.0)
        self.assertIn("lastHour", all_default["heart_rate"])
        self.assertNotIn("records", all_default["heart_rate"])
        self.assertEqual(all_default["sleep"]["value"], 375)
        self.assertEqual(all_default["sleep"]["details"]["stages"][0]["stage"], 2)
        self.assertEqual(all_default["sleep"]["heartRate"]["samples"], 2)
        self.assertEqual(all_default["steps"]["value"], 1200)
        self.assertEqual(all_days["heart_rate"]["view"], "daily")
        self.assertLessEqual(len(all_days["heart_rate"]["records"]), 7)
        self.assertEqual(all_days["sleep"]["value"], 375)
        self.assertEqual(all_days["cycle"]["error_code"], "auth_expired")


if __name__ == "__main__":
    unittest.main()
