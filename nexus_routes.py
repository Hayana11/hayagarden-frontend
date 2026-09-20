"""Frozen Nexus HTTP API — /api/nexus/*."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request, stream_with_context

from moments_auth import OwnerAuthError, require_owner
from nexus_git import git_summary
from nexus_paths import NexusPathError
from nexus_runtime import NexusBusyError, NexusRuntime, NexusTurnError

_ALLOWED_TURN_KEYS = frozenset({"agent", "instruction"})
_REJECTED_CONTROL_KEYS = frozenset(
    {
        "cwd",
        "command",
        "shell",
        "env",
        "sandbox",
        "allowedTools",
        "allowed_tools",
        "mcp-config",
        "mcp_config",
        "push",
        "merge",
        "deploy",
        "reset",
        "checkout",
        "clean",
    }
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sse(event_payload: dict[str, Any]) -> str:
    event_name = event_payload.get("event") or "status"
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(event_payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _validate_instruction(value: Any) -> str:
    if not isinstance(value, str):
        raise NexusTurnError("invalid_instruction", "instruction must be a JSON string", 400)
    if len(value) < 1 or len(value) > 8000:
        raise NexusTurnError("invalid_instruction", "instruction length must be 1-8000", 400)
    if _CONTROL_CHAR_RE.search(value):
        raise NexusTurnError("invalid_instruction", "instruction contains disallowed control characters", 400)
    return value


def create_nexus_blueprint(
    runtime: NexusRuntime,
    *,
    owner_guard: Callable[[object], None] | None = None,
) -> Blueprint:
    bp = Blueprint("nexus", __name__, url_prefix="/api/nexus")
    guard = owner_guard or require_owner

    @bp.before_request
    def require_nexus_owner():
        try:
            guard(request)
        except OwnerAuthError as exc:
            response = jsonify({"ok": False, "code": "OWNER_AUTH_REQUIRED", "detail": exc.message})
            response.status_code = exc.status_code
            if exc.status_code == 401:
                response.headers["WWW-Authenticate"] = "Bearer"
            return response
        return None

    @bp.get("/status")
    def status():
        # Always return a UI-readable payload. Missing workspace degrades inside
        # runtime.status() (workspace.ok=false) instead of hard 503.
        return jsonify(runtime.status())

    @bp.post("/turn")
    def start_turn():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "code": "invalid_json", "detail": "JSON object required"}), 400

        extra = set(data.keys()) - _ALLOWED_TURN_KEYS
        if extra:
            rejected = sorted(extra)
            control = sorted(extra & _REJECTED_CONTROL_KEYS)
            return jsonify(
                {
                    "ok": False,
                    "code": "unexpected_fields",
                    "detail": "only agent and instruction are allowed",
                    "rejected_fields": rejected,
                    "control_fields": control,
                }
            ), 400

        missing = _ALLOWED_TURN_KEYS - set(data.keys())
        if missing:
            return jsonify({"ok": False, "code": "missing_fields", "detail": sorted(missing)}), 400

        try:
            instruction = _validate_instruction(data.get("instruction"))
            result = runtime.start_turn(data.get("agent"), instruction)
            return jsonify(result), 202
        except NexusBusyError as exc:
            return jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "code": "nexus_busy",
                    "retryable": True,
                }
            ), 423
        except NexusTurnError as exc:
            return jsonify({"ok": False, "code": exc.code, "detail": exc.message}), exc.status
        except NexusPathError as exc:
            return jsonify({"ok": False, "code": exc.code, "detail": exc.detail}), 503

    @bp.get("/turn/<turn_id>/events")
    def turn_events(turn_id: str):
        # Last-Event-ID is intentionally unsupported (adjudicated NO).
        # Validate turn + subscriber quota BEFORE creating the SSE Response so
        # turn_not_found / too_many_subscribers never ride a 200 stream.
        try:
            turn = runtime.reserve_event_subscription(turn_id)
        except NexusTurnError as exc:
            return jsonify({"ok": False, "code": exc.code, "detail": exc.message}), exc.status

        @stream_with_context
        def generate():
            try:
                for event in runtime.iter_events_for_turn(turn, after_sequence=0):
                    yield _sse(event)
            finally:
                runtime.release_event_subscription(turn_id)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @bp.post("/turn/<turn_id>/interrupt")
    def interrupt_turn(turn_id: str):
        try:
            return jsonify(runtime.interrupt(turn_id))
        except NexusTurnError as exc:
            return jsonify({"ok": False, "code": exc.code, "detail": exc.message}), exc.status

    @bp.get("/git")
    def git():
        try:
            return jsonify(git_summary(runtime.workspace))
        except NexusPathError as exc:
            return jsonify({"ok": False, "code": exc.code, "detail": exc.detail}), 503

    @bp.get("/turns")
    def turns():
        return jsonify({"turns": runtime.list_turns()})

    return bp
