from __future__ import annotations

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from tools.external_mcp_discovery import (
    BRIDGE_ERROR,
    BRIDGE_UNAVAILABLE,
    BridgeFailure,
    ExternalMcpDiscovery,
    STALE,
    SubprocessDiscoveryRunner,
    discover_external_server,
)
from tools.external_server_registry import (
    ExternalServerRegistry,
    REGISTRATION_STATE,
    REVIEW_REQUIRED_STATE,
    REVOKED_STATE,
)


SUCCESS = {
    "status": "SUCCESS",
    "catalog_complete": True,
    "zero_tools": False,
    "server_metadata": {"server_info": {"name": "fixture"}},
    "tools": [
        {
            "name": "calendar.list",
            "description": "complete SDK-visible record",
            "inputSchema": {"type": "object"},
            "outputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        }
    ],
    "diagnostics": {"request_count": 1},
    "error": None,
}


class ExternalMcpDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.registry = ExternalServerRegistry(self.connection)
        self.record = self.registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="owner-admin",
        )

    def tearDown(self):
        self.connection.close()

    def test_production_primitive_only_accepts_server_id(self):
        self.assertEqual(list(inspect.signature(discover_external_server).parameters), ["registry", "server_id"])
        self.assertEqual(list(inspect.signature(ExternalMcpDiscovery.discover).parameters), ["self", "server_id"])
        with self.assertRaises(TypeError):
            ExternalMcpDiscovery(self.registry, runner=lambda _: SUCCESS).discover(
                self.record.server_id, endpoint="https://caller.example/override"
            )

    def test_registered_and_review_required_use_exact_registry_snapshot(self):
        calls = []

        def runner(record):
            calls.append(record)
            return SUCCESS

        facade = ExternalMcpDiscovery(self.registry, runner=runner)
        registered = facade.discover(self.record.server_id)
        self.assertEqual(registered["status"], "SUCCESS")
        self.assertEqual(registered["server_id"], self.record.server_id)
        self.assertEqual(registered["registration_provenance"], "owner-admin")
        self.assertEqual(registered["registry_revision"], self.record.revision)
        self.assertEqual(registered["lifecycle_state"], REGISTRATION_STATE)
        self.assertEqual(registered["endpoint_snapshot"], self.record.endpoint)
        self.assertEqual(calls[0].endpoint, self.record.endpoint)
        self.assertEqual(calls[0].transport, self.record.transport)
        self.assertEqual(registered["tools"], SUCCESS["tools"])

        reviewed = self.registry.update_connection(
            self.record.server_id, endpoint="https://calendar-new.example/mcp"
        )
        reviewed_result = facade.discover(reviewed.server_id)
        self.assertEqual(reviewed_result["status"], "SUCCESS")
        self.assertEqual(reviewed_result["lifecycle_state"], REVIEW_REQUIRED_STATE)
        self.assertEqual(self.registry.get(reviewed.server_id).lifecycle_state, REVIEW_REQUIRED_STATE)
        self.assertEqual(self.registry.get(reviewed.server_id).master_state, "OFF")
        self.assertEqual(self.registry.get(reviewed.server_id).revision, reviewed.revision)
        self.assertEqual(calls[-1].endpoint, reviewed.endpoint)

    def test_unknown_and_revoked_fail_before_child_spawn(self):
        calls = []
        facade = ExternalMcpDiscovery(self.registry, runner=lambda record: calls.append(record) or SUCCESS)
        unknown = facade.discover("unknown-server")
        self.assertEqual(unknown["status"], "UNKNOWN_SERVER")
        self.assertEqual(self.registry.get(self.record.server_id).lifecycle_state, REGISTRATION_STATE)
        self.registry.revoke(self.record.server_id)
        revoked = facade.discover(self.record.server_id)
        self.assertEqual(revoked["status"], REVOKED_STATE)
        self.assertEqual(calls, [])

    def test_success_does_not_mutate_registry_and_failure_classification_is_preserved(self):
        before = self.registry.get(self.record.server_id)
        success = ExternalMcpDiscovery(self.registry, runner=lambda _: SUCCESS).discover(self.record.server_id)
        self.assertEqual(success["status"], "SUCCESS")
        self.assertEqual(self.registry.get(self.record.server_id), before)

        def unavailable(_record):
            return {
                **SUCCESS,
                "status": "AUTH_REQUIRED",
                "catalog_complete": False,
                "zero_tools": False,
                "tools": [],
                "error": {"code": "AUTH_REQUIRED", "summary": "authentication required"},
            }

        failed = ExternalMcpDiscovery(self.registry, runner=unavailable).discover(self.record.server_id)
        self.assertEqual(failed["status"], "AUTH_REQUIRED")
        self.assertEqual(self.registry.get(self.record.server_id), before)

    def test_registry_races_invalidate_success_catalog(self):
        for mutation in (
            "rename",
            "endpoint",
            "revoke",
        ):
            connection = sqlite3.connect(":memory:")
            registry = ExternalServerRegistry(connection)
            record = registry.register(
                display_name="Calendar",
                endpoint="https://calendar.example/mcp",
                provenance="owner-admin",
            )

            def runner(_record, mutation=mutation):
                if mutation == "rename":
                    registry.rename(record.server_id, "Renamed")
                elif mutation == "endpoint":
                    registry.update_connection(
                        record.server_id, endpoint="https://calendar-race.example/mcp"
                    )
                else:
                    registry.revoke(record.server_id)
                return SUCCESS

            result = ExternalMcpDiscovery(registry, runner=runner).discover(record.server_id)
            self.assertEqual(result["status"], STALE)
            self.assertFalse(result["catalog_complete"])
            self.assertEqual(result["tools"], [])
            self.assertTrue(result["diagnostics"]["registry_changed_during_attempt"])
            connection.close()

    def test_registry_race_does_not_turn_remote_failure_into_success(self):
        def runner(_record):
            self.registry.update_connection(
                self.record.server_id, endpoint="https://calendar-failure-race.example/mcp"
            )
            return {
                **SUCCESS,
                "status": "UNAVAILABLE",
                "catalog_complete": False,
                "zero_tools": False,
                "tools": [],
                "error": {"code": "UNAVAILABLE", "summary": "remote unavailable"},
            }

        result = ExternalMcpDiscovery(self.registry, runner=runner).discover(self.record.server_id)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertTrue(result["diagnostics"]["registry_changed_during_attempt"])
        self.assertEqual(result["tools"], [])

    def test_bridge_failure_is_local_and_registry_remains_unchanged(self):
        before = self.registry.get(self.record.server_id)
        result = ExternalMcpDiscovery(
            self.registry,
            runner=lambda _: (_ for _ in ()).throw(BridgeFailure(BRIDGE_ERROR, "bad local output")),
        ).discover(self.record.server_id)
        self.assertEqual(result["status"], BRIDGE_ERROR)
        self.assertEqual(self.registry.get(self.record.server_id), before)


class SubprocessBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)
        self.connection = sqlite3.connect(":memory:")
        self.registry = ExternalServerRegistry(self.connection)
        self.record = self.registry.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="owner-admin",
        )

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def fake_child(self, body: str) -> Path:
        path = self.directory / "fake_child.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def runner(self, path: Path, timeout=1.0):
        return SubprocessDiscoveryRunner(
            node_executable=sys.executable,
            bridge_path=path,
            timeout_seconds=timeout,
        )

    def test_child_gets_minimal_environment_and_success_stdout_is_strict_json(self):
        marker = "M5_PARENT_SECRET_MARKER"
        child = self.fake_child(
            """
            import json, os, sys
            sys.stdin.buffer.read()
            sys.stderr.write('remote body must not escape\\n')
            print(json.dumps({
                'bridge_version': 1, 'status': 'SUCCESS', 'catalog_complete': True,
                'zero_tools': True, 'tools': [], 'diagnostics': {
                    'secret_seen': 'M5_PARENT_SECRET_MARKER' in os.environ,
                }, 'error': None,
            }), end='')
            """
        )
        old = os.environ.get(marker)
        os.environ[marker] = "must-not-cross"
        try:
            result = self.runner(child).run(self.record)
        finally:
            if old is None:
                os.environ.pop(marker, None)
            else:
                os.environ[marker] = old
        self.assertEqual(result["status"], "SUCCESS")
        self.assertFalse(result["diagnostics"]["secret_seen"])
        self.assertNotIn("remote body", json.dumps(result))

    def test_nonzero_malformed_wrong_version_and_oversized_output_fail_closed(self):
        cases = (
            ("import sys; sys.stdin.buffer.read(); sys.exit(3)", BRIDGE_ERROR),
            ("import sys; sys.stdin.buffer.read(); print('not-json')", BRIDGE_ERROR),
            (
                "import json, sys; sys.stdin.buffer.read(); print(json.dumps({'bridge_version': 99}))",
                BRIDGE_ERROR,
            ),
            ("import sys; sys.stdin.buffer.read(); sys.stdout.write('x' * (5 * 1024 * 1024))", BRIDGE_ERROR),
        )
        for body, expected in cases:
            with self.subTest(expected=expected):
                self.assertRaises(
                    BridgeFailure,
                    lambda body=body: self.runner(self.fake_child(body)).run(self.record),
                )

    def test_child_timeout_is_finite_and_has_no_retry(self):
        child = self.fake_child(
            """
            import sys, time
            sys.stdin.buffer.read()
            time.sleep(0.5)
            """
        )
        started = time.monotonic()
        with self.assertRaises(BridgeFailure) as raised:
            self.runner(child, timeout=0.05).run(self.record)
        self.assertEqual(raised.exception.status, BRIDGE_UNAVAILABLE)
        self.assertLess(time.monotonic() - started, 0.4)


if __name__ == "__main__":
    unittest.main()

