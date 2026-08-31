from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from tools.external_mcp_auth_binding import AUTH_NONE, ExternalMcpAuthBindingRegistry
from tools.external_mcp_surface import (
    current_external_tools,
    list_external_surface,
    model_tool_definitions,
    surface_tool_name,
)
from tools.external_mcp_invocation import ExternalMcpInvocation
from tools.external_mcp_secret_materializer import ExternalMcpSecretMaterializer
from tools.external_mcp_runtime import ExternalMcpRuntime
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_registry import ExternalToolCandidateRegistry
from tools.execution_fence import evaluate_tool_call
from tools.lease_signer import issue_turn_lease


class ExternalSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.servers = ExternalServerRegistry(self.connection, id_factory=lambda: "calendar")
        self.server = self.servers.register(
            display_name="Calendar",
            endpoint="https://calendar.example/mcp",
            provenance="test",
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name) / "key"
        self.key.write_bytes(Fernet.generate_key())
        self.secrets = ExternalSecretStore(
            self.connection, key_file=str(self.key), registry=self.servers
        )
        self.auth = ExternalMcpAuthBindingRegistry(
            self.connection, server_registry=self.servers, secret_store=self.secrets
        )
        self.auth.set_binding(self.server.server_id, AUTH_NONE)
        self.candidates = ExternalToolCandidateRegistry(
            self.connection, server_registry=self.servers
        )
        self.invocation = ExternalMcpInvocation(
            self.connection,
            server_registry=self.servers,
            candidate_registry=self.candidates,
            auth_binding_registry=self.auth,
        )
        self.runtime = ExternalMcpRuntime(
            self.invocation,
            server_registry=self.servers,
            auth_binding_registry=self.auth,
            secret_store=self.secrets,
            materializer=ExternalMcpSecretMaterializer(
                self.secrets, key_file=str(self.key)
            ),
        )

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _ingest(self):
        connected = self.servers.mark_connected(
            self.server.server_id, self.servers.get(self.server.server_id).revision
        )
        self.candidates.ingest({
            "status": "SUCCESS",
            "server_id": connected.server_id,
            "registry_revision": connected.revision,
            "catalog_complete": True,
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "tools": [{
                "name": "calendar.list",
                "description": "List calendar entries",
                "inputSchema": {
                    "type": "object",
                    "properties": {"day": {"type": "string"}},
                },
            }],
            "diagnostics": {"registry_changed_during_attempt": False},
        })

    def test_connected_present_current_is_one_model_tool(self):
        self._ingest()
        catalog = list_external_surface(self._graph())
        self.assertEqual(len(current_external_tools(catalog)), 1)
        tool = catalog[0]["tools"][0]
        self.assertTrue(tool["available"])
        self.assertIn("Calendar", tool["description"])
        self.assertIn("List calendar entries", tool["description"])
        self.assertEqual(
            tool["surface_tool_name"],
            surface_tool_name("calendar", "calendar.list", tool["control_id"]),
        )
        self.assertEqual(tool["source_registry_revision"], catalog[0]["revision"])
        self.assertNotIn("endpoint", tool)
        self.assertNotIn("secret_ref", tool)

    def test_disconnected_candidate_stays_toolroom_gray_and_leaves_model(self):
        self._ingest()
        changed = self.servers.update_connection(
            self.server.server_id, endpoint="https://calendar-2.example/mcp"
        )
        catalog = list_external_surface(self._graph())
        self.assertEqual(catalog[0]["lifecycle_state"], "DISCONNECTED")
        self.assertEqual(catalog[0]["revision"], changed.revision)
        self.assertEqual(len(catalog[0]["tools"]), 1)
        self.assertFalse(catalog[0]["tools"][0]["available"])
        self.assertEqual(current_external_tools(catalog), ())

    def test_toolroom_and_model_use_one_catalog(self):
        catalog = [{
            "server_id": "calendar",
            "display_name": "Calendar",
            "lifecycle_state": "CONNECTED",
            "transport": "STREAMABLE_HTTP",
            "revision": 4,
            "tools": [{
                "control_id": "ext:calendar:roll",
                "remote_tool_name": "roll",
                "description": "Calendar: Roll",
                "input_schema": {"type": "object"},
                "fingerprint": "a" * 64,
                "source_registry_revision": 4,
                "surface_tool_name": "calendar__roll__1234567890",
                "available": True,
            }],
        }]
        with patch("tools.external_mcp_surface.list_external_surface", return_value=catalog):
            from tools import tool_inventory

            inventory_group = tool_inventory._external_groups()[0]
            model_tools = model_tool_definitions()
        self.assertEqual(inventory_group["tools"][0]["tool_name"], "roll")
        self.assertEqual(model_tools[0]["name"], "calendar__roll__1234567890")
        self.assertEqual(model_tools[0]["description"], inventory_group["tools"][0]["display_label"])

    def test_dynamic_pretooluse_binding_uses_current_catalog_result(self):
        lease = issue_turn_lease(
            turn_id="turn-1", turn_mode="chat", issued_from="default_policy"
        )
        surface = {
            "control_id": "ext:calendar:calendar.list",
            "available": True,
        }
        with patch(
            "tools.execution_fence.external_surface_tool", return_value=surface
        ):
            result = evaluate_tool_call(
                "mcp__external__calendar__calendar_list__abc123",
                {"day": "today"},
                lease,
            )
        self.assertEqual(result["lease_decision"], "ALLOW")
        self.assertEqual(result["capability_id"], "external_mcp:ext:calendar:calendar.list")

        with patch(
            "tools.execution_fence.external_surface_tool",
            return_value={**surface, "available": False},
        ):
            denied = evaluate_tool_call(
                "mcp__external__calendar__calendar_list__abc123",
                {},
                lease,
            )
        self.assertEqual(denied["lease_decision"], "DENIED_CAPABILITY")

    def _graph(self):
        class Graph:
            pass

        graph = Graph()
        graph.server_registry = self.servers
        graph.candidate_registry = self.candidates
        graph.runtime = self.runtime
        return graph


if __name__ == "__main__":
    unittest.main()
