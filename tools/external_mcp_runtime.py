"""Composition owner for authenticated external MCP calls and discovery.

The durable invocation and administrative discovery modules remain the owners
of their respective state machines.  This module only composes their frozen
authority snapshots with the already reviewed secret materializer and the
one-shot Node bridges.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .external_mcp_auth_binding import ExternalMcpAuthBindingRegistry
from .external_mcp_discovery import ExternalMcpDiscovery
from .external_mcp_invocation import (
    OUTCOME_UNKNOWN,
    SUCCEEDED,
    TOOL_ERROR,
    ExternalMcpInvocation,
    MAX_TOOL_INPUT_BYTES,
)
from .external_mcp_secret_materializer import (
    MAX_MATERIALIZED_CREDENTIAL_BYTES,
    ExternalMcpSecretMaterializer,
    ExternalMcpSecretMaterializerError,
)
from .external_secret_store import ExternalSecretStore
from .external_server_registry import ExternalServerRecord, ExternalServerRegistry, REVOKED_STATE

BRIDGE_VERSION = 1
NOT_INVOKED = "NOT_INVOKED"
SUCCESS = "SUCCESS"
CALL_TOOL_ERROR = "TOOL_ERROR"
SECRET_REFLECTION_BLOCKED = "SECRET_REFLECTION_BLOCKED"
CALL_BRIDGE_INPUT_LIMIT = 320 * 1024
CALL_BRIDGE_OUTPUT_LIMIT = 256 * 1024
CALL_BRIDGE_STDERR_LIMIT = 4 * 1024
CHILD_TIMEOUT_SECONDS = 17.0


class ExternalMcpRuntimeInitializationError(ValueError):
    code = "SERVER_AUTHORITY_MISMATCH"


class _PreSpawnFailure(Exception):
    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code
        self.summary = summary


class _PostSpawnFailure(Exception):
    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code
        self.summary = summary


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    return value


def _contains(value: Any, needle: str, seen: Optional[set[int]] = None) -> bool:
    if not needle or value is None:
        return False
    if isinstance(value, str):
        return needle in value
    if seen is None:
        seen = set()
    if isinstance(value, BaseException):
        return needle in str(value) or needle in repr(value)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        return any(_contains(key, needle, seen) or _contains(item, needle, seen) for key, item in value.items())
    if isinstance(value, (tuple, list, set)):
        marker = id(value)
        if marker in seen:
            return False
        seen.add(marker)
        return any(_contains(item, needle, seen) for item in value)
    return False


def _safe_error(code: str, summary: str) -> dict[str, str]:
    return {"code": code, "summary": summary}


class ExternalMcpRuntime:
    """Authenticated composition facade with no caller-controlled runner."""

    def __init__(
        self,
        invocation: ExternalMcpInvocation,
        *,
        server_registry: ExternalServerRegistry,
        auth_binding_registry: ExternalMcpAuthBindingRegistry,
        secret_store: ExternalSecretStore,
        materializer: ExternalMcpSecretMaterializer,
        node_executable: Optional[str] = None,
        call_bridge_path: Optional[Path] = None,
        discovery_bridge_path: Optional[Path] = None,
        timeout_seconds: float = CHILD_TIMEOUT_SECONDS,
    ) -> None:
        connection = getattr(invocation, "_connection", None)
        if (
            not isinstance(connection, sqlite3.Connection)
            or not isinstance(invocation, ExternalMcpInvocation)
            or not isinstance(server_registry, ExternalServerRegistry)
            or not isinstance(auth_binding_registry, ExternalMcpAuthBindingRegistry)
            or not isinstance(secret_store, ExternalSecretStore)
            or not isinstance(materializer, ExternalMcpSecretMaterializer)
            or getattr(server_registry, "_connection", None) is not connection
            or getattr(auth_binding_registry, "_connection", None) is not connection
            or getattr(secret_store, "_connection", None) is not connection
            or getattr(invocation, "_server_registry", None) is not server_registry
            or getattr(invocation, "_auth_binding_registry", None) is not auth_binding_registry
            or getattr(auth_binding_registry, "_server_registry", None) is not server_registry
            or getattr(secret_store, "_registry", None) is not server_registry
            or getattr(materializer, "_secret_store", None) is not secret_store
        ):
            raise ExternalMcpRuntimeInitializationError("runtime owners must share one authority graph")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._connection = connection
        self._invocation = invocation
        self._server_registry = server_registry
        self._auth_binding_registry = auth_binding_registry
        self._secret_store = secret_store
        self._materializer = materializer
        self._node_executable = node_executable or shutil.which("node") or "node"
        self._call_bridge_path = Path(call_bridge_path or Path(__file__).with_name("external_mcp_call_bridge.mjs"))
        self._discovery_bridge_path = Path(discovery_bridge_path or Path(__file__).with_name("external_mcp_discovery_bridge.mjs"))
        self._timeout_seconds = float(timeout_seconds)

    def invoke(
        self,
        control_id: Any,
        tool_input: Any,
        turn_lease: Any,
        *,
        expected_turn_id: Any,
    ) -> dict[str, Any]:
        ephemeral: dict[str, Any] = {}

        def runner(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
            result = self._run_call_envelope(envelope)
            ephemeral["result"] = result
            return result

        durable = self._invocation.invoke(
            control_id,
            tool_input,
            turn_lease,
            expected_turn_id=expected_turn_id,
            runner=runner,
        )
        response = dict(durable)
        if durable.get("status") in (SUCCEEDED, TOOL_ERROR):
            result = ephemeral.get("result")
            if isinstance(result, Mapping):
                response["mcp_result"] = result.get("result")
                response["mcp_error"] = result.get("error")
                response["mcp_diagnostics"] = result.get("diagnostics")
        else:
            response["mcp_result"] = None
            response["mcp_error"] = None
            response["mcp_diagnostics"] = None
        return response

    def discover(self, server_id: object) -> dict[str, object]:
        def runner(snapshot: ExternalServerRecord) -> Mapping[str, object]:
            return self._run_discovery_snapshot(snapshot)

        return ExternalMcpDiscovery(self._server_registry, runner=runner).discover(server_id)

    def _fresh_call_authority(self, envelope: Mapping[str, Any]) -> tuple[ExternalServerRecord, Any]:
        server_id = envelope.get("server_id")
        server = self._server_registry.get(server_id)
        if (
            server.server_id != server_id
            or server.revision != envelope.get("source_registry_revision")
            or server.lifecycle_state == REVOKED_STATE
            or server.endpoint != envelope.get("endpoint")
            or server.transport != envelope.get("transport")
        ):
            raise _PreSpawnFailure("SERVER_AUTHORITY_CHANGED", "server authority is stale")
        binding = self._auth_binding_registry.get_binding(server_id)
        if binding is None or any(
            (
                binding.revision != envelope.get("auth_binding_revision"),
                binding.auth_scheme != envelope.get("auth_scheme"),
                binding.secret_ref != envelope.get("secret_ref"),
                binding.credential_slot != envelope.get("credential_slot"),
            )
        ):
            raise _PreSpawnFailure("AUTH_BINDING_CHANGED", "auth binding is stale")
        return server, binding

    def _fresh_discovery_authority(self, snapshot: ExternalServerRecord, binding: Any) -> None:
        current = self._server_registry.get(snapshot.server_id)
        current_binding = self._auth_binding_registry.get_binding(snapshot.server_id)
        if (
            current.server_id != snapshot.server_id
            or current.revision != snapshot.revision
            or current.lifecycle_state != snapshot.lifecycle_state
            or current.endpoint != snapshot.endpoint
            or current.transport != snapshot.transport
            or current_binding is None
            or current_binding.revision != binding.revision
            or current_binding.auth_scheme != binding.auth_scheme
            or current_binding.secret_ref != binding.secret_ref
            or current_binding.credential_slot != binding.credential_slot
        ):
            raise _PreSpawnFailure("AUTHORITY_CHANGED", "external MCP authority is stale")

    def _auth_payload(self, server: ExternalServerRecord, binding: Any) -> Optional[dict[str, str]]:
        fields = {
            "server_id": server.server_id,
            "auth_scheme": binding.auth_scheme,
            "secret_ref": binding.secret_ref,
            "credential_slot": binding.credential_slot,
        }
        try:
            materialized = self._materializer.materialize(fields)
        except ExternalMcpSecretMaterializerError as exc:
            raise _PreSpawnFailure(exc.code, "external MCP credential is unavailable") from exc
        if materialized.scheme == "none":
            return None
        if materialized.scheme != "bearer" or not materialized.credential:
            raise _PreSpawnFailure("AUTH_SCHEME_UNSUPPORTED", "auth scheme is unsupported")
        return {"scheme": "bearer", "credential": materialized.credential}

    def _run_call_envelope(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        try:
            server, binding = self._fresh_call_authority(envelope)
            auth = self._auth_payload(server, binding)
            # Materialization is a TOCTOU boundary: the frozen server and
            # binding must still be current immediately before the child is
            # spawned.  Never re-materialize or fall back after this check.
            self._fresh_call_authority(envelope)
            semantic = {
                "bridge_version": BRIDGE_VERSION,
                "endpoint": server.endpoint,
                "transport": server.transport,
                "tool_name": envelope["tool_name"],
                "tool_input": _thaw(envelope["tool_input"]),
                "auth": auth,
            }
            result = self._spawn_json(self._call_bridge_path, semantic, credential=auth.get("credential") if auth else None)
            self._guard_reflection(result, auth)
            return self._normalize_call_result(result)
        except _PreSpawnFailure as exc:
            return {"status": NOT_INVOKED, "result": None, "error": _safe_error(exc.code, exc.summary), "diagnostics": {"phase": "PRE_SPAWN"}}
        except _PostSpawnFailure as exc:
            return {"status": OUTCOME_UNKNOWN, "result": None, "error": _safe_error(exc.code, exc.summary), "diagnostics": {"phase": "POST_SPAWN"}}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return {"status": NOT_INVOKED, "result": None, "error": _safe_error("CALL_INPUT_INVALID", "call envelope is invalid"), "diagnostics": {"phase": "PRE_SPAWN"}}

    def _run_discovery_snapshot(self, snapshot: ExternalServerRecord) -> dict[str, object]:
        try:
            current = self._server_registry.get(snapshot.server_id)
            if current.revision != snapshot.revision or current.endpoint != snapshot.endpoint or current.transport != snapshot.transport:
                raise _PreSpawnFailure("SERVER_AUTHORITY_CHANGED", "server authority is stale")
            binding = self._auth_binding_registry.get_binding(snapshot.server_id)
            if binding is None:
                raise _PreSpawnFailure("AUTH_BINDING_MISSING", "auth binding is required")
            current_after_binding = self._server_registry.get(snapshot.server_id)
            if current_after_binding.revision != snapshot.revision or current_after_binding.endpoint != snapshot.endpoint or current_after_binding.transport != snapshot.transport:
                raise _PreSpawnFailure("SERVER_AUTHORITY_CHANGED", "server authority is stale")
            auth = self._auth_payload(snapshot, binding)
            # The catalog is accepted only if both the server snapshot and the
            # exact binding used for materialization remain unchanged.
            self._fresh_discovery_authority(snapshot, binding)
            semantic = {
                "bridge_version": BRIDGE_VERSION,
                "endpoint": snapshot.endpoint,
                "transport": snapshot.transport,
                "auth": auth,
            }
            result = self._spawn_json(self._discovery_bridge_path, semantic, credential=auth.get("credential") if auth else None)
            self._guard_reflection(result, auth)
            if not isinstance(result, dict):
                raise _PostSpawnFailure("DISCOVERY_BRIDGE_INVALID", "discovery bridge result is invalid")
            return result
        except _PreSpawnFailure as exc:
            return {"status": NOT_INVOKED, "catalog_complete": False, "zero_tools": False, "tools": [], "diagnostics": {"phase": "PRE_SPAWN"}, "error": _safe_error(exc.code, exc.summary)}
        except _PostSpawnFailure as exc:
            return {"status": "OUTCOME_UNKNOWN", "catalog_complete": False, "zero_tools": False, "tools": [], "diagnostics": {"phase": "POST_SPAWN"}, "error": _safe_error(exc.code, exc.summary)}
        except (KeyError, TypeError, ValueError, OSError):
            return {"status": "OUTCOME_UNKNOWN", "catalog_complete": False, "zero_tools": False, "tools": [], "diagnostics": {"phase": "PRE_SPAWN"}, "error": _safe_error("DISCOVERY_BRIDGE_INVALID", "discovery bridge failed")}

    @staticmethod
    def _guard_reflection(value: Any, auth: Optional[Mapping[str, str]]) -> None:
        credential = auth.get("credential") if auth else None
        if credential and _contains(value, credential):
            raise _PostSpawnFailure(SECRET_REFLECTION_BLOCKED, "provider material was suppressed")

    @staticmethod
    def _normalize_call_result(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise _PostSpawnFailure("CALL_BRIDGE_INVALID", "call bridge result is invalid")
        status = value.get("status")
        if status not in {SUCCESS, CALL_TOOL_ERROR, NOT_INVOKED, OUTCOME_UNKNOWN}:
            raise _PostSpawnFailure("CALL_BRIDGE_INVALID", "call bridge result status is invalid")
        return {
            "status": status,
            "result": value.get("result"),
            "error": value.get("error"),
            "diagnostics": value.get("diagnostics") if isinstance(value.get("diagnostics"), dict) else {},
        }

    def _spawn_json(self, bridge_path: Path, envelope: Mapping[str, Any], *, credential: Optional[str] = None) -> Any:
        if not bridge_path.is_file():
            raise _PreSpawnFailure("BRIDGE_UNAVAILABLE", "local MCP bridge is unavailable")
        try:
            payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise _PreSpawnFailure("BRIDGE_INPUT_INVALID", "bridge input is not JSON-safe") from exc
        if len(payload) > CALL_BRIDGE_INPUT_LIMIT:
            raise _PreSpawnFailure("BRIDGE_INPUT_TOO_LARGE", "bridge input exceeded the byte limit")
        try:
            process = subprocess.Popen(
                [self._node_executable, str(bridge_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env={"PATH": os.defpath, "NODE_NO_WARNINGS": "1"},
            )
        except (OSError, ValueError) as exc:
            raise _PreSpawnFailure("BRIDGE_UNAVAILABLE", "local MCP bridge could not start") from exc
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdout = bytearray()
        stderr = bytearray()
        overflow = threading.Event()

        def read_stream(stream: Any, bucket: bytearray, limit: int) -> None:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                if len(bucket) + len(chunk) > limit:
                    overflow.set()
                    return
                bucket.extend(chunk)

        writer_error: list[BaseException] = []

        def write_input() -> None:
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except BaseException as exc:  # pragma: no cover - race-dependent OS failure
                writer_error.append(exc)

        out_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout, CALL_BRIDGE_OUTPUT_LIMIT), daemon=True)
        err_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr, CALL_BRIDGE_STDERR_LIMIT), daemon=True)
        in_thread = threading.Thread(target=write_input, daemon=True)
        out_thread.start(); err_thread.start(); in_thread.start()
        deadline = time.monotonic() + self._timeout_seconds
        timed_out = False
        while process.poll() is None:
            if overflow.is_set():
                process.kill()
                break
            if time.monotonic() >= deadline:
                timed_out = True
                process.kill()
                break
            time.sleep(0.005)
        process.wait()
        in_thread.join(timeout=1); out_thread.join(timeout=1); err_thread.join(timeout=1)
        process.stdout.close(); process.stderr.close()
        if credential and (credential.encode("utf-8") in stdout or credential.encode("utf-8") in stderr):
            raise _PostSpawnFailure(SECRET_REFLECTION_BLOCKED, "provider material was suppressed")
        if timed_out:
            raise _PostSpawnFailure("BRIDGE_TIMEOUT", "local MCP bridge timed out")
        if overflow.is_set():
            raise _PostSpawnFailure("BRIDGE_OUTPUT_LIMIT", "local MCP bridge output exceeded the byte limit")
        if writer_error:
            raise _PostSpawnFailure("BRIDGE_INPUT_FAILED", "local MCP bridge input failed")
        if process.returncode != 0:
            if stderr:
                try:
                    diagnostic = stderr.decode("utf-8", "replace")
                except Exception:
                    diagnostic = ""
                if diagnostic:
                    raise _PostSpawnFailure("BRIDGE_FAILED", "local MCP bridge exited unsuccessfully")
            raise _PostSpawnFailure("BRIDGE_FAILED", "local MCP bridge exited unsuccessfully")
        try:
            decoded = bytes(stdout).decode("utf-8")
            return json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _PostSpawnFailure("BRIDGE_MALFORMED", "local MCP bridge returned malformed JSON") from exc


__all__ = [
    "CALL_BRIDGE_INPUT_LIMIT",
    "ExternalMcpRuntime",
    "ExternalMcpRuntimeInitializationError",
    "SECRET_REFLECTION_BLOCKED",
]
