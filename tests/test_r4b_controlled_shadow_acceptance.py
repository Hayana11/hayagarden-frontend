"""Focused R4B1 retry-contract tests."""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import ombre_read_shadow as shadow
from tools import ombre_read_shadow_worker as worker
from tools import r4b_controlled_shadow_acceptance as acceptance


class R4B1WorkerBudgetTests(unittest.TestCase):
    def test_detached_handoff_uses_widened_budget_and_safe_metadata(self):
        captured = {}

        def capture(_db_path, _event, comparison, **_kwargs):
            captured["comparison"] = comparison

        with mock.patch.object(
            worker.ombre_adapter,
            "get_handoff",
            return_value="shadow handoff",
        ) as get_handoff, mock.patch.object(
            worker,
            "_write_receipt",
            side_effect=capture,
        ):
            worker.run_event(
                {
                    "operation": "handoff",
                    "receipt_db_path": "/tmp/r4b1-test.db",
                    "authoritative": {
                        "available": True,
                        "length": 13,
                        "sha256": shadow.sha256_text("shadow handoff"),
                    },
                }
            )

        get_handoff.assert_called_once_with(timeout=5.0, wall_timeout=8.0)
        metadata = captured["comparison"]["metadata"]
        self.assertEqual(metadata["handoff_timeout_seconds"], 5.0)
        self.assertEqual(metadata["handoff_wall_timeout_seconds"], 8.0)

    def test_third_concurrent_observer_is_recorded_as_dropped_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "receipts.db")
            env = {
                "OMBRE_ADAPTER_BACKEND": "legacy_module",
                "OMBRE_READ_SHADOW_ENABLED": "1",
                "OMBRE_READ_SHADOW_RECORDS_ENABLED": "1",
                "OMBRE_READ_SHADOW_DB_PATH": db,
            }
            slots = [
                shadow.try_acquire_shadow_slot(db),
                shadow.try_acquire_shadow_slot(db),
            ]
            try:
                self.assertTrue(all(slots))
                self.assertFalse(
                    shadow.dispatch_shadow_event(
                        {
                            "operation": "records",
                            "authoritative": [{"id": "one"}],
                        },
                        environ=env,
                    )
                )
                with sqlite3.connect(db) as connection:
                    status = connection.execute(
                        "SELECT status FROM ombre_read_shadow_receipts"
                    ).fetchone()[0]
                self.assertEqual(status, "dropped_busy")
            finally:
                for slot in slots:
                    if slot is not None:
                        slot.close()

    def test_authoritative_observer_dispatch_does_not_join_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            db = str(Path(temp) / "receipts.db")
            env = {
                "OMBRE_ADAPTER_BACKEND": "legacy_module",
                "OMBRE_READ_SHADOW_ENABLED": "1",
                "OMBRE_READ_SHADOW_RECORDS_ENABLED": "1",
                "OMBRE_READ_SHADOW_DB_PATH": db,
            }

            class _Stdin:
                def __init__(self):
                    self.closed = False

                def write(self, _payload):
                    return 0

                def close(self):
                    self.closed = True

            class _Process:
                def __init__(self):
                    self.stdin = _Stdin()
                    self.wait_called = False

                def wait(self, *_args, **_kwargs):
                    self.wait_called = True
                    raise AssertionError("authoritative path joined worker")

            process = _Process()
            with mock.patch(
                "tools.ombre_read_shadow.subprocess.Popen",
                return_value=process,
            ):
                value = [{"id": "one", "content": "opaque"}]
                started = time.monotonic()
                returned = shadow.observe_records(
                    value,
                    bucket_type="dynamic",
                    limit=15,
                    environ=env,
                )
                elapsed = time.monotonic() - started

            self.assertIs(returned, value)
            self.assertFalse(process.wait_called)
            self.assertLess(elapsed, 0.5)

    def test_acceptance_operation_wait_precedes_next_authoritative_call(self):
        events = []

        def call(name, value):
            def run():
                events.append("call:" + name)
                return value
            return run

        receipt = {
            "operation": "handoff",
            "status": "exact",
            "error_code": "",
            "metadata": {},
        }
        receipts = iter(
            (
                dict(receipt),
                dict(receipt, operation="records"),
            )
        )
        with mock.patch.object(acceptance, "receipt_count", return_value=0), mock.patch.object(
            acceptance,
            "wait_for_terminal_receipt",
            side_effect=lambda _before, timeout=20.0: (
                events.append("wait"),
                next(receipts),
            )[1],
        ):
            acceptance._run_operation("handoff", call("handoff", "value"))
            acceptance._run_operation("records", call("records", []))

        self.assertEqual(
            events,
            ["call:handoff", "wait", "call:records", "wait"],
        )


if __name__ == "__main__":
    unittest.main()
