"""Owner-only administrative surface for registering external MCP servers.

This module intentionally orchestrates only the already-composed production graph.
It never opens the production DB/key itself, invokes tools, approves candidates, or
changes model/execution visibility.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Callable, Iterator

from flask import Blueprint, jsonify, request

from moments_auth import OwnerAuthError, require_owner
from tools.external_mcp_production import open_external_mcp_production
from tools.external_server_registry import (
    DuplicateEndpointError,
    RegistryError,
    RegistryValidationError,
)

_EXPECTED_TOP_LEVEL = frozenset({"display_name", "endpoint", "auth"})
_EXPECTED_AUTH_FIELDS = frozenset({"scheme", "credential"})
_SAFE_DISCOVERY_FAILURE = "DISCOVERY_FAILED"
_GENERIC_FAILURE = "EXTERNAL_MCP_ADMIN_FAILED"


def _owner_error_response(exc: OwnerAuthError):
    response = jsonify({"ok": False, "error": exc.message})
    response.status_code = exc.status_code
    if exc.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _safe_server(record) -> dict[str, object]:
    return {
        "server_id": record.server_id,
        "display_name": record.display_name,
        "endpoint": record.endpoint,
        "transport": record.transport,
        "lifecycle_state": record.lifecycle_state,
        "master_state": record.master_state,
        "revision": record.revision,
    }


def _error_code(error: object, fallback: str = _SAFE_DISCOVERY_FAILURE) -> str:
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code and len(code) <= 128:
            return code
    return fallback


def _discovery_summary(result: object) -> dict[str, object]:
    if not isinstance(result, Mapping):
        return {"status": "FAILED", "reason_code": _SAFE_DISCOVERY_FAILURE, "tool_count": 0}
    status = result.get("status")
    status_value = status if isinstance(status, str) and status else "FAILED"
    tools = result.get("tools")
    tool_count = len(tools) if isinstance(tools, list) and status_value == "SUCCESS" else 0
    reason_code = None if status_value == "SUCCESS" else _error_code(result.get("error"))
    return {
        "status": status_value,
        "reason_code": reason_code,
        "tool_count": tool_count,
    }


def _partial_response(
    *,
    server,
    scheme: str,
    auth_configured: bool,
    discovery: Mapping[str, object],
    status_code: int = 207,
):
    payload = {
        "ok": False,
        "connection_saved": True,
        "auth_configured": auth_configured,
        "server": _safe_server(server),
        "auth": {"scheme": scheme, "configured": auth_configured},
        "discovery": dict(discovery),
    }
    return jsonify(payload), status_code


def _validate_payload(payload: object) -> tuple[str, str, str, str | None] | tuple[None, None, None, None]:
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_TOP_LEVEL:
        return None, None, None, None
    display_name = payload.get("display_name")
    endpoint = payload.get("endpoint")
    auth = payload.get("auth")
    if not isinstance(display_name, str) or not isinstance(endpoint, str) or not isinstance(auth, dict):
        return None, None, None, None
    scheme = auth.get("scheme")
    if scheme not in {"none", "bearer"}:
        return None, None, None, None
    if scheme == "none":
        if set(auth) != {"scheme"}:
            return None, None, None, None
        return display_name, endpoint, scheme, None
    if set(auth) != _EXPECTED_AUTH_FIELDS:
        return None, None, None, None
    credential = auth.get("credential")
    if not isinstance(credential, str) or not credential:
        return None, None, None, None
    return display_name, endpoint, scheme, credential


def create_external_mcp_admin_blueprint(
    graph_opener: Callable[[], Iterator[object]] | None = None,
) -> Blueprint:
    opener = graph_opener or open_external_mcp_production
    blueprint = Blueprint("external_mcp_admin", __name__)

    @blueprint.post("/api/external-mcp/servers")
    def create_external_mcp_server():
        try:
            require_owner(request)
        except OwnerAuthError as exc:
            return _owner_error_response(exc)

        payload = request.get_json(silent=True)
        display_name, endpoint, scheme, credential = _validate_payload(payload)
        if display_name is None:
            return jsonify({"ok": False, "error": "INVALID_REQUEST"}), 400

        server = None
        auth_configured = False
        try:
            with opener() as graph:
                try:
                    server = graph.server_registry.register(
                        display_name=display_name,
                        endpoint=endpoint,
                        transport="streamable_http",
                        provenance="toolroom-owner",
                    )
                except DuplicateEndpointError as exc:
                    return jsonify({"ok": False, "error": exc.code}), 409
                except RegistryValidationError as exc:
                    return jsonify({"ok": False, "error": exc.code}), 400
                except RegistryError as exc:
                    return jsonify({"ok": False, "error": exc.code}), 400

                try:
                    if scheme == "none":
                        graph.auth_binding_registry.set_binding(server.server_id, "none")
                    else:
                        secret_record = graph.secret_store.create(
                            server_id=server.server_id,
                            credential_slot="toolroom",
                            secret=credential,
                        )
                        graph.auth_binding_registry.set_binding(
                            server.server_id,
                            "bearer",
                            secret_ref=secret_record.secret_ref,
                            credential_slot="toolroom",
                        )
                    auth_configured = True
                except Exception as exc:
                    code = getattr(exc, "code", "AUTH_CONFIGURATION_FAILED")
                    if not isinstance(code, str) or not code:
                        code = "AUTH_CONFIGURATION_FAILED"
                    return _partial_response(
                        server=server,
                        scheme=scheme,
                        auth_configured=False,
                        discovery={
                            "status": "NOT_ATTEMPTED",
                            "reason_code": code,
                            "tool_count": 0,
                        },
                    )

                try:
                    discovery_result = graph.runtime.discover(server.server_id)
                except Exception:
                    return _partial_response(
                        server=server,
                        scheme=scheme,
                        auth_configured=True,
                        discovery={
                            "status": "FAILED",
                            "reason_code": _SAFE_DISCOVERY_FAILURE,
                            "tool_count": 0,
                        },
                    )

                discovery = _discovery_summary(discovery_result)
                if (
                    discovery["status"] == "SUCCESS"
                    and isinstance(discovery_result, Mapping)
                    and discovery_result.get("catalog_complete") is True
                ):
                    try:
                        graph.candidate_registry.ingest(discovery_result)
                    except Exception:
                        return _partial_response(
                            server=server,
                            scheme=scheme,
                            auth_configured=True,
                            discovery={
                                "status": "FAILED",
                                "reason_code": "CANDIDATE_INGEST_FAILED",
                                "tool_count": 0,
                            },
                        )
                    return jsonify(
                        {
                            "ok": True,
                            "connection_saved": True,
                            "auth_configured": True,
                            "server": _safe_server(server),
                            "auth": {"scheme": scheme, "configured": True},
                            "discovery": discovery,
                        }
                    ), 201

                return _partial_response(
                    server=server,
                    scheme=scheme,
                    auth_configured=True,
                    discovery=discovery,
                )
        except (OSError, RuntimeError):
            if server is not None:
                return _partial_response(
                    server=server,
                    scheme=scheme,
                    auth_configured=auth_configured,
                    discovery={
                        "status": "FAILED",
                        "reason_code": _GENERIC_FAILURE,
                        "tool_count": 0,
                    },
                )
            return jsonify({"ok": False, "error": _GENERIC_FAILURE}), 503
        except Exception:
            if server is not None:
                return _partial_response(
                    server=server,
                    scheme=scheme,
                    auth_configured=auth_configured,
                    discovery={
                        "status": "FAILED",
                        "reason_code": _GENERIC_FAILURE,
                        "tool_count": 0,
                    },
                )
            return jsonify({"ok": False, "error": _GENERIC_FAILURE}), 503

    return blueprint


__all__ = ["create_external_mcp_admin_blueprint"]
