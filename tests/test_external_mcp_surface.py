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
    invoke_external_surface,
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
        self.servers = ExternalServerRegistry(self.connection, id_factory=lambda: "monopoly")
        self.server = self.servers.register(
            display_name="Monopoly",
            endpoint="https://monopoly.example/mcp",
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
                "name": "roll",
                "description": "Roll Monopoly dice",
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
        self.assertIn("Monopoly", tool["description"])
        self.assertIn("Roll Monopoly dice", tool["description"])
        self.assertEqual(
            tool["surface_tool_name"],
            surface_tool_name("monopoly", "roll", tool["control_id"]),
        )
        self.assertEqual(tool["source_registry_revision"], catalog[0]["revision"])
        self.assertNotIn("endpoint", tool)
        self.assertNotIn("secret_ref", tool)

    def test_disconnected_candidate_stays_toolroom_gray_and_leaves_model(self):
        self._ingest()
        changed = self.servers.update_connection(
            self.server.server_id, endpoint="https://monopoly-2.example/mcp"
        )
        catalog = list_external_surface(self._graph())
        self.assertEqual(catalog[0]["lifecycle_state"], "DISCONNECTED")
        self.assertEqual(catalog[0]["revision"], changed.revision)
        self.assertEqual(len(catalog[0]["tools"]), 1)
        self.assertFalse(catalog[0]["tools"][0]["available"])
        self.assertEqual(current_external_tools(catalog), ())

    def test_toolroom_and_model_use_one_catalog(self):
        catalog = [{
            "server_id": "monopoly",
            "display_name": "Monopoly",
            "lifecycle_state": "CONNECTED",
            "transport": "STREAMABLE_HTTP",
            "revision": 4,
            "tools": [{
                "control_id": "ext:monopoly:roll",
                "remote_tool_name": "roll",
                "description": "Monopoly: Roll",
                "input_schema": {"type": "object"},
                "fingerprint": "a" * 64,
                "source_registry_revision": 4,
                "surface_tool_name": "monopoly__roll__1234567890",
                "available": True,
            }],
        }]
        with patch("tools.external_mcp_surface.list_external_surface", return_value=catalog):
            from tools import tool_inventory

            inventory_group = tool_inventory._external_groups()[0]
            model_tools = model_tool_definitions()
        self.assertEqual(inventory_group["tools"][0]["tool_name"], "roll")
        self.assertEqual(model_tools[0]["name"], "monopoly__roll__1234567890")
        self.assertEqual(model_tools[0]["description"], inventory_group["tools"][0]["display_label"])

    def test_surface_name_contract(self):
        first = surface_tool_name("Monopoly", "roll", "ext:monopoly:roll")
        self.assertEqual(first, surface_tool_name("Monopoly", "roll", "ext:monopoly:roll"))
        self.assertTrue(first.startswith("monopoly__roll__"))
        self.assertNotEqual(
            first,
            surface_tool_name("Monopoly", "roll", "ext:other-monopoly:roll"),
        )
        non_ascii = surface_tool_name("中文服务器", "骰子工具", "ext:cn:roll")
        self.assertRegex(non_ascii, r"^[a-z0-9_]+__[a-z0-9_]+__[0-9a-f]{10}$")
        long_name = surface_tool_name("S" * 500, "T" * 500, "ext:long:roll")
        self.assertRegex(long_name, r"^[a-z0-9_]+__[a-z0-9_]+__[0-9a-f]{10}$")

    def test_invoke_maps_surface_to_runtime_once_and_fails_closed(self):
        self._ingest()
        graph = self._graph()
        catalog = list_external_surface(graph)
        surface = catalog[0]["tools"][0]
        calls = []

        class RecordingRuntime:
            def invoke(self, control_id, tool_input, turn_lease, *, expected_turn_id):
                calls.append((control_id, tool_input, turn_lease, expected_turn_id))
                return {
                    "status": "SUCCEEDED",
                    "mcp_result": {
                        "content": [{"type": "text", "text": "rolled"}],
                        "structuredContent": {"value": 6},
                    },
                }

        graph.runtime = RecordingRuntime()

        class GraphContext:
            def __enter__(self):
                return graph

            def __exit__(self, *args):
                return False

        with patch(
            "tools.external_mcp_surface.open_external_mcp_production",
            return_value=GraphContext(),
        ):
            result = invoke_external_surface(
                surface["surface_tool_name"],
                {"sides": 6},
                turn_id="turn-current",
            )
            self.assertEqual(
                result,
                {
                    "status": "SUCCESS",
                    "result": {
                        "content": [{"type": "text", "text": "rolled"}],
                        "structuredContent": {"value": 6},
                    },
                },
            )
            self.assertEqual(
                calls,
                [(surface["control_id"], {"sides": 6}, None, "turn-current")],
            )

            invoke_external_surface(
                surface["surface_tool_name"],
                {"sides": 8},
                turn_id=None,
            )
            invoke_external_surface(
                surface["surface_tool_name"] + "__stale",
                {"sides": 8},
                turn_id="turn-unknown",
            )
            changed = self.servers.update_connection(
                self.server.server_id,
                endpoint="https://monopoly-disconnected.example/mcp",
            )
            self.assertEqual(changed.lifecycle_state, "DISCONNECTED")
            invoke_external_surface(
                surface["surface_tool_name"],
                {"sides": 10},
                turn_id="turn-disconnected",
            )
        self.assertEqual(len(calls), 1)

    def test_invoke_preserves_bounded_tool_error_result(self):
        self._ingest()
        graph = self._graph()
        surface = list_external_surface(graph)[0]["tools"][0]
        remote_error = {
            "content": [
                {"type": "text", "text": "invalid move"},
                {"type": "image", "data": "bounded"},
            ],
            "structuredContent": {"code": "INVALID_MOVE"},
            "isError": True,
        }

        class RecordingRuntime:
            def invoke(self, control_id, tool_input, turn_lease, *, expected_turn_id):
                self.assertEqual(control_id, surface["control_id"])
                self.assertEqual(tool_input, {"move": "bad"})
                self.assertIsNone(turn_lease)
                self.assertEqual(expected_turn_id, "turn-error")
                return {"status": "TOOL_ERROR", "mcp_result": remote_error}

        graph.runtime = RecordingRuntime()

        class GraphContext:
            def __enter__(self):
                return graph

            def __exit__(self, *args):
                return False

        with patch(
            "tools.external_mcp_surface.open_external_mcp_production",
            return_value=GraphContext(),
        ):
            self.assertEqual(
                invoke_external_surface(
                    surface["surface_tool_name"],
                    {"move": "bad"},
                    turn_id="turn-error",
                ),
                {"status": "TOOL_ERROR", "result": remote_error},
            )

    def test_dynamic_pretooluse_binding_uses_current_catalog_result(self):
        lease = issue_turn_lease(
            turn_id="turn-1", turn_mode="chat", issued_from="default_policy"
        )
        surface = {
            "control_id": "ext:monopoly:roll",
            "available": True,
        }
        with patch(
            "tools.execution_fence.external_surface_tool", return_value=surface
        ):
            result = evaluate_tool_call(
                "mcp__external__monopoly__roll__abc123",
                {"day": "today"},
                lease,
            )
        self.assertEqual(result["lease_decision"], "ALLOW")
        self.assertEqual(result["capability_id"], "external_mcp:ext:monopoly:roll")

        with patch(
            "tools.execution_fence.external_surface_tool",
            return_value={**surface, "available": False},
        ):
            denied = evaluate_tool_call(
                "mcp__external__monopoly__roll__abc123",
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
