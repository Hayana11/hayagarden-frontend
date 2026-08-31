import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask

with patch.dict(os.environ, {"MOMENTS_OWNER_TOKEN": "synthetic-admin-test-owner"}):
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
        if self.catalog_complete:
            server = self.graph.server_registry.mark_connected(server_id, server.revision)
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
                graph_factory=lambda: _GraphContext(self.graph)
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
        for field in ("transport", "provenance", "secret_ref", "credential_slot", "auth_scheme", "unknown"):
            with self.subTest(field=field):
                with patch.object(self.graph.server_registry, "register", wraps=self.graph.server_registry.register) as register:
                    response = self.post(self.payload(**{field: "forbidden"}))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.graph.server_registry.list(), ())
                register.assert_not_called()
        self.assertEqual(self.graph.runtime.discover_calls, [])

    def test_unknown_auth_fields_fail_closed(self):
        for scheme in ("none", "bearer"):
            for field in ("transport", "provenance", "secret_ref", "credential_slot", "auth_scheme", "unknown"):
                with self.subTest(scheme=scheme, field=field):
                    payload = self.payload(scheme=scheme, credential="synthetic-credential")
                    payload["auth"][field] = "forbidden"
                    response = self.post(payload)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.graph.server_registry.list(), ())
        self.assertEqual(self.graph.runtime.discover_calls, [])

    def test_none_uses_real_owners_and_current_revision_two(self):
        response = self.post(self.payload())
        body = response.get_json()
        server = self.graph.server_registry.get("srv-test")
        binding = self.graph.auth_binding_registry.get_binding("srv-test")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server.lifecycle_state, "CONNECTED")
        self.assertEqual(server.revision, 2)
        self.assertEqual(binding.auth_scheme, "none")
        self.assertIsNone(binding.secret_ref)
        self.assertEqual(self.graph.connection.execute(
            "SELECT count(*) FROM external_secret_records"
        ).fetchone()[0], 0)
        self.assertEqual(server.transport, "streamable_http")
        self.assertEqual(server.registration_provenance, "toolroom-owner")
        self.assertEqual(body["status"], "CONNECTED")

    def test_bearer_secret_store_binding_and_response_hygiene(self):
        credential = "bearer-test-credential"
        response = self.post(self.payload(scheme="bearer", credential=credential))
        body = response.get_json()
        self.graph.connection.commit()
        dump = self.graph.db_path.read_bytes()
        encoded = json.dumps(body, ensure_ascii=False)
        binding = self.graph.auth_binding_registry.get_binding("srv-test")
        self.assertEqual(response.status_code, 200)
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
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.graph.runtime.discover_calls), 1)
        self.assertEqual(counter.calls, 1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["presence_state"], "PRESENT")
        self.assertEqual(self.graph.runtime.invoke_calls, 0)
        self.assertEqual(response.get_json()["status"], "CONNECTED")
        self.assertEqual(response.get_json()["tool_count"], 1)
        self.assertEqual(candidate["current_source_registry_revision"], 2)

    def test_zero_tool_complete_success_is_connected(self):
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["discovery"]["status"], "SUCCESS")
        self.assertEqual(body["discovery"]["tool_count"], 0)

    def test_discovery_failure_is_partial_and_uses_current_server(self):
        self.graph.runtime.status = "BRIDGE_ERROR"
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.graph.runtime.discover_calls), 1)
        self.assertEqual(self.graph.candidate_registry.list_candidates("srv-test"), ())
        self.assertEqual(body["status"], "DISCONNECTED")
        self.assertEqual(self.graph.server_registry.get("srv-test").revision, 2)
        self.assertEqual(body["discovery"]["reason_code"], "BRIDGE_ERROR")

    def test_success_without_complete_catalog_is_safe_failure(self):
        self.graph.runtime.catalog_complete = False
        self.graph.runtime.tools = [{"name": "echo"}]
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["discovery"], {
            "status": "DISCONNECTED",
            "reason_code": "INCOMPLETE_DISCOVERY",
        })
        self.assertEqual(self.graph.candidate_registry.list_candidates("srv-test"), ())

    def test_candidate_ingest_failure_is_partial_without_retry(self):
        actual = self.graph.candidate_registry
        self.graph.candidate_registry = _FailingIngest(actual)
        self.graph.runtime.tools = [{"name": "echo"}]
        response = self.post(self.payload())
        body = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.graph.candidate_registry.calls, 1)
        self.assertEqual(self.graph.server_registry.get("srv-test").revision, 2)
        self.assertEqual(body["status"], "DISCONNECTED")
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
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["discovery"]["reason_code"], "DISCOVERY_FAILED")
        self.assertNotIn("bad-code", encoded)
        self.assertNotIn("remote secret", encoded)

    def test_duplicate_endpoint_is_409_before_second_secret_or_discovery(self):
        first = self.post(self.payload(scheme="bearer", credential="first-secret"))
        self.assertEqual(first.status_code, 200)
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
        self.assertEqual(body["status"], "DISCONNECTED")
        self.assertEqual(self.graph.server_registry.get("srv-test").revision, 1)

    def test_owner_auth_denied_before_body_or_graph(self):
        graph_opened = []
        app = Flask(__name__)
        app.register_blueprint(
            routes.create_external_mcp_admin_blueprint(
                graph_factory=lambda: graph_opened.append(True)
            )
        )
        client = app.test_client()
        routes.require_owner = lambda request: (_ for _ in ()).throw(
            OwnerAuthError("unauthorized", 401)
        )
        with patch("flask.Request.get_json", side_effect=AssertionError("body parsed before auth")):
            response = client.post("/api/external-mcp/servers", data="{")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(graph_opened, [])

    def test_required_keys_and_auth_shape_rejected_before_opening_graph(self):
        malformed = [None, [], {}, self.payload(auth=None), self.payload(auth={}),
                     self.payload(auth={"scheme": "none", "credential": "unused"}),
                     self.payload(auth={"scheme": "bearer"}),
                     self.payload(auth={"scheme": []}),
                     self.payload(auth={"scheme": {}}),
                     self.payload(display_name=False), self.payload(endpoint=23)]
        malformed.extend(self.payload(auth={"scheme": "bearer", "credential": value})
                         for value in (None, "", True, 12, [], {}))
        malformed.extend({k: v for k, v in self.payload().items() if k != missing}
                         for missing in ("display_name", "endpoint", "auth"))
        for payload in malformed:
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.graph.server_registry.list(), ())
        self.assertEqual(self.graph.runtime.discover_calls, [])
        self.assertEqual(self.graph.connection.execute("SELECT count(*) FROM external_secret_records").fetchone()[0], 0)

    def test_safe_discovery_code_exact_length_and_charset_gate(self):
        self.post(self.payload())
        self.graph.runtime.status = "REMOTE_FAILURE"
        for code in ("A", "A" * 65, "unsafe_code", "BAD-CODE", "AB\n", " AB", 123, None, {}, ["SAFE"], "ÅA"):
            with self.subTest(code=code):
                self.graph.runtime.error = {"code": code, "summary": "remote-diagnostic-canary"}
                response = self.client.post("/api/external-mcp/servers/srv-test/check")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason_code"], "DISCOVERY_FAILED")
                self.assertNotIn("remote-diagnostic-canary", response.get_data(as_text=True))
        for code in ("AB", "A" * 64, "REMOTE_ERROR_429"):
            with self.subTest(code=code):
                self.graph.runtime.error = {"code": code}
                response = self.client.post("/api/external-mcp/servers/srv-test/check")
                self.assertEqual(response.get_json()["reason_code"], code)

    def test_discovery_exception_and_non_mapping_never_reflect_or_remain_connected(self):
        self.post(self.payload())
        for value in (None, [], "remote-body-canary"):
            with self.subTest(value=value), patch.object(self.graph.runtime, "discover", return_value=value):
                response = self.client.post("/api/external-mcp/servers/srv-test/check")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["status"], "DISCONNECTED")
                self.assertEqual(response.get_json()["reason_code"], "DISCOVERY_FAILED")
                self.assertNotIn("remote-body-canary", response.get_data(as_text=True))
        with patch.object(self.graph.runtime, "discover", side_effect=RuntimeError("exception-secret-canary")):
            response = self.client.post("/api/external-mcp/servers/srv-test/check")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("exception-secret-canary", response.get_data(as_text=True))

    def test_get_and_check_are_owner_only_with_bearer_challenge(self):
        routes.require_owner = lambda request: (_ for _ in ()).throw(OwnerAuthError("unauthorized", 401))
        with patch.object(self.graph.runtime, "discover", side_effect=AssertionError("unauthenticated discovery")):
            for method, url in (("get", "/api/external-mcp/servers"), ("post", "/api/external-mcp/servers/unknown/check")):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
        self.assertEqual(self.graph.server_registry.list(), ())

    def test_list_is_metadata_only_and_reports_present_tools(self):
        self.graph.runtime.tools = [{"name": "echo"}]
        self.post(self.payload(scheme="bearer", credential="synthetic-list-secret"))
        response = self.client.get("/api/external-mcp/servers")
        body = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["servers"][0]["lifecycle_state"], "CONNECTED")
        self.assertEqual(body["servers"][0]["present_tool_count"], 1)
        self.assertNotIn("synthetic-list-secret", response.get_data(as_text=True))
        self.assertNotIn("secret_ref", response.get_data(as_text=True))


class ExternalMcpAppImportSmokeTests(unittest.TestCase):
    def test_real_app_import_registers_admin_blueprint_exactly_once(self):
        # Execute the real app and Flask registration in a clean process. Only
        # legacy filesystem/SQLite boundaries are redirected to synthetic data;
        # no application module, blueprint, route or registration is replaced.
        script = r'''
import builtins
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

code_root = Path.cwd().resolve()
os.environ.clear()
os.environ.update({"MOMENTS_OWNER_TOKEN": "synthetic-import-owner", "PYTHONDONTWRITEBYTECODE": "1"})
with tempfile.TemporaryDirectory(prefix="external-mcp-app-smoke-") as directory:
    fixture = Path(directory).resolve()
    original_open = builtins.open
    original_connect = sqlite3.connect
    original_makedirs = os.makedirs
    opened_databases = set()
    network_attempts = []

    def beneath(path, root):
        return path == root or root in path.parents

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.bind", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.posix_spawn"}:
            network_attempts.append(event)
            raise AssertionError("app smoke forbids network/process activity")
        if event == "open" and not isinstance(args[0], int):
            path = Path(os.fsdecode(args[0])).resolve()
            if any(beneath(path, Path(root)) for root in ("/opt", "/etc/hayagarden", "/var/lib/hayagarden", "/root")) and not beneath(path, code_root):
                raise AssertionError("app smoke forbids production file reads")
            mode = args[1] or ""
            flags = args[2] or 0
            writing = any(c in mode for c in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not beneath(path, fixture):
                raise AssertionError("app smoke forbids non-fixture writes")
        if event == "sqlite3.connect":
            target = str(args[0])
            if target != ":memory:" and not beneath(Path(target).resolve(), fixture):
                raise AssertionError("app smoke forbids production database access")
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"}:
            path = Path(os.fsdecode(args[0])).absolute()
            dir_fd = args[-1]
            if isinstance(dir_fd, int) and dir_fd >= 0 and not Path(os.fsdecode(args[0])).is_absolute():
                path = Path(os.readlink("/proc/self/fd/" + str(dir_fd))) / os.fsdecode(args[0])
            if not beneath(path, fixture):
                raise AssertionError("app smoke forbids non-fixture mutation")
            if event == "os.rename":
                destination = Path(os.fsdecode(args[1])).resolve()
                if not beneath(destination, fixture):
                    raise AssertionError("app smoke forbids non-fixture rename")

    def fixture_makedirs(name, mode=0o777, exist_ok=False):
        path = Path(name).absolute()
        if not beneath(path, fixture):
            path = fixture / "legacy-directories" / path.name
        return original_makedirs(path, mode=mode, exist_ok=exist_ok)

    def fixture_open(file, mode="r", *args, **kwargs):
        if not isinstance(file, int) and os.fspath(file) == "/opt/frontend/.env":
            if mode not in {"r", "rt"}:
                raise AssertionError("fixture environment is read only")
            return io.StringIO("")
        return original_open(file, mode, *args, **kwargs)

    def fixture_connect(database, *args, **kwargs):
        # Never pass a caller's path to sqlite. Distinct logical databases stay
        # distinct, entirely under the fixture; ATTACH is denied below as well.
        identity = hashlib.sha256(os.fsencode(database)).hexdigest()
        target = fixture / (identity + ".db")
        connection = original_connect(str(target), *args, **kwargs)
        connection.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_ATTACH else sqlite3.SQLITE_OK)
        if str(target) not in opened_databases:
            connection.executescript("""
                CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, author TEXT, content TEXT, created_at TEXT, source_kind TEXT DEFAULT 'chat');
                CREATE TABLE posts (id INTEGER PRIMARY KEY, type TEXT, content TEXT, author TEXT, created_at TEXT);
            """)
            opened_databases.add(str(target))
        return connection

    sys.addaudithook(audit)
    builtins.open = fixture_open
    sqlite3.connect = fixture_connect
    os.makedirs = fixture_makedirs
    import app
    assert Path(app.__file__).resolve() == code_root / "app.py"
    assert list(app.app.blueprints).count("external_mcp_admin") == 1
    rules = [rule for rule in app.app.url_map.iter_rules() if rule.endpoint.startswith("external_mcp_admin.")]
    assert len(rules) == 3
    assert opened_databases
    assert not network_attempts
    print(json.dumps({"imported": True, "blueprint_count": 1, "admin_rules": len(rules), "production_database_opened": False, "network_attempted": False}))
'''
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR") if key in os.environ}
        environment["PYTHONPATH"] = os.pathsep.join(sys.path)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(evidence, {"imported": True, "blueprint_count": 1, "admin_rules": 3,
                                    "production_database_opened": False, "network_attempted": False})


if __name__ == "__main__":
    unittest.main()
