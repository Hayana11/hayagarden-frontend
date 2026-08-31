"""Owner-only External MCP administration routes.

The route surface exposes connection state and tool presence only.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
import re
from typing import Any, Callable, Mapping

from flask import Blueprint, jsonify, request

from moments_auth import OwnerAuthError, require_owner
from tools.external_mcp_production import open_external_mcp_production
from tools.external_server_registry import DISCONNECTED_STATE, DuplicateEndpointError, RegistryError

_EXPECTED_TOP_LEVEL = frozenset({"display_name", "endpoint", "auth"})
_SAFE_CODE_RE = re.compile(r"^[A-Z0-9_]{2,64}$")


def _error_code(error: object, fallback: str = "DISCOVERY_FAILED") -> str:
    code = error.get("code") if isinstance(error, Mapping) else getattr(error, "code", None)
    return code if isinstance(code, str) and _SAFE_CODE_RE.fullmatch(code) else fallback


def _validate_payload(payload: object) -> tuple[str, str, str, str | None] | None:
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_TOP_LEVEL:
        return None
    display_name, endpoint, auth = payload["display_name"], payload["endpoint"], payload["auth"]
    if not isinstance(display_name, str) or not isinstance(endpoint, str) or not isinstance(auth, dict):
        return None
    scheme = auth.get("scheme")
    if scheme == "none" and set(auth) == {"scheme"}:
        return display_name, endpoint, scheme, None
    if scheme == "bearer" and set(auth) == {"scheme", "credential"}:
        credential = auth["credential"]
        if isinstance(credential, str) and credential:
            return display_name, endpoint, scheme, credential
    return None


def _safe_error(code: str) -> tuple[Any, int]:
    return jsonify({"ok": False, "reason_code": _error_code({"code": code}, "EXTERNAL_MCP_ADMIN_FAILED")}), 400


def create_external_mcp_admin_blueprint(
    *,
    graph_factory: Callable[[], AbstractContextManager] = open_external_mcp_production,
    owner_required: Callable[[Any], Any] | None = None,
) -> Blueprint:
    bp = Blueprint("external_mcp_admin", __name__)

    def owner_error():
        try:
            (owner_required or require_owner)(request)
        except OwnerAuthError as exc:
            response = jsonify({"ok": False, "reason_code": "OWNER_REQUIRED"})
            response.status_code = exc.status_code
            if exc.status_code == 401:
                response.headers["WWW-Authenticate"] = "Bearer"
            return response
        except Exception:
            response = jsonify({"ok": False, "reason_code": "OWNER_REQUIRED"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        return None

    def public_server(graph, server):
        candidates = graph.candidate_registry.list_candidates(server.server_id)
        binding = graph.auth_binding_registry.get_binding(server.server_id)
        result = server.to_public_dict()
        result.pop("registration_provenance", None)
        result.update({
            "auth_scheme": binding.auth_scheme if binding else None,
            "configured": binding is not None,
            "present_tool_count": sum(item["presence_state"] == "PRESENT" for item in candidates),
        })
        return result

    def discover_once(graph, server_id: str):
        try:
            discovery = graph.runtime.discover(server_id)
        except Exception:
            discovery = None
        if not isinstance(discovery, Mapping):
            discovery = {"error": {"code": "DISCOVERY_FAILED"}}
        if discovery.get("status") == "SUCCESS" and discovery.get("catalog_complete") is True:
            try:
                result = graph.candidate_registry.ingest(discovery)
                return result, discovery, None
            except Exception:
                current = graph.server_registry.get(server_id)
                graph.server_registry.mark_disconnected(server_id, expected_revision=current.revision)
                return None, discovery, "CANDIDATE_INGEST_FAILED"
        current = graph.server_registry.get(server_id)
        if current.lifecycle_state != DISCONNECTED_STATE:
            graph.server_registry.mark_disconnected(server_id, expected_revision=current.revision)
        if discovery.get("status") == "SUCCESS":
            return None, discovery, "INCOMPLETE_DISCOVERY"
        return None, discovery, _error_code(discovery.get("error"))

    @bp.get("/api/external-mcp/servers")
    def list_servers():
        denied = owner_error()
        if denied is not None: return denied
        with graph_factory() as graph:
            return jsonify({"ok": True, "servers": [public_server(graph, item) for item in graph.server_registry.list()]})

    @bp.post("/api/external-mcp/servers")
    def register_server():
        denied = owner_error()
        if denied is not None: return denied
        payload = request.get_json(silent=True)
        validated = _validate_payload(payload)
        if validated is None:
            return _safe_error("INVALID_REQUEST")
        display_name, endpoint, auth_scheme, credential = validated
        try:
            with graph_factory() as graph:
                server = graph.server_registry.register(display_name=display_name, endpoint=endpoint, transport="streamable_http", provenance="toolroom-owner")
                try:
                    if auth_scheme == "none":
                        graph.auth_binding_registry.set_binding(server.server_id, "none")
                    else:
                        secret_record = graph.secret_store.create(
                            server_id=server.server_id,
                            credential_slot="toolroom",
                            secret=credential,
                        )
                        graph.auth_binding_registry.set_binding(server.server_id, "bearer", secret_ref=secret_record.secret_ref, credential_slot="toolroom")
                except Exception as exc:
                    return jsonify({"ok": False, "connected": False, "status": server.lifecycle_state, "auth_configured": False, "discovery": {"status": "NOT_ATTEMPTED", "reason_code": _error_code(exc, "AUTH_CONFIGURATION_FAILED"), "tool_count": 0}}), 207
                result, discovery, error = discover_once(graph, server.server_id)
                if error:
                    current = graph.server_registry.get(server.server_id)
                    return jsonify({"ok": False, "connected": False, "status": current.lifecycle_state, "reason_code": error, "discovery": {"status": current.lifecycle_state, "reason_code": error}}), 400
                current = graph.server_registry.get(server.server_id)
                return jsonify({"ok": True, "connected": True, "status": current.lifecycle_state, "tool_count": result["present_tool_count"], "discovery": {"status": "SUCCESS", "tool_count": result["present_tool_count"]}})
        except DuplicateEndpointError as exc:
            return jsonify({"ok": False, "reason_code": _error_code(exc, "DUPLICATE_ACTIVE_ENDPOINT")}), 409
        except (RegistryError, ValueError) as exc:
            return _safe_error(_error_code(exc, "INVALID_SERVER"))
        except Exception:
            return jsonify({"ok": False, "reason_code": "EXTERNAL_MCP_ADMIN_FAILED"}), 503

    @bp.post("/api/external-mcp/servers/<server_id>/check")
    def check_server(server_id: str):
        denied = owner_error()
        if denied is not None: return denied
        try:
            with graph_factory() as graph:
                result, discovery, error = discover_once(graph, server_id)
                current = graph.server_registry.get(server_id)
                if error:
                    return jsonify({"ok": False, "connected": False, "status": current.lifecycle_state, "reason_code": error, "discovery": {"status": current.lifecycle_state, "reason_code": error}}), 400
                return jsonify({"ok": True, "connected": True, "status": current.lifecycle_state, "tool_count": result["present_tool_count"], "discovery": {"status": "SUCCESS", "tool_count": result["present_tool_count"]}})
        except (RegistryError, ValueError) as exc:
            return _safe_error(_error_code(exc))
        except Exception:
            return jsonify({"ok": False, "reason_code": "EXTERNAL_MCP_ADMIN_FAILED"}), 503

    return bp


__all__ = ["create_external_mcp_admin_blueprint"]
