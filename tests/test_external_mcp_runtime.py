from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from cryptography.fernet import Fernet

from tools.external_mcp_auth_binding import AUTH_BEARER, AUTH_NONE, ExternalMcpAuthBindingRegistry
from tools.external_mcp_invocation import FAILED_PRE_CALL, OUTCOME_UNKNOWN, SUCCEEDED, ExternalMcpInvocation
from tools.external_mcp_runtime import ExternalMcpRuntime, ExternalMcpRuntimeInitializationError
from tools.external_mcp_secret_materializer import ExternalMcpSecretMaterializer, ExternalMcpSecretMaterializerError
from tools.external_secret_store import ExternalSecretStore
from tools.external_server_registry import ExternalServerRegistry
from tools.external_tool_registry import ExternalToolCandidateRegistry


CANARY = "B2_TEST_BEARER_CANARY"


def tool(name="calendar.list"):
    return {"name": name, "description": "Calendar", "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}}


class MutatingMaterializer(ExternalMcpSecretMaterializer):
    def __init__(self, secret_store, *, key_file, mutation):
        super().__init__(secret_store, key_file=key_file)
        self._mutation = mutation

    def materialize(self, auth=None, **kwargs):
        result = super().materialize(auth, **kwargs)
        self._mutation()
        return result


class FailingMaterializer(ExternalMcpSecretMaterializer):
    def materialize(self, auth=None, **kwargs):
        raise ExternalMcpSecretMaterializerError("secret unavailable", code="AUTH_SECRET_UNAVAILABLE")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.servers = ExternalServerRegistry(self.connection, id_factory=lambda: "server-1")
        self.server = self.servers.register(display_name="Calendar", endpoint="https://calendar.example/mcp", provenance="owner-admin")
        self.tmp = tempfile.TemporaryDirectory()
        self.key = Path(self.tmp.name) / "key"
        self.key.write_bytes(Fernet.generate_key())
        if os.name == "posix":
            os.chmod(self.key, 0o600)
        self.secrets = ExternalSecretStore(self.connection, key_file=str(self.key), registry=self.servers)
        self.auth = ExternalMcpAuthBindingRegistry(self.connection, server_registry=self.servers, secret_store=self.secrets)
        self.auth.set_binding(self.server.server_id, AUTH_NONE)
        self.candidates = ExternalToolCandidateRegistry(self.connection, server_registry=self.servers)
        self.invocation = ExternalMcpInvocation(
            self.connection,
            server_registry=self.servers,
            candidate_registry=self.candidates,
            auth_binding_registry=self.auth,
            id_factory=iter(f"attempt-{number}" for number in range(1, 1000)).__next__,
        )
        self.materializer = ExternalMcpSecretMaterializer(self.secrets, key_file=str(self.key))
        self.bridge = Path(self.tmp.name) / "call-bridge.mjs"
        self.discovery_bridge = Path(self.tmp.name) / "discovery-bridge.mjs"

    def tearDown(self):
        self.connection.close()
        self.tmp.cleanup()

    def _runtime(self, call_output=None, discovery_output=None, *, node=None, materializer=None, mock_spawn=True):
        if mock_spawn:
            self.bridge.write_text("", encoding="utf-8")
        self.discovery_bridge.write_text("", encoding="utf-8")
        runtime = ExternalMcpRuntime(
            self.invocation,
            server_registry=self.servers,
            auth_binding_registry=self.auth,
            secret_store=self.secrets,
            materializer=materializer or self.materializer,
            node_executable=node or "node",
            call_bridge_path=self.bridge,
            discovery_bridge_path=self.discovery_bridge,
        )
        if mock_spawn:
            output = call_output if call_output is not None else discovery_output
            runtime._spawn_json = Mock(return_value=output or {"status": "SUCCESS", "result": {"ok": True}, "error": None, "diagnostics": {}})
        return runtime

    def _prepared_candidate(self):
        current = self.servers.get(self.server.server_id)
        current = self.servers.mark_connected(self.server.server_id, current.revision)
        discovered = {
            "status": "SUCCESS",
            "server_id": self.server.server_id,
            "display_name": current.display_name,
            "registration_provenance": current.registration_provenance,
            "registry_revision": current.revision,
            "lifecycle_state": current.lifecycle_state,
            "transport": current.transport,
            "endpoint_snapshot": current.endpoint,
            "catalog_complete": True,
            "tool_record_boundary": "SDK_VISIBLE_RAW",
            "tools": [tool()],
            "diagnostics": {"registry_changed_during_attempt": False},
        }
        self.candidates.ingest(discovered)
        candidate = self.candidates.get_candidate(self.server.server_id, "calendar.list")
        return None

    def _bearer(self):
        secret = self.secrets.create(server_id=self.server.server_id, credential_slot="slot-a", secret=CANARY)
        binding = self.auth.set_binding(self.server.server_id, AUTH_BEARER, secret_ref=secret.secret_ref)
        return secret, binding

    def _discovery_output(self, tools=None):
        return {"status": "SUCCESS", "catalog_complete": True, "zero_tools": not tools, "tools": tools or [], "diagnostics": {}, "error": None}

    def _real_child_bridge(self, capture: Path):
        self.bridge.write_text(
            "import json, sys\n"
            f"capture = {str(capture)!r}\n"
            "value = json.load(sys.stdin)\n"
            "with open(capture, 'w', encoding='utf-8') as stream:\n"
            "    json.dump({'argv': sys.argv, 'env': dict(__import__('os').environ), 'stdin': value}, stream, ensure_ascii=False, sort_keys=True)\n"
            "print(json.dumps({'status': 'SUCCESS', 'result': {'ok': True}, 'error': None, 'diagnostics': {'phase': 'CALL'}}))\n",
            encoding="utf-8",
        )

    def test_shared_authority_graph_and_none_call_has_zero_secret_access(self):
        self.secrets._load_runtime_record = Mock(side_effect=AssertionError("none auth accessed a secret"))
        runtime = self._runtime()
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], SUCCEEDED)
        self.assertEqual(result["mcp_result"], {"ok": True})
        runtime._spawn_json.assert_called_once()
        semantic = runtime._spawn_json.call_args.args[1]
        self.assertIsNone(semantic["auth"])
        self.assertEqual(set(semantic), {"bridge_version", "endpoint", "transport", "tool_name", "tool_input", "auth"})

    def test_bearer_uses_exact_ref_and_never_persists_plaintext(self):
        secret, binding = self._bearer()
        runtime = self._runtime()
        runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        semantic = runtime._spawn_json.call_args.args[1]
        self.assertEqual(semantic["auth"], {"scheme": "bearer", "credential": CANARY})
        self.assertNotIn(CANARY, "\n".join(self.connection.iterdump()))
        self.assertEqual(secret.secret_ref, binding.secret_ref)

    def test_call_auth_mutation_during_materialize_is_pre_call_and_zero_network(self):
        self._bearer()
        materializer = MutatingMaterializer(self.secrets, key_file=str(self.key), mutation=lambda: self.auth.set_binding(self.server.server_id, AUTH_NONE))
        runtime = self._runtime(materializer=materializer)
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        runtime._spawn_json.assert_not_called()

    def test_call_server_mutation_during_materialize_is_pre_call_and_zero_network(self):
        self._bearer()
        materializer = MutatingMaterializer(self.secrets, key_file=str(self.key), mutation=lambda: self.servers.update_connection(self.server.server_id, endpoint="https://changed.example/mcp"))
        runtime = self._runtime(materializer=materializer)
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        runtime._spawn_json.assert_not_called()

    def test_materialization_failure_is_pre_call_and_zero_network(self):
        self._bearer()
        runtime = self._runtime(materializer=FailingMaterializer(self.secrets, key_file=str(self.key)))
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        runtime._spawn_json.assert_not_called()

    def test_stale_authority_and_oversize_never_spawn(self):
        runtime = self._runtime()
        envelope = {
            "server_id": self.server.server_id,
            "endpoint": self.server.endpoint,
            "transport": self.server.transport,
            "source_registry_revision": self.server.revision,
            "auth_binding_revision": self.auth.get_binding(self.server.server_id).revision,
            "auth_scheme": AUTH_NONE,
            "secret_ref": None,
            "credential_slot": None,
            "tool_name": "calendar.list",
            "tool_input": {"q": "today"},
        }
        self.servers.update_connection(self.server.server_id, endpoint="https://changed.example/mcp")
        result = runtime._run_call_envelope(envelope)
        self.assertEqual(result["status"], "NOT_INVOKED")
        runtime._spawn_json.assert_not_called()

        fresh = self._runtime()
        result = fresh.invoke(f"ext:{self.server.server_id}:calendar.list", {"data": "x" * (256 * 1024)}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        fresh._spawn_json.assert_not_called()

    def test_runtime_preserves_safe_not_invoked_reason(self):
        self._prepared_candidate()
        summary = "RUNTIME_FAKE_SECRET_LIKE_SUMMARY_MUST_NOT_PERSIST"
        runtime = self._runtime(
            call_output={
                "status": "NOT_INVOKED",
                "result": None,
                "error": {
                    "code": "AUTH_REQUIRED",
                    "summary": summary,
                },
                "diagnostics": {
                    "phase": "PRE_CALL",
                    "request_count": 1,
                    "call_started": False,
                },
            }
        )
        result = runtime.invoke(
            f"ext:{self.server.server_id}:calendar.list",
            {"q": "today"},
            None,
            expected_turn_id="turn-1",
        )
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        self.assertEqual(result["reason_code"], "AUTH_REQUIRED")
        attempt = self.invocation.get_attempt(result["attempt_id"])
        self.assertEqual(attempt["status"], FAILED_PRE_CALL)
        self.assertEqual(attempt["reason_code"], "AUTH_REQUIRED")
        self.assertEqual(self.invocation.list_audit(result["attempt_id"])[-1]["reason_code"], "AUTH_REQUIRED")
        self.assertNotIn(summary, "\n".join(self.connection.iterdump()))

    def test_post_spawn_failure_is_unknown_and_no_retry(self):
        runtime = self._runtime()
        runtime._spawn_json.side_effect = RuntimeError("child failed")
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], OUTCOME_UNKNOWN)
        self.assertIsNone(result.get("mcp_result"))
        self.assertEqual(runtime._spawn_json.call_count, 1)

    def test_unknown_outcome_suppresses_ephemeral_result(self):
        runtime = self._runtime({"status": "OUTCOME_UNKNOWN", "result": {"leak": "no"}, "error": {"code": "UNKNOWN"}, "diagnostics": {}})
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], OUTCOME_UNKNOWN)
        self.assertIsNone(result.get("mcp_result"))

    def test_post_persistence_unknown_suppresses_ephemeral_result(self):
        self.connection.executescript("CREATE TRIGGER fail_terminal BEFORE UPDATE OF status ON external_tool_invocation_attempts WHEN NEW.status IN ('SUCCEEDED','TOOL_ERROR','FAILED_PRE_CALL','OUTCOME_UNKNOWN') BEGIN SELECT RAISE(ABORT, 'fail terminal persistence'); END;")
        runtime = self._runtime()
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], OUTCOME_UNKNOWN)
        self.assertIsNone(result.get("mcp_result"))

    def test_duplicate_does_not_replay_ephemeral_result(self):
        runtime = self._runtime()
        lease = self._prepared_candidate()
        first = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, lease, expected_turn_id="turn-1")
        second = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, lease, expected_turn_id="turn-1")
        self.assertEqual(first["mcp_result"], {"ok": True})
        self.assertIsNone(second.get("mcp_result"))
        self.assertEqual(runtime._spawn_json.call_count, 1)

    def test_discovery_none_has_no_secret_materialization(self):
        self.secrets._load_runtime_record = Mock(side_effect=AssertionError("none discovery accessed a secret"))
        runtime = self._runtime(discovery_output=self._discovery_output())
        result = runtime.discover(self.server.server_id)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["tools"], [])
        self.assertIsNone(runtime._spawn_json.call_args.args[1]["auth"])

    def test_discovery_bearer_uses_exact_ref(self):
        secret, binding = self._bearer()
        runtime = self._runtime(discovery_output=self._discovery_output())
        result = runtime.discover(self.server.server_id)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(runtime._spawn_json.call_args.args[1]["auth"], {"scheme": "bearer", "credential": CANARY})
        self.assertEqual(secret.secret_ref, binding.secret_ref)
        self.assertNotIn(CANARY, "\n".join(self.connection.iterdump()))

    def test_discovery_missing_binding_is_not_invoked(self):
        self.connection.execute("DELETE FROM external_mcp_auth_bindings WHERE server_id=?", (self.server.server_id,))
        self.connection.commit()
        runtime = self._runtime(discovery_output=self._discovery_output())
        result = runtime.discover(self.server.server_id)
        self.assertEqual(result["status"], "NOT_INVOKED")
        runtime._spawn_json.assert_not_called()

    def test_discovery_auth_mutation_during_materialize_is_not_invoked(self):
        self._bearer()
        materializer = MutatingMaterializer(self.secrets, key_file=str(self.key), mutation=lambda: self.auth.set_binding(self.server.server_id, AUTH_NONE))
        runtime = self._runtime(discovery_output=self._discovery_output(), materializer=materializer)
        result = runtime.discover(self.server.server_id)
        self.assertNotEqual(result["status"], "SUCCESS")
        self.assertEqual(result["tools"], [])
        runtime._spawn_json.assert_not_called()

    def test_discovery_stale_snapshot_is_not_invoked(self):
        self._bearer()
        runtime = self._runtime(discovery_output=self._discovery_output())
        snapshot = self.servers.get(self.server.server_id)
        binding = self.auth.get_binding(self.server.server_id)
        self.auth.set_binding(self.server.server_id, AUTH_NONE)
        result = runtime._run_discovery_snapshot(snapshot)
        self.assertEqual(result["status"], "NOT_INVOKED")
        runtime._spawn_json.assert_not_called()
        self.assertIsNotNone(binding)

    def test_discovery_reflection_is_unknown_and_suppressed(self):
        self._bearer()
        runtime = self._runtime(discovery_output=self._discovery_output([{"name": CANARY}]))
        result = runtime.discover(self.server.server_id)
        self.assertNotEqual(result["status"], "SUCCESS")
        self.assertEqual(result["tools"], [])
        self.assertNotIn(CANARY, json.dumps(result))

    def test_real_child_boundary_keeps_bearer_only_in_stdin(self):
        self._bearer()
        capture = Path(self.tmp.name) / "capture.json"
        self._real_child_bridge(capture)
        runtime = self._runtime(node=sys.executable, mock_spawn=False)
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", {"q": "today"}, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], SUCCEEDED)
        captured = json.loads(capture.read_text(encoding="utf-8"))
        self.assertNotIn(CANARY, json.dumps(captured["argv"]))
        self.assertNotIn(CANARY, json.dumps(captured["env"]))
        self.assertEqual(captured["stdin"]["auth"], {"scheme": "bearer", "credential": CANARY})
        self.assertNotIn(CANARY, json.dumps(result.get("mcp_diagnostics")))

    def test_real_near_256k_canonical_input_spawns_one_child(self):
        capture = Path(self.tmp.name) / "near-capture.json"
        self._real_child_bridge(capture)
        value = "x" * (256 * 1024 - 100)
        tool_input = {"data": value}
        canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertLessEqual(len(canonical), 256 * 1024)
        self.assertGreater(len(canonical), 250 * 1024)
        runtime = self._runtime(node=sys.executable, mock_spawn=False)
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", tool_input, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], SUCCEEDED)
        captured = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(captured["stdin"]["tool_input"], tool_input)
        print(f"B2_BOUNDARY NEAR_TOOL_INPUT_BYTES={len(canonical)} CHILD_COUNT=1")

    def test_real_oversize_canonical_input_spawns_zero_children(self):
        capture = Path(self.tmp.name) / "oversize-capture.json"
        self._real_child_bridge(capture)
        value = "x" * (256 * 1024)
        tool_input = {"data": value}
        canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertGreater(len(canonical), 256 * 1024)
        runtime = self._runtime(node=sys.executable, mock_spawn=False)
        result = runtime.invoke(f"ext:{self.server.server_id}:calendar.list", tool_input, self._prepared_candidate(), expected_turn_id="turn-1")
        self.assertEqual(result["status"], FAILED_PRE_CALL)
        self.assertFalse(capture.exists())
        print(f"B2_BOUNDARY OVERSIZE_TOOL_INPUT_BYTES={len(canonical)} CHILD_COUNT=0")

    def test_runtime_rejects_split_authority_graph(self):
        other = sqlite3.connect(":memory:")
        try:
            other_servers = ExternalServerRegistry(other)
            with self.assertRaises(ExternalMcpRuntimeInitializationError):
                ExternalMcpRuntime(self.invocation, server_registry=other_servers, auth_binding_registry=self.auth, secret_store=self.secrets, materializer=self.materializer)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
