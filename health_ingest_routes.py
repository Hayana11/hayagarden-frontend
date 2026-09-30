"""Authenticated mobile-health ingest and owner diagnostics routes."""
from __future__ import annotations

import hmac
import json
from typing import Callable

from flask import Blueprint, jsonify, request

import health_store
from moments_auth import OwnerAuthError, require_owner


def create_health_blueprint(*, db_path: str, token_getter: Callable[[], str]) -> Blueprint:
    blueprint = Blueprint("health_mobile", __name__)

    @blueprint.route("/api/health/mobile/ingest", methods=["POST"])
    def ingest():
        if request.content_length and request.content_length > 256 * 1024:
            return jsonify({"ok": False, "error": "request too large"}), 413
        expected = str(token_getter() or "").strip()
        if not expected:
            return jsonify({"ok": False, "error": "ingest token is not configured"}), 503
        header = request.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            response = jsonify({"ok": False, "error": "unauthorized"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        raw = request.get_data(cache=False)
        if len(raw) > 256 * 1024:
            return jsonify({"ok": False, "error": "request too large"}), 413
        try:
            payload = json.loads(raw.decode("utf-8"))
            result = health_store.ingest_payload(payload, db_path)
            return jsonify({"ok": True, **result})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400

    @blueprint.route("/api/health/mobile/status", methods=["GET"])
    def status():
        try:
            require_owner(request)
        except OwnerAuthError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status_code
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

    return blueprint
