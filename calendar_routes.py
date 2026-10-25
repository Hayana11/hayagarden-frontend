"""Device-authenticated phone calendar sync and the owner status view."""
from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from flask import Blueprint, jsonify, request

from moments_auth import OwnerAuthError, require_owner
from tools import calendar_store, health_store

MAX_SYNC_BYTES = 512 * 1024


def _unauthorized():
    response = jsonify({"ok": False, "error": "unauthorized"})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = "Bearer"
    return response


def device_id_from_request(health_db_path: str) -> Optional[str]:
    """Health Bridge device credential; None if absent or wrong. Never echoes it."""
    device_id = (request.headers.get("X-Health-Device-ID") or "").strip()
    authorization = request.headers.get("Authorization", "")
    credential = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    if not device_id or not credential:
        return None
    if not health_store.authenticate_device(health_db_path, device_id, credential):
        return None
    return str(uuid.UUID(device_id))


def create_calendar_blueprint(
    *,
    db_path: str,
    health_db_path: str,
    owner_guard: Callable = require_owner,
) -> Blueprint:
    blueprint = Blueprint("calendar_mobile", __name__)

    @blueprint.route("/api/calendar/mobile/sync", methods=["POST"])
    def sync():
        device_id = device_id_from_request(health_db_path)
        if device_id is None:
            return _unauthorized()
        raw = request.get_data(cache=False, as_text=False) or b""
        if len(raw) > MAX_SYNC_BYTES:
            return jsonify({"ok": False, "error": "request too large"}), 413
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"ok": False, "error": "invalid JSON"}), 400
        try:
            result = calendar_store.sync_device(db_path, device_id, payload)
        except calendar_store.CalendarError as exc:
            return jsonify({"ok": False, "error": str(exc)[:160]}), 400
        return jsonify({"ok": True, **result})

    @blueprint.route("/api/calendar/mobile/status", methods=["GET"])
    def status():
        try:
            owner_guard(request)
        except OwnerAuthError as exc:
            return jsonify({"ok": False, "error": exc.message}), exc.status_code
        response = jsonify(calendar_store.get_status(db_path))
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
