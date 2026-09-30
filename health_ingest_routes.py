"""Owner-controlled health device enrollment and device-authenticated ingest."""
from __future__ import annotations

import json
from typing import Callable, Optional

from flask import Blueprint, jsonify, request

from daily_context_bff import same_origin_mutation_ok
from moments_auth import OwnerAuthError, require_owner
from tools import health_store

MAX_INGEST_BYTES = 256 * 1024
MAX_ENROLL_BYTES = 16 * 1024


def _auth_error(exc: OwnerAuthError):
    return jsonify({"ok": False, "error": exc.message}), exc.status_code


def _owner_denied(owner_guard: Callable) -> Optional[object]:
    try:
        owner_guard(request)
    except OwnerAuthError as exc:
        return _auth_error(exc)
    return None


def _cross_origin_denied():
    return jsonify({"ok": False, "error": "cross-origin mutation rejected"}), 403


def _unauthorized():
    response = jsonify({"ok": False, "error": "unauthorized"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _json_body(max_bytes: int):
    raw = request.get_data(cache=False, as_text=False) or b""
    if len(raw) > max_bytes:
        return None, (jsonify({"ok": False, "error": "request too large"}), 413)
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, (jsonify({"ok": False, "error": "invalid JSON"}), 400)


def create_health_blueprint(
    *,
    db_path: str,
    owner_guard: Callable = require_owner,
) -> Blueprint:
    blueprint = Blueprint("health_mobile", __name__)

    @blueprint.route("/api/health/mobile/enroll", methods=["POST"])
    def enroll():
        denied = _owner_denied(owner_guard)
        if denied is not None:
            return denied
        if not same_origin_mutation_ok(request):
            return _cross_origin_denied()
        payload, error = _json_body(MAX_ENROLL_BYTES)
        if error is not None:
            return error
        try:
            result = health_store.enroll_device(payload, db_path)
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400
        return jsonify({"ok": True, **result})

    @blueprint.route("/api/health/mobile/ingest", methods=["POST"])
    def ingest():
        if request.content_length and request.content_length > MAX_INGEST_BYTES:
            return jsonify({"ok": False, "error": "request too large"}), 413
        device_id = (request.headers.get("X-Health-Device-ID") or "").strip()
        authorization = request.headers.get("Authorization", "")
        credential = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        if not health_store.authenticate_device(db_path, device_id, credential):
            return _unauthorized()
        payload, error = _json_body(MAX_INGEST_BYTES)
        if error is not None:
            return error
        try:
            result = health_store.ingest_payload(payload, db_path)
            return jsonify({"ok": True, **result})
        except (TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400

    @blueprint.route("/api/health/mobile/status", methods=["GET"])
    def status():
        denied = _owner_denied(owner_guard)
        if denied is not None:
            return denied
        try:
            result = health_store.get_status(db_path)
        except (OSError, ValueError):
            result = {
                "schemaVersion": health_store.SCHEMA_VERSION,
                "available": False,
                "lastIngestAt": None,
                "lastCollectedAt": None,
                "acceptedRows": 0,
                "metrics": {},
            }
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.route("/api/health/mobile/devices", methods=["GET"])
    def devices():
        denied = _owner_denied(owner_guard)
        if denied is not None:
            return denied
        return jsonify({"schemaVersion": health_store.SCHEMA_VERSION, "devices": health_store.list_devices(db_path)})

    @blueprint.route("/api/health/mobile/devices/<device_id>/revoke", methods=["POST"])
    def revoke(device_id: str):
        denied = _owner_denied(owner_guard)
        if denied is not None:
            return denied
        if not same_origin_mutation_ok(request):
            return _cross_origin_denied()
        try:
            health_store.revoke_device(db_path, device_id)
        except ValueError:
            # Keep revocation responses non-enumerating and idempotent.
            pass
        return jsonify({"ok": True, "deviceId": device_id, "revoked": True})

    return blueprint
