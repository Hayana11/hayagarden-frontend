import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from flask import Flask

import external_mcp_admin_routes as routes
from moments_auth import OwnerAuthError
from tools.external_mcp_auth_binding import ExternalMcpAuthBindingRegistry
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_registry import ExternalToolCandidateRegistry


class _GraphContext:
    def __init__(self, graph):
        self.graph = graph

    def __enter__(self):
        return self.graph

    def __exit__(self, exc_type, exc, tb):
        return False


class _Runtime:
    def __init__(self, graph, *, status="SUCCESS", catalog_complete=True, tools=None, error=None):
        self.graph = graph
        self.status = status
        self.catalog_complete = catalog_complete
        self.tools = list(tools or [])
        self.error = error
        self.discover_calls = []
        self.invoke_calls = 0

    def discover(self, server_id):
        self.discover_calls.append(server_id)
        server = self.graph.server_registry.get(server_id)
        if self.status != "SUCCESS":
            return {
                "status": self.status,
                "catalog_complete": False,
                "tools": [],
                "error": self.error or {"code": "BRIDGE_ERROR", "summary": "redacted"},
            }
        return {
            "status": "SUCCESS",
            "catalog_complete": self.catalog_complete,
            "zero_tools": not self.tools,
            "tools": list(self.tools),
            "diagnostics": {"registry_changed_during_attempt": False},
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "error": None,
            "server_id": server.server_id,
            "registry_revision": server.revision,
            "lifecycle_state": server.lifecycle_state,
            "master_state": server.master_state,
            "model_visible": False,
            "execution_allowed": False,
        }

    def invoke(self, *args, **kwargs):
        self.invoke_calls += 1
        raise AssertionError("admin route must never invoke tools")


class _CountingCandidateRegistry:
    def __init__(self, actual):
        self.actual = actual
        self.calls = 0

    def ingest(self, result):
        self.calls += 1
        return self.actual.ingest(result)


class _FailingIngest:
    def __init__(self, actual):
        self.actual = actual
        self.calls = 0

    def ingest(self, result):
        self.calls += 1
        raise RuntimeError("candidate ingest failure must not reach response")


class _FailingBinding:
    def set_binding(self, *args, **kwargs):
        raise RuntimeError("binding failure")


class _HermeticGraph:
    def __init__(self, *, runtime=None):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db_path = root / "external-mcp.db"
        self.key_path = root / "credentials.key"
        self.key_path.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            self.key_path.chmod(0o600)
        self.connection = sqlite3.connect(self.db_path)
        self.server_registry = ExternalServerRegistry(
            self.connection, id_factory=lambda: "srv-test"
        )
        self.secret_store = ExternalSecretStore(
            self.connection,
            key_file=self.key_path,
            registry=self.server_registry,
            id_factory=lambda: "extsecret_test",
        )
        self.auth_binding_registry = ExternalMcpAuthBindingRegistry(
            self.connection,
            server_registry=self.server_registry,
            secret_store=self.secret_store,
        )
        self.candidate_registry = ExternalToolCandidateRegistry(
            self.connection, server_registry=self.server_registry
        )
        self.runtime = runtime or _Runtime(self)

    def close(self):
        self.connection.close()
        self.tempdir.cleanup()


class ExternalMcpAdminRouteTests(unittest.TestCase):
    def setUp(self):
        self.graph = _HermeticGraph()
        self.app = Flask(__name__)
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
        self.graph.close()

    @staticmethod
    def payload(*, scheme="none", credential=None, **extra):
        auth = {"scheme": scheme}
        if scheme == "bearer":
            auth["credential"] = credential
        value = {
            "display_name": "Example MCP",
            "endpoint": "https://example.com/mcp",
            "auth": auth,
        }
        value.update(extra)
        return value

    def post(self, body):
        return self.client.post("/api/external-mcp/servers", json=body)

    def test_authenticated_malformed_json_has_zero_mutation(self):
        response = self.client.post(
            "/api/external-mcp/servers",
            data="{",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.server_registry.list(), ())

    def test_unknown_top_level_fields_fail_closed(self):
        response = self.post(self.payload(transport="streamable_http"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.server_registry.list(), ())

    def test_unknown_auth_fields_fail_closed(self):
        response = self.post({
            "display_name": "Example MCP",
            "endpoint": "https://example.com/mcp",
            "auth": {"scheme": "none", "credential_slot": "toolroom"},
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.server_registry.list(), ())

    def test_none_uses_real_owners_and_current_revision_two(self):
        response = self.post(self.payload())
        body = response.get_json()
        server = self.graph.server_registry.get("srv-test")
        binding = self.graph.auth_binding_registry.get_binding("srv-test")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(server.lifecycle_state, "REVIEW_REQUIRED")
        self.assertEqual(server.master_state, "OFF")
        self.assertEqual(server.revision, 2)
        self.assertEqual(binding.auth_scheme, "none")
        self.assertIsNone(binding.secret_ref)
        self.assertEqual(self.graph.connection.execute(
            "SELECT count(*) FROM external_secret_records"
        ).fetchone()[0], 0)
        self.assertEqual(body["server"]["revision"], 2)
        self.assertEqual(body["server"]["lifecycle_state"], "REVIEW_REQUIRED")

    def test_bearer_secret_store_binding_and_response_hygiene(self):
        credential = "bearer-test-credential"
        response = self.post(self.payload(scheme="bearer", credential=credential))
        body = response.get_json()
        self.graph.connection.commit()
        dump = self.graph.db_path.read_bytes()
        encoded = json.dumps(body, ensure_ascii=False)
        binding = self.graph.auth_binding_registry.get_binding("srv-test")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(binding.auth_scheme, "bearer")
        self.assertEqual(binding.credential_slot, "toolroom")
        self.assertEqual(self.graph.connection.execute(
            "SELECT lifecycle_state FROM external_secret_records"
        ).fetchone()[0], "ACTIVE")
        self.assertNotIn(credential.encode(), dump)
        self.assertNotIn(credential, encoded)
        self.assertNotIn(binding.secret_ref, encoded)
        self.assertNotIn("secret_ref", body)

    def test_full_success_ingests_once_with_safe_candidate_state(self):
        self.graph.runtime.tools = [{"name": "echo", "description": "safe"}]
        actual = self.graph.candidate_registry
        counter = _CountingCandidateRegistry(actual)
        self.graph.candidate_registry = counter
        response = self.post(self.payload())
        candidate = actual.get_candidate("srv-test", "echo")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.graph.runtime.discover_calls), 1)
        self.assertEqual(counter.calls, 1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["presence_state"], "PRESENT")
        self.assertEqual(candidate["review_state"], "REVIEW_REQUIRED")
        self.assertEqual(candidate["model_visible"], 0)
        self.assertEqual(candidate["execution_allowed"], 0)
        self.assertEqual(self.graph.runtime.invoke_calls, 0)
        self.assertEqual(self.graph.connection.execute(
            "SELECT count(*) FROM external_tool_approval_baselines"
        ).fetchone()[0], 0)
        self.assertEqual(self.graph.connection.execute(
            "SELECT count(*) FROM external_tool_review_audit"
        ).fetchone()[0], 0)

    def test_zero_tool_complete_success_is_201(self):
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["discovery"]["status"], "SUCCESS")
        self.assertEqual(body["discovery"]["tool_count"], 0)

    def test_discovery_failure_is_partial_and_uses_current_server(self):
        self.graph.runtime.status = "BRIDGE_ERROR"
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertEqual(len(self.graph.runtime.discover_calls), 1)
        self.assertEqual(self.graph.candidate_registry.list_candidates("srv-test"), ())
        self.assertEqual(body["server"]["lifecycle_state"], "REVIEW_REQUIRED")
        self.assertEqual(body["server"]["revision"], 2)
        self.assertEqual(body["discovery"]["reason_code"], "BRIDGE_ERROR")

    def test_success_without_complete_catalog_is_safe_failure(self):
        self.graph.runtime.catalog_complete = False
        self.graph.runtime.tools = [{"name": "echo"}]
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertEqual(body["discovery"], {
            "status": "FAILED",
            "reason_code": "INCOMPLETE_DISCOVERY",
            "tool_count": 0,
        })
        self.assertEqual(self.graph.candidate_registry.list_candidates("srv-test"), ())

    def test_candidate_ingest_failure_is_partial_without_retry(self):
        actual = self.graph.candidate_registry
        self.graph.candidate_registry = _FailingIngest(actual)
        self.graph.runtime.tools = [{"name": "echo"}]
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertEqual(self.graph.candidate_registry.calls, 1)
        self.assertEqual(body["server"]["revision"], 2)
        self.assertEqual(body["discovery"]["reason_code"], "CANDIDATE_INGEST_FAILED")

    def test_unsafe_discovery_code_falls_back_without_remote_text(self):
        self.graph.runtime.status = "REMOTE_FAILURE"
        self.graph.runtime.error = {
            "code": "bad-code\n",
            "summary": "remote secret and diagnostics",
        }
        response = self.post(self.payload())
        body = response.get_json()
        encoded = json.dumps(body, ensure_ascii=False)
        self.assertEqual(response.status_code, 207)
        self.assertEqual(body["discovery"]["reason_code"], "DISCOVERY_FAILED")
        self.assertNotIn("bad-code", encoded)
        self.assertNotIn("remote secret", encoded)

    def test_duplicate_endpoint_is_409_before_second_secret_or_discovery(self):
        first = self.post(self.payload(scheme="bearer", credential="first-secret"))
        self.assertEqual(first.status_code, 201)
        second = self.post(self.payload(scheme="bearer", credential="second-secret"))
        self.assertEqual(second.status_code, 409)
        self.assertEqual(len(self.graph.runtime.discover_calls), 1)
        self.assertEqual(self.graph.connection.execute(
            "SELECT count(*) FROM external_secret_records"
        ).fetchone()[0], 1)

    def test_auth_configuration_failure_is_truthful_partial_without_discovery(self):
        self.graph.auth_binding_registry = _FailingBinding()
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 207)
        self.assertFalse(body["auth_configured"])
        self.assertEqual(body["discovery"]["status"], "NOT_ATTEMPTED")
        self.assertEqual(self.graph.runtime.discover_calls, [])
        self.assertEqual(body["server"]["lifecycle_state"], "REGISTERED")
        self.assertEqual(body["server"]["revision"], 1)

    def test_owner_auth_denied_before_body_or_graph(self):
        graph_opened = []
        app = Flask(__name__)
        app.register_blueprint(
            routes.create_external_mcp_admin_blueprint(
                graph_opener=lambda: graph_opened.append(True)
            )
        )
        client = app.test_client()
        routes.require_owner = lambda request: (_ for _ in ()).throw(
            OwnerAuthError("unauthorized", 401)
        )
        response = client.post("/api/external-mcp/servers", data="{")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(graph_opened, [])


if __name__ == "__main__":
    unittest.main()
