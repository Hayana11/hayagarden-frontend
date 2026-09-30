from __future__ import annotations

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tools import health_store
from tools.xiaomi_health import internal_adapter
from health_ingest_routes import create_health_blueprint


def payload(records=None, statuses=None):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    stamp = now.isoformat().replace("+00:00", "Z")
    return {
        "schemaVersion": 1,
        "collectedAt": stamp,
        "metricStatuses": statuses or {
            "heart_rate": {"status": "PASS", "source": "health_connect"},
            "steps": {"status": "EMPTY", "source": "health_connect"},
            "sleep": {"status": "PERMISSION_DENIED", "source": "health_connect"},
        },
        "records": records or [{
            "metric": "heart_rate",
            "sampled_at": stamp,
            "data_date": stamp[:10],
            "value": 72,
            "unit": "bpm",
            "details": {"resting_heart_rate": 61},
            "source": "health_connect",
            "source_record_id": "hr-1",
            "collected_at": stamp,
        }],
    }


class HealthBridgeR1Tests(unittest.TestCase):
    def test_ingest_requires_auth_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            app = __import__("flask").Flask(__name__)
            app.register_blueprint(create_health_blueprint(db_path=db, token_getter=lambda: "secret"))
            client = app.test_client()
            body = payload()
            unauthorized = client.post("/api/health/mobile/ingest", json=body)
            self.assertEqual(unauthorized.status_code, 401)
            first = client.post(
                "/api/health/mobile/ingest",
                json=body,
                headers={"Authorization": "Bearer secret"},
            )
            second = client.post(
                "/api/health/mobile/ingest",
                json=body,
                headers={"Authorization": "Bearer secret"},
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json["accepted"], 1)
            self.assertEqual(second.json["accepted"], 0)
            self.assertEqual(second.json["duplicates"], 1)

    def test_validation_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            row = payload()["records"][0]
            result = health_store.ingest_payload(payload(), db)
            self.assertEqual(result["accepted"], 1)
            latest = health_store.get_local_metric(db, "heart_rate", 7)
            self.assertEqual(latest["records"][0]["source"], "health_connect")
            self.assertEqual(latest["records"][0]["sourceRecordId"], "hr-1")
            future = dict(row)
            future["sampled_at"] = "2999-01-01T00:00:00Z"
            with self.assertRaisesRegex(ValueError, "future sampled_at"):
                health_store.ingest_payload(payload([future]), db)
            invalid = dict(row, metric="calories")
            with self.assertRaisesRegex(ValueError, "invalid metric"):
                health_store.ingest_payload(payload([invalid]), db)

    def test_provider_resolution_local_fresh_wins_and_unavailable_falls_back(self):
        class Store:
            def status(self):
                return {"connected": True, "auth_state": "valid"}

        class Client:
            def get_series(self, metric, days):
                return {"status": "PASS", "records": [{"sampledAt": "2026-09-30T00:00:00Z", "dataDate": "2026-09-30", "value": 99, "unit": "bpm"}]}
            def get_latest_partial(self, days, request_timeout=None):
                return {"status": "PASS", "heart_rate": {"sampledAt": "2026-09-30T00:00:00Z", "dataDate": "2026-09-30", "value": 99, "unit": "bpm"}, "metric_status": {}}
            def get_cycle(self, days, request_timeout=None):
                return {"status": "EMPTY", "events": [], "periods": [], "symptoms": []}

        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            health_store.ingest_payload(payload(), db)
            old = internal_adapter.LOCAL_DB_PATH
            try:
                internal_adapter.LOCAL_DB_PATH = db
                local = internal_adapter.run("get_health", metric="heart_rate", days=7, store=Store(), client=Client())
                self.assertEqual(local["source"], "health_connect")
                self.assertEqual(local["records"][0]["value"], 72.0)
                internal_adapter.LOCAL_DB_PATH = str(Path(directory) / "missing.db")
                fallback = internal_adapter.run("get_health", metric="heart_rate", days=7, store=Store(), client=Client())
                self.assertEqual(fallback["source"], "xiaomi_fitness_cloud")
            finally:
                internal_adapter.LOCAL_DB_PATH = old

    def test_all_mixed_source_and_cycle_remains_cloud(self):
        class Store:
            def status(self):
                return {"connected": True, "auth_state": "valid"}
        class Client:
            def get_latest_partial(self, days, request_timeout=None):
                return {
                    "status": "PASS",
                    "steps": {"sampledAt": "2026-09-30T00:00:00Z", "dataDate": "2026-09-30", "value": 10, "unit": "steps"},
                    "metric_status": {"sleep": {"status": "EMPTY"}, "heart_rate": {"status": "EMPTY"}},
                }
            def get_cycle(self, days, request_timeout=None):
                return {"status": "EMPTY", "events": [], "periods": [], "symptoms": []}
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            health_store.ingest_payload(payload(), db)
            old = internal_adapter.LOCAL_DB_PATH
            try:
                internal_adapter.LOCAL_DB_PATH = db
                result = internal_adapter.run("get_health", metric="all", days=7, store=Store(), client=Client())
                self.assertEqual(result["provider"], "mixed")
                self.assertEqual(result["metric_status"]["heart_rate"]["source"], "health_connect")
                self.assertEqual(result["metric_status"]["steps"]["source"], "xiaomi_fitness_cloud")
                self.assertEqual(result["cycle"]["source"], "xiaomi_fitness_cloud")
            finally:
                internal_adapter.LOCAL_DB_PATH = old

    def test_external_health_contract_and_wake_binding_are_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        manifest = (root / "tools" / "capability_manifest.py").read_text()
        mcp = (root / "internal-mcp-server.js").read_text()
        wake = (root / "tests" / "test_wake_read_observation.py").read_text()
        self.assertIn("health.read", manifest)
        self.assertIn("mcp__internal__get.health", manifest)
        self.assertIn("get.health", mcp)
        self.assertIn("health.read", wake)

    def test_permission_denied_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            denied = payload(statuses={
                "heart_rate": {"status": "PERMISSION_DENIED", "source": "health_connect"},
                "steps": {"status": "EMPTY", "source": "health_connect"},
                "sleep": {"status": "UNAVAILABLE", "source": "health_connect"},
            })
            denied["records"] = []
            health_store.ingest_payload(denied, db)
            result = health_store.get_local_metric(db, "heart_rate", 7)
            self.assertEqual(result["status"], "PERMISSION_DENIED")
            self.assertEqual(result["records"], [])


    def test_last_good_records_keep_denied_status_and_cloud_fallback(self):
        class Store:
            def status(self):
                return {"connected": True, "auth_state": "valid"}

        class Client:
            def get_series(self, metric, days):
                return {
                    "status": "PASS",
                    "records": [{
                        "sampledAt": "2026-09-30T00:00:00Z",
                        "dataDate": "2026-09-30",
                        "value": 99,
                        "unit": "bpm",
                    }],
                }

        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            health_store.ingest_payload(payload(), db, now="2026-09-30T12:00:00Z")
            denied = payload(statuses={
                "heart_rate": {"status": "PERMISSION_DENIED", "source": "health_connect"},
                "steps": {"status": "EMPTY", "source": "health_connect"},
                "sleep": {"status": "UNAVAILABLE", "source": "health_connect"},
            })
            denied["records"] = []
            health_store.ingest_payload(denied, db, now="2026-09-30T12:01:00Z")
            local = health_store.get_local_metric(
                db, "heart_rate", 7, now="2026-09-30T12:02:00Z"
            )
            self.assertEqual(local["status"], "PERMISSION_DENIED")
            self.assertEqual(len(local["records"]), 1)
            old = internal_adapter.LOCAL_DB_PATH
            try:
                internal_adapter.LOCAL_DB_PATH = db
                resolved = internal_adapter.run(
                    "get_health", metric="heart_rate", days=7,
                    store=Store(), client=Client()
                )
                self.assertEqual(resolved["source"], "xiaomi_fitness_cloud")
                self.assertEqual(resolved["records"][0]["value"], 99)
            finally:
                internal_adapter.LOCAL_DB_PATH = old

    def test_server_receipt_updates_last_upload_at(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "health.db")
            health_store.ingest_payload(payload(), db, now="2026-09-30T12:00:00Z")
            latest = health_store.get_local_metric(
                db, "heart_rate", 7, now="2026-09-30T12:01:00Z"
            )
            self.assertEqual(latest["lastUploadAt"], "2026-09-30T12:00:00.000Z")
            status = health_store.get_status(db, now="2026-09-30T12:01:00Z")
            self.assertEqual(
                status["metrics"]["heart_rate"]["lastUploadAt"],
                "2026-09-30T12:00:00.000Z",
            )


if __name__ == "__main__":
    unittest.main()
