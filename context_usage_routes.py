"""Flask routes for receiving and serving context-usage snapshots."""

from __future__ import annotations

import hmac
from typing import Callable

from flask import Blueprint, jsonify, request

import context_usage_store


def create_context_usage_blueprint(*, db_path: str, report_token_getter: Callable[[], str]) -> Blueprint:
    blueprint = Blueprint("context_usage", __name__)

    @blueprint.route("/api/context-usage", methods=["GET"])
    def get_context_usage():
        response = jsonify(context_usage_store.get_snapshot(db_path))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.route("/api/context-usage/report", methods=["POST"])
    def report_context_usage():
        if request.content_length and request.content_length > 64 * 1024:
            return jsonify({"ok": False, "error": "request too large"}), 413
        expected = str(report_token_getter() or "").strip()
        if not expected:
            return jsonify({"ok": False, "error": "report token is not configured"}), 503
        header = request.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            response = jsonify({"ok": False, "error": "unauthorized"})
            response.status_code = 401
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
        try:
            accepted, snapshot = context_usage_store.save_report(
                request.get_json(silent=True),
                db_path,
            )
            return jsonify({"ok": True, "accepted": accepted, "snapshot": snapshot})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    return blueprint
