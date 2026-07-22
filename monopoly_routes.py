"""Public Flask API for monopoly rooms."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from flask import Blueprint, Response, jsonify, request, stream_with_context

import monopoly_store as store
from moments_auth import OwnerAuthError, require_owner
from monopoly_rooms import MonopolyService, RoomError, RoomStatus


def _load_internal_token() -> str:
    token = os.environ.get("MONOPOLY_INTERNAL_TOKEN", "").strip()
    if token:
        return token
    try:
        with open("/opt/frontend/.env", encoding="utf-8") as env_file:
            for line in env_file:
                key, sep, value = line.partition("=")
                if sep and key.strip() == "MONOPOLY_INTERNAL_TOKEN":
                    return value.strip()
    except OSError:
        pass
    return ""


class GatewayAgentNotifier:
    """Fire-and-forget bridge to the existing AI gateway on localhost."""

    def __init__(self, url: str = "http://127.0.0.1:5051/monopoly/generate", token: str | None = None):
        self.url = url
        self.token = token if token is not None else _load_internal_token()

    def __call__(self, payload: dict) -> None:
        def send() -> None:
            req = urllib.request.Request(
                self.url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Monopoly-Internal-Token": self.token,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    response.read(256)
            except (OSError, urllib.error.URLError):
                # Generation availability is surfaced by agent status; a failed
                # notification must never roll back a saved user/game action.
                return

        threading.Thread(target=send, daemon=True, name="monopoly-agent-notify").start()


def _error_response(exc: RoomError):
    body = {"ok": False, "code": exc.code, "detail": exc.detail}
    if exc.latest is not None:
        body["latest"] = exc.latest
    return jsonify(body), exc.status


def _sse(event: str, data: dict, *, event_id: str | int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def create_monopoly_blueprint(
    service: MonopolyService,
    *,
    notify_agents: Callable[[dict], None] | None = None,
    owner_guard: Callable[[object], None] | None = None,
) -> Blueprint:
    bp = Blueprint("monopoly", __name__, url_prefix="/api/monopoly")
    notifier = notify_agents or GatewayAgentNotifier()
    guard = owner_guard or require_owner

    @bp.before_request
    def require_monopoly_owner():
        try:
            guard(request)
        except OwnerAuthError as exc:
            response = jsonify({"ok": False, "code": "OWNER_AUTH_REQUIRED", "detail": exc.message})
            response.status_code = exc.status_code
            if exc.status_code == 401:
                response.headers["WWW-Authenticate"] = "Bearer"
            return response
        return None

    @bp.post("/rooms")
    def create_room():
        data = request.get_json(silent=True) or {}
        try:
            snapshot = service.create_room(mode=data.get("mode", "two_player_three_chat"), seats=data.get("seats"))
            return jsonify(snapshot), 201
        except RoomError as exc:
            return _error_response(exc)

    @bp.get("/rooms/<room_id>")
    def get_room(room_id: str):
        try:
            return jsonify(service.snapshot(room_id))
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/setup")
    def setup_room(room_id: str):
        data = request.get_json(silent=True) or {}
        expected = data.pop("expectedEventSeq", data.pop("expected_seq", None))
        try:
            snapshot = service.setup(room_id, data, expected_seq=expected)
            notifier({"room_id": room_id, "reason": "turn", "targets": ["cc", "codex"]})
            return jsonify(snapshot)
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/actions")
    def room_action(room_id: str):
        data = request.get_json(silent=True) or {}
        data["actor"] = "haya"
        try:
            snapshot = service.execute(room_id, data)
            notifier({"room_id": room_id, "reason": "turn", "targets": ["cc", "codex"]})
            return jsonify(snapshot)
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/messages")
    def room_message(room_id: str):
        data = request.get_json(silent=True) or {}
        try:
            result = service.post_message(
                room_id,
                author="haya",
                content=data.get("content", ""),
                reply_to=data.get("reply_to"),
                safe_word=bool(data.get("safe_word") or data.get("safeWord")),
            )
            if result["snapshot"]["room"]["status"] != RoomStatus.PAUSED.value:
                targets = data.get("targets")
                if not isinstance(targets, list):
                    targets = ["cc", "codex"]
                targets = list(dict.fromkeys(actor for actor in targets if actor in {"cc", "codex"}))
                notifier({
                    "room_id": room_id,
                    "reason": "user_message",
                    "targets": targets,
                    "reply_to": result["message"]["id"],
                })
            return jsonify({"ok": True, **result})
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/speak")
    def room_speak(room_id: str):
        data = request.get_json(silent=True) or {}
        try:
            snapshot = service.snapshot(room_id)
            if snapshot["room"]["status"] == RoomStatus.PAUSED.value:
                raise RoomError("GAME_PAUSED", "游戏已停，AI 不会继续演绎", status=409)
            targets = data.get("targets")
            if not isinstance(targets, list) or not targets:
                targets = ["cc", "codex"]
            targets = list(dict.fromkeys(actor for actor in targets if actor in {"cc", "codex"}))
            notifier({"room_id": room_id, "reason": "continue", "targets": targets})
            return jsonify({"ok": True})
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/pause")
    def pause_room(room_id: str):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(service.pause(room_id, expected_seq=data.get("expectedEventSeq")))
        except RoomError as exc:
            return _error_response(exc)

    @bp.post("/rooms/<room_id>/resume")
    def resume_room(room_id: str):
        data = request.get_json(silent=True) or {}
        try:
            return jsonify(service.resume(room_id, expected_seq=data.get("expectedEventSeq")))
        except RoomError as exc:
            return _error_response(exc)

    @bp.delete("/rooms/<room_id>")
    def delete_room(room_id: str):
        try:
            service.delete(room_id)
            return jsonify({"ok": True})
        except RoomError as exc:
            return _error_response(exc)

    @bp.get("/rooms/<room_id>/stream")
    def room_stream(room_id: str):
        try:
            snapshot = service.snapshot(room_id)
        except RoomError as exc:
            return _error_response(exc)
        after = request.args.get("after", 0, type=int) or 0
        last_message_id = max((row["id"] for row in snapshot["messages"]), default=0)
        initial_live_id = store.latest_live_event_id(room_id, service.db_path)

        @stream_with_context
        def generate():
            # A brand-new client receives the complete snapshot and then only
            # live events. Reconnecting clients pass their last non-zero seq
            # and receive the exact persisted gap.
            event_seq = int(snapshot["room"]["event_seq"]) if after <= 0 else after
            message_id = last_message_id
            live_id = initial_live_id
            yield _sse("room.snapshot", {"type": "room.snapshot", "data": snapshot}, event_id=event_seq)
            last_keepalive = time.monotonic()
            while True:
                emitted = False
                events, messages, live_events = store.poll_room_stream(
                    room_id,
                    after_seq=event_seq,
                    after_message_id=message_id,
                    after_live_id=live_id,
                    db_path=service.db_path,
                )
                for event in events:
                    event_seq = event["seq"]
                    if event["type"] == "state":
                        envelope = {"type": "game.state", "seq": event_seq, "data": event["payload"]}
                        name = "game.state"
                    elif event["type"] == "pending":
                        payload = event["payload"] or {}
                        if isinstance(payload, dict) and (
                            "pending" in payload or "status" in payload
                        ):
                            envelope = {
                                "type": "game.pending",
                                "seq": event_seq,
                                "status": payload.get("status"),
                                "data": payload.get("pending"),
                            }
                        else:
                            envelope = {
                                "type": "game.pending",
                                "seq": event_seq,
                                "data": payload or None,
                            }
                        name = "game.pending"
                    elif event["type"] == "room_error":
                        envelope = {"type": "room.error", "data": event["payload"]}
                        name = "room.error"
                    else:
                        envelope = {"type": "game.event", "seq": event_seq, "data": event}
                        name = "game.event"
                    yield _sse(name, envelope, event_id=event_seq)
                    emitted = True
                for event in live_events:
                    live_id = event["id"]
                    if event["type"] == "agent_status":
                        envelope = {
                            "type": "agent.status",
                            "actor": event.get("actor"),
                            "data": event["payload"],
                        }
                        name = "agent.status"
                    else:
                        name = event["type"].replace("_", ".")
                        envelope = {
                            "type": name,
                            "actor": event.get("actor"),
                            **event["payload"],
                        }
                    yield _sse(name, envelope, event_id=f"l{live_id}")
                    emitted = True
                for message in messages:
                    message_id = message["id"]
                    yield _sse("chat.message", {"type": "chat.message", "data": message}, event_id=f"m{message_id}")
                    emitted = True
                now = time.monotonic()
                if not emitted and now - last_keepalive >= 15:
                    yield ": keepalive\n\n"
                    last_keepalive = now
                time.sleep(0.5)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return bp

