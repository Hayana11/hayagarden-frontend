"""R4A fail-open Ombre read-shadow contract tests."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import ombre_read_shadow as shadow
from tools import ombre_read_shadow_worker as worker


def enabled_env(db_path: str, **extra: str) -> dict[str, str]:
    env = {
        "OMBRE_ADAPTER_BACKEND": "legacy_module",
        "OMBRE_READ_SHADOW_ENABLED": "1",
        "OMBRE_READ_SHADOW_HANDOFF_ENABLED": "1",
        "OMBRE_READ_SHADOW_RECORDS_ENABLED": "1",
        "OMBRE_READ_SHADOW_EMOTION_ENABLED": "1",
        "OMBRE_READ_SHADOW_SEARCH_ENABLED": "0",
        "OMBRE_READ_SHADOW_DB_PATH": db_path,
    }
    env.update(extra)
    return env


class ShadowGateTests(unittest.TestCase):
    def test_master_gate_defaults_off_and_requires_exact_one(self):
        self.assertFalse(shadow.shadow_enabled({}))
        for value in ("true", "yes", "on", "TRUE", " 1 "):
            self.assertFalse(shadow.shadow_enabled({"OMBRE_READ_SHADOW_ENABLED": value}))
        self.assertTrue(shadow.shadow_enabled({"OMBRE_READ_SHADOW_ENABLED": "1"}))

    def test_operation_gate_cannot_bypass_master(self):
        env = {
            "OMBRE_READ_SHADOW_ENABLED": "0",
            "OMBRE_READ_SHADOW_HANDOFF_ENABLED": "1",
            "OMBRE_READ_SHADOW_RECORDS_ENABLED": "1",
            "OMBRE_READ_SHADOW_EMOTION_ENABLED": "1",
            "OMBRE_READ_SHADOW_SEARCH_ENABLED": "1",
        }
        for operation in ("handoff", "records", "record", "emotion", "search"):
            self.assertFalse(shadow.operation_enabled(operation, env))

    def test_http_authority_never_recursively_shadows(self):
        with tempfile.TemporaryDirectory() as temp:
            env = enabled_env(
                str(Path(temp) / "receipts.db"),
                OMBRE_ADAPTER_BACKEND="http",
            )
            with mock.patch("tools.ombre_read_shadow.subprocess.Popen") as popen:
                result = shadow.dispatch_shadow_event(
                    {"operation": "handoff", "authoritative": {}},
                    environ=env,
                )
            self.assertFalse(result)
            popen.assert_not_called()

    def test_authoritative_result_is_unchanged_when_spawn_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            env = enabled_env(str(Path(temp) / "receipts.db"))
            authoritative = [{"id": "one", "content": "opaque"}]
            with mock.patch(
                "tools.ombre_read_shadow.subprocess.Popen",
                side_effect=OSError("spawn"),
            ):
                returned = shadow.observe_records(
                    authoritative,
                    bucket_type="dynamic",
                    limit=15,
                    environ=env,
                )
            self.assertIs(returned, authoritative)

    def test_authoritative_result_is_unchanged_for_invalid_db(self):
        env = enabled_env("/opt/ombre-brain/memories.db")
        authoritative = "legacy handoff"
        with mock.patch("tools.ombre_read_shadow.subprocess.Popen") as popen:
            returned = shadow.observe_handoff(authoritative, environ=env)
        self.assertIs(returned, authoritative)
        popen.assert_not_called()

    def test_authoritative_result_is_unchanged_when_slot_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            env = enabled_env(str(Path(temp) / "receipts.db"))
            authoritative = {"count": 1, "valence": 0.5, "arousal": 0.3}
            with mock.patch(
                "tools.ombre_read_shadow.try_acquire_shadow_slot",
                return_value=None,
            ):
                returned = shadow.observe_emotion(authoritative, environ=env)
            self.assertIs(returned, authoritative)

    def test_authoritative_result_is_unchanged_when_sidecar_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            env = enabled_env(str(Path(temp) / "receipts.db"))
            authoritative = "legacy handoff"
            def unavailable(*_args, **_kwargs):
                raise ConnectionError("sidecar")
            returned = shadow.observe_handoff(
                authoritative,
                environ=env,
                dispatch=unavailable,
            )
            self.assertIs(returned, authoritative)

    def test_at_most_two_cross_process_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "receipts.db")
            slots = [
                shadow.try_acquire_shadow_slot(path),
                shadow.try_acquire_shadow_slot(path),
            ]
            self.assertTrue(all(slots))
            self.assertIsNone(shadow.try_acquire_shadow_slot(path))
            for slot in slots:
                slot.close()


class ReceiptSafetyTests(unittest.TestCase):
    def test_receipt_path_rejects_production_memories_embeddings_and_root(self):
        cases = (
            "/opt/ombre-brain/memories.db",
            "/opt/ombre-brain/buckets/embeddings.db",
            "/opt/ombre-brain",
        )
        for path in cases:
            with self.subTest(path=path):
                self.assertIsNone(
                    shadow.resolve_receipt_db_path(
                        {"OMBRE_READ_SHADOW_DB_PATH": path}
                    )
                )

    def test_raw_content_and_query_are_not_written(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "receipts.db")
            query = "记忆系统"
            with mock.patch.object(
                worker.ombre_adapter,
                "search_memories",
                return_value=[],
            ):
                worker.run_event({
                    "operation": "search",
                    "receipt_db_path": db,
                    "query": query,
                    "limit": 5,
                    "authoritative": {
                        "name_hashes": [],
                        "content_hashes": [],
                        "result_hash": shadow._hash_json({"names": [], "contents": []}),
                    },
                })
            raw = Path(db).read_bytes()
            self.assertNotIn(query.encode("utf-8"), raw)
            self.assertIn(hashlib.sha256(query.encode("utf-8")).hexdigest().encode(), raw)
            connection = sqlite3.connect(db)
            rows = connection.execute(
                "SELECT metadata_json FROM ombre_read_shadow_receipts"
            ).fetchall()
            connection.close()
            self.assertEqual(len(rows), 1)
            self.assertNotIn(b"private memory body", raw)

    def test_write_surfaces_are_not_exposed_by_r4a_observer(self):
        source = Path(shadow.__file__).read_text(encoding="utf-8")
        self.assertNotIn("hold_memory", source)
        self.assertNotIn("update_memory_emotion", source)
        self.assertFalse(hasattr(shadow, "observe_write"))


class ShadowWorkerTests(unittest.TestCase):
    def test_sidecar_unavailable_is_recorded_without_raw_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "receipts.db")
            with mock.patch.object(worker.ombre_adapter, "get_handoff", return_value=None):
                worker.run_event({
                    "operation": "handoff",
                    "receipt_db_path": db,
                    "authoritative": {
                        "available": True,
                        "length": 10,
                        "sha256": "a" * 64,
                    },
                })
            connection = sqlite3.connect(db)
            status = connection.execute(
                "SELECT status FROM ombre_read_shadow_receipts"
            ).fetchone()[0]
            connection.close()
            self.assertEqual(status, "shadow_unavailable")


if __name__ == "__main__":
    unittest.main()
