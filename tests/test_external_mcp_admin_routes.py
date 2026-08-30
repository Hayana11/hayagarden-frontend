import json
import unittest
from types import SimpleNamespace

from flask import Flask

import external_mcp_admin_routes as routes
from moments_auth import OwnerAuthError


class _GraphContext:
    def __init__(self, graph):
        self.graph = graph
    def __enter__(self):
        return self.graph
    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeGraph:
    def __init__(self, *, discovery=None, register_error=None, auth_error=None):
        self.register_error = register_error
        self.auth_error = auth_error
        self.discovery = discovery or {
            "status": "SUCCESS",
            "catalog_complete": True,
            "tools": [],
            "error": None,
        }
        self.register_calls = []
        self.secret_calls = []
        self.binding_calls = []
        self.discover_calls = []
        self.ingest_calls = []
        self.server = SimpleNamespace(
            server_id="srv-test",
            display_name="Example MCP",
            endpoint="https://example.com/mcp",
            transport="streamable_http",
            lifecycle_state="REGISTERED",
            master_state="OFF",
            revision=1,
        )
        self.server_registry = SimpleNamespace(register=self.register)
        self.secret_store = SimpleNamespace(create=self.create_secret)
        self.auth_binding_registry = SimpleNamespace(set_binding=self.set_binding)
        self.runtime = SimpleNamespace(discover=self.discover)
        self.candidate_registry = SimpleNamespace(ingest=self.ingest)

    def register(self, **kwargs):
        self.register_calls.append(kwargs)
        if self.register_error:
            raise self.register_error
        return self.server

    def create_secret(self, **kwargs):
        self.secret_calls.append(kwargs)
        if self.auth_error:
            raise self.auth_error
        return SimpleNamespace(secret_ref="generated-ref")

    def set_binding(self, *args, **kwargs):
        self.binding_calls.append((args, kwargs))
        if self.auth_error:
            raise self.auth_error
        return SimpleNamespace()

    def discover(self, server_id):
        self.discover_calls.append(server_id)
        return self.discovery

    def ingest(self, result):
        self.ingest_calls.append(result)
        return {"count": len(result.get("tools", []))}


class ExternalMcpAdminRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.graph = _FakeGraph()
        self.app.register_blueprint(
            routes.create_external_mcp_admin_blueprint(
                graph_opener=lambda: _GraphContext(self.graph)
            )
        )
        self.client = self.app.test_client()
        self.original_require_owner = routes.require_owner
        routes.require_owner = lambda request: None

    def tearDown(self):
        routes.require_owner = self.original_require_owner

    def post(self, body):
        return self.client.post("/api/external-mcp/servers", json=body)

    def test_unknown_fields_fail_before_mutation(self):
        response = self.post({
            "display_name": "Example",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "none"},
            "transport": "streamable_http",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.register_calls, [])

    def test_none_creates_explicit_binding_without_secret(self):
        response = self.post({
            "display_name": "Example",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "none"},
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.graph.secret_calls, [])
        self.assertEqual(self.graph.binding_calls[0][0], ("srv-test", "none"))
        self.assertEqual(self.graph.discover_calls, ["srv-test"])
        self.assertEqual(len(self.graph.ingest_calls), 1)

    def test_bearer_uses_secret_store_and_hides_secret_metadata(self):
        credential = "test-bearer-value"
        response = self.post({
            "display_name": "Example",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "bearer", "credential": credential},
        })
        body = response.get_json()
        encoded = json.dumps(body, ensure_ascii=False)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.graph.secret_calls[0]["credential_slot"], "toolroom")
        self.assertNotIn(credential, encoded)
        self.assertNotIn("generated-ref", encoded)
        self.assertNotIn("secret_ref", body)

    def test_discovery_failure_is_truthful_partial_without_ingest(self):
        self.graph.discovery = {
            "status": "BRIDGE_ERROR",
            "catalog_complete": False,
            "tools": [],
            "error": {"code": "BRIDGE_ERROR", "summary": "safe"},
        }
        response = self.post({
            "display_name": "Example",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "none"},
        })
        body = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertTrue(body["connection_saved"])
        self.assertTrue(body["auth_configured"])
        self.assertEqual(body["discovery"]["reason_code"], "BRIDGE_ERROR")
        self.assertEqual(self.graph.ingest_calls, [])

    def test_duplicate_endpoint_is_409(self):
        self.graph.register_error = routes.DuplicateEndpointError("duplicate")
        response = self.post({
            "display_name": "Example",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "none"},
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.graph.secret_calls, [])
        self.assertEqual(self.graph.discover_calls, [])

    def test_owner_auth_denied_before_body_or_graph(self):
        graph_opened = []
        self.app = Flask(__name__)
        self.app.register_blueprint(
            routes.create_external_mcp_admin_blueprint(
                graph_opener=lambda: graph_opened.append(True)
            )
        )
        client = self.app.test_client()
        routes.require_owner = lambda request: (_ for _ in ()).throw(
            OwnerAuthError("unauthorized", 401)
        )
        response = client.post("/api/external-mcp/servers", data="{")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(graph_opened, [])


if __name__ == "__main__":
    unittest.main()
