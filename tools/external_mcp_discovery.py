"""Administrative registry-to-discovery orchestration.

The registry remains the only identity and connection authority.  This module
accepts only a server ID at its production-facing boundary, invokes the fixed
local Node bridge with a small JSON envelope, and re-checks registry revision
before accepting a successful catalog.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .external_server_registry import (
    ExternalServerRecord,
    ExternalServerRegistry,
    REGISTRATION_STATE,
    REVIEW_REQUIRED_STATE,
    REVOKED_STATE,
    UnknownServerError,
)

BRIDGE_VERSION = 1
BRIDGE_UNAVAILABLE = "BRIDGE_UNAVAILABLE"
BRIDGE_ERROR = "BRIDGE_ERROR"
STALE = "STALE"
MAX_BRIDGE_INPUT_BYTES = 16 * 1024
MAX_BRIDGE_STDOUT_BYTES = 4 * 1024 * 1024
MAX_BRIDGE_STDERR_BYTES = 4 * 1024
CHILD_TIMEOUT_SECONDS = 17.0
_SAFE_ENV = {"PATH": os.defpath, "NODE_NO_WARNINGS": "1"}


class BridgeFailure(Exception):
    def __init__(self, status: str, summary: str):
        super().__init__(summary)
        self.status = status
        self.summary = summary


def _bounded_text(value: object, limit: int = 512) -> str:
    return " ".join(str(value).replace("\x00", " ").split())[:limit]


def _base_result(
    record: Optional[ExternalServerRecord],
    *,
    status: str,
    error: Optional[Mapping[str, object]],
    diagnostics: Optional[Mapping[str, object]] = None,
) -> dict[str, object]:
    diagnostics_out = dict(diagnostics or {})
    diagnostics_out.setdefault("registry_changed_during_attempt", False)
    return {
        "server_id": record.server_id if record else None,
        "display_name": record.display_name if record else None,
        "registration_provenance": record.registration_provenance if record else None,
        "registry_revision": record.revision if record else None,
        "lifecycle_state": record.lifecycle_state if record else None,
        "master_state": record.master_state if record else None,
        "transport": record.transport if record else None,
        "endpoint_snapshot": record.endpoint if record else None,
        "status": status,
        "catalog_complete": False,
        "zero_tools": False,
        "server_metadata": None,
        "tools": [],
        "diagnostics": diagnostics_out,
        "error": dict(error) if error else None,
        "model_visible": False,
        "execution_allowed": False,
    }


def _with_registry_provenance(
    discovered: Mapping[str, object], record: ExternalServerRecord
) -> dict[str, object]:
    result = dict(discovered)
    result.update(
        {
            "server_id": record.server_id,
            "display_name": record.display_name,
            "registration_provenance": record.registration_provenance,
            "registry_revision": record.revision,
            "lifecycle_state": record.lifecycle_state,
            "master_state": record.master_state,
            "transport": record.transport,
            "endpoint_snapshot": record.endpoint,
            "model_visible": False,
            "execution_allowed": False,
        }
    )
    return result


class SubprocessDiscoveryRunner:
    """Fixed production bridge runner; constructor overrides are test seams."""

    def __init__(
        self,
        *,
        node_executable: Optional[str] = None,
        bridge_path: Optional[Path] = None,
        timeout_seconds: float = CHILD_TIMEOUT_SECONDS,
    ) -> None:
        self._node_executable = node_executable or self._resolve_node()
        self._bridge_path = bridge_path or Path(__file__).with_name(
            "external_mcp_discovery_bridge.mjs"
        )
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _resolve_node() -> str:
        executable = shutil.which("node")
        if not executable:
            raise BridgeFailure(BRIDGE_UNAVAILABLE, "node executable is unavailable")
        return executable

    @staticmethod
    def _read_stream(stream, bucket: bytearray, limit: int, overflow: threading.Event) -> None:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            if len(bucket) + len(chunk) > limit:
                overflow.set()
                return
            bucket.extend(chunk)

    def run(self, record: ExternalServerRecord) -> dict[str, object]:
        envelope = {
            "bridge_version": BRIDGE_VERSION,
            "endpoint": record.endpoint,
            "transport": record.transport,
        }
        payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode()
        if len(payload) > MAX_BRIDGE_INPUT_BYTES:
            raise BridgeFailure(BRIDGE_ERROR, "bridge input exceeded the byte limit")

        try:
            process = subprocess.Popen(
                [self._node_executable, str(self._bridge_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=dict(_SAFE_ENV),
            )
        except OSError as exc:
            raise BridgeFailure(BRIDGE_UNAVAILABLE, "local discovery bridge could not start") from exc

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_bytes = bytearray()
        stderr_bytes = bytearray()
        overflow = threading.Event()
        stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stdout, stdout_bytes, MAX_BRIDGE_STDOUT_BYTES, overflow),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stderr, stderr_bytes, MAX_BRIDGE_STDERR_BYTES, overflow),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except OSError as exc:
            process.kill()
            process.wait()
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge input failed") from exc

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
            time.sleep(0.01)
        process.wait()
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        process.stdout.close()
        process.stderr.close()

        if timed_out:
            raise BridgeFailure(BRIDGE_UNAVAILABLE, "local discovery bridge timed out")
        if overflow.is_set():
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge output exceeded the byte limit")
        if process.returncode != 0:
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge exited unsuccessfully")
        try:
            decoded = stdout_bytes.decode("utf-8")
            output = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge returned malformed JSON") from exc
        if not isinstance(output, dict):
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge returned a non-object")
        if output.get("bridge_version") != BRIDGE_VERSION:
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge version is unsupported")
        required = {"status", "catalog_complete", "zero_tools", "tools", "diagnostics", "error"}
        if not required.issubset(output):
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge output schema is incomplete")
        if not isinstance(output["tools"], list) or not isinstance(output["diagnostics"], dict):
            raise BridgeFailure(BRIDGE_ERROR, "local discovery bridge output schema is invalid")
        output.pop("bridge_version", None)
        return output


class ExternalMcpDiscovery:
    """Administrative-only discovery facade keyed solely by ``server_id``."""

    def __init__(
        self,
        registry: ExternalServerRegistry,
        *,
        runner: Optional[Callable[[ExternalServerRecord], Mapping[str, object]]] = None,
    ) -> None:
        self._registry = registry
        self._runner = runner

    def discover(self, server_id: object) -> dict[str, object]:
        try:
            record = self._registry.get(server_id)
        except UnknownServerError:
            return _base_result(
                None,
                status="UNKNOWN_SERVER",
                error={"code": "UNKNOWN_SERVER", "summary": "server identity is unknown"},
            )
        if record.lifecycle_state == REVOKED_STATE:
            return _base_result(
                record,
                status=REVOKED_STATE,
                error={"code": REVOKED_STATE, "summary": "revoked server discovery is denied"},
            )
        if record.lifecycle_state not in (REGISTRATION_STATE, REVIEW_REQUIRED_STATE):
            return _base_result(
                record,
                status="REGISTRY_ERROR",
                error={"code": "REGISTRY_ERROR", "summary": "server lifecycle is not discoverable"},
            )

        snapshot = record
        try:
            runner = self._runner or SubprocessDiscoveryRunner().run
            discovered = dict(runner(snapshot))
        except BridgeFailure as exc:
            discovered = _base_result(
                snapshot,
                status=exc.status,
                error={"code": exc.status, "summary": exc.summary},
            )
        except Exception:
            discovered = _base_result(
                snapshot,
                status=BRIDGE_ERROR,
                error={"code": BRIDGE_ERROR, "summary": "local discovery bridge failed"},
            )

        try:
            current = self._registry.get(snapshot.server_id)
        except UnknownServerError:
            current = None
        changed = current is None or current.revision != snapshot.revision
        diagnostics = dict(discovered.get("diagnostics") or {})
        diagnostics["registry_changed_during_attempt"] = changed
        discovered["diagnostics"] = diagnostics

        if changed and discovered.get("status") == "SUCCESS":
            stale = _with_registry_provenance(
                _base_result(
                    snapshot,
                    status=STALE,
                    error={
                        "code": STALE,
                        "summary": "registry changed during discovery",
                    },
                    diagnostics=diagnostics,
                ),
                current or snapshot,
            )
            stale["registry_revision"] = current.revision if current else None
            stale["lifecycle_state"] = current.lifecycle_state if current else None
            stale["master_state"] = current.master_state if current else None
            return stale
        return _with_registry_provenance(discovered, snapshot)


def discover_external_server(
    registry: ExternalServerRegistry, server_id: object
) -> dict[str, object]:
    """Production-facing primitive.  ``server_id`` is its only request input."""
    return ExternalMcpDiscovery(registry).discover(server_id)


__all__ = [
    "BRIDGE_ERROR",
    "BRIDGE_UNAVAILABLE",
    "BRIDGE_VERSION",
    "CHILD_TIMEOUT_SECONDS",
    "BridgeFailure",
    "ExternalMcpDiscovery",
    "MAX_BRIDGE_STDERR_BYTES",
    "MAX_BRIDGE_STDOUT_BYTES",
    "STALE",
    "SubprocessDiscoveryRunner",
    "discover_external_server",
]
