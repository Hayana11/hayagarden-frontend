"""Owner-only External MCP administration routes.

The route surface exposes connection state and tool presence only.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Callable, Mapping

from flask import Blueprint, jsonify, request

from tools.external_mcp_production import open_external_mcp_production
from tools.external_server_registry import DISCONNECTED_STATE, InvalidStateTransitionError, RegistryError


def _safe_error(code: str) -> tuple[Any, int]:
    return jsonify({"ok": False, "reason_code": code}), 400


def create_external_mcp_admin_blueprint(
    *,
    graph_factory: Callable[[], AbstractContextManager] = open_external_mcp_production,
    owner_required: Callable[[Any], Any] | None = None,
) -> Blueprint:
    bp = Blueprint("external_mcp_admin", __name__)

    def owner_error():
        if owner_required is None:
            try:
                from moments_auth import OwnerAuthError, require_owner
                require_owner(request)
            except OwnerAuthError as exc:
                response = jsonify({"ok": False, "reason_code": "OWNER_REQUIRED"})
                response.status_code = getattr(exc, "status_code", 401)
                return response
            return None
        try:
            owner_required(request)
        except Exception:
            return jsonify({"ok": False, "reason_code": "OWNER_REQUIRED"}), 401
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
        discovery = graph.runtime.discover(server_id)
        if discovery.get("status") == "SUCCESS" and discovery.get("catalog_complete") is True:
            try:
                result = graph.candidate_registry.ingest(discovery)
                return result, discovery, None
            except (RegistryError, ValueError):
                current = graph.server_registry.get(server_id)
                graph.server_registry.mark_disconnected(server_id, expected_revision=current.revision)
                return None, discovery, "DISCOVERY_INGEST_FAILED"
        current = graph.server_registry.get(server_id)
        if current.lifecycle_state != DISCONNECTED_STATE:
            graph.server_registry.mark_disconnected(server_id, expected_revision=current.revision)
        error = discovery.get("error") if isinstance(discovery.get("error"), Mapping) else {}
        return None, discovery, str(error.get("code") or "DISCOVERY_FAILED")

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
        if not isinstance(payload, Mapping): return _safe_error("INVALID_JSON")
        try:
            with graph_factory() as graph:
                server = graph.server_registry.register(display_name=payload.get("display_name"), endpoint=payload.get("endpoint"), transport=payload.get("transport", "streamable_http"), provenance=payload.get("provenance", "settings-admin"))
                auth = payload.get("auth") if isinstance(payload.get("auth"), Mapping) else payload
                auth_scheme = auth.get("scheme", auth.get("auth_scheme", "none"))
                secret_ref = auth.get("secret_ref")
                credential_slot = auth.get("credential_slot")
                if auth_scheme == "bearer" and secret_ref is None:
                    credential = auth.get("credential")
                    secret_record = graph.secret_store.create(
                        server_id=server.server_id,
                        credential_slot=credential_slot or "default",
                        secret=credential,
                    )
                    secret_ref = secret_record.secret_ref
                    credential_slot = secret_record.credential_slot
                graph.auth_binding_registry.set_binding(server.server_id, auth_scheme, secret_ref=secret_ref, credential_slot=credential_slot)
                result, discovery, error = discover_once(graph, server.server_id)
                if error:
                    current = graph.server_registry.get(server.server_id)
                    return jsonify({"ok": False, "connected": False, "status": current.lifecycle_state, "reason_code": error, "discovery": {"status": current.lifecycle_state, "reason_code": error}}), 400
                current = graph.server_registry.get(server.server_id)
                return jsonify({"ok": True, "connected": True, "status": current.lifecycle_state, "tool_count": result["present_tool_count"], "discovery": {"status": "SUCCESS", "tool_count": result["present_tool_count"]}})
        except (RegistryError, ValueError) as exc:
            return _safe_error(getattr(exc, "code", "INVALID_SERVER"))

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
            return _safe_error(getattr(exc, "code", "DISCOVERY_FAILED"))

    return bp


__all__ = ["create_external_mcp_admin_blueprint"]

