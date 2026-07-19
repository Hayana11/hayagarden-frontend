"""Monopoly room state machine and command dispatcher.

This is the only module allowed to translate user/agent intent into engine
calls.  Chat text never reaches the engine.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

import monopoly_store as store
from monopoly_engine import EngineClient, EngineError, EngineUnavailable, EngineValidationError


class RoomStatus(str, Enum):
    LOBBY = "lobby"
    SETUP = "setup"
    IDLE = "idle"
    TASK_PENDING = "task_pending"
    DUEL_PENDING = "duel_pending"
    TOLL_PENDING = "toll_pending"
    SUPER_PENDING = "super_pending"
    JAIL_TURN = "jail_turn"
    FINISHED = "finished"
    ENGINE_DOWN = "engine_down"
    PAUSED = "paused"


class RoomError(RuntimeError):
    def __init__(self, code: str, detail: str, *, status: int = 400, latest: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status
        self.latest = latest


@dataclass
class PendingDecision:
    kind: str
    actor: str
    default: dict | None
    chosen: dict | None
    created_seq: int

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "actor": self.actor,
            "default": self.default,
            "chosen": self.chosen,
            "created_seq": self.created_seq,
        }


class TokenCipher:
    """Fernet wrapper using the existing root-owned HayaGarden vault key."""

    def __init__(self, key_file: str | None = None):
        self.key_file = key_file or os.environ.get("MONOPOLY_TOKEN_KEY_FILE")

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, value: str) -> str:
        try:
            from relay.credential_vault import encrypt_secret
            return encrypt_secret(value, key_file=self.key_file)
        except Exception as exc:
            raise RoomError("TOKEN_CIPHER_NOT_CONFIGURED", "删局令牌保险箱不可用", status=503) from exc

    def decrypt(self, value: str) -> str:
        try:
            from relay.credential_vault import decrypt_secret
            return decrypt_secret(value, key_file=self.key_file)
        except Exception as exc:
            raise RoomError("TOKEN_DECRYPT_FAILED", "删局令牌无法解密", status=500) from exc


class PassthroughTokenCipher(TokenCipher):
    """Test-only cipher injectable by callers; never selected from environment."""
    def encrypt(self, value: str) -> str:
        return "test:" + value

    def decrypt(self, value: str) -> str:
        return value.removeprefix("test:")


class RoomLockRegistry:
    """Thread + OS file lock, so gunicorn workers cannot double-roll a room."""

    def __init__(self, lock_dir: str | None = None):
        self.lock_dir = lock_dir or os.environ.get(
            "MONOPOLY_LOCK_DIR", os.path.join(tempfile.gettempdir(), "hayagarden-monopoly-locks")
        )
        os.makedirs(self.lock_dir, exist_ok=True)
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    @contextmanager
    def hold(self, room_id: str) -> Iterator[None]:
        with self._guard:
            local = self._locks.setdefault(room_id, threading.RLock())
        digest = hashlib.sha256(room_id.encode("utf-8")).hexdigest()
        path = os.path.join(self.lock_dir, digest + ".lock")
        with local, open(path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


_PENDING_DEFAULTS = {
    "task": {"task": "done"},
    "truth": {"task": "done"},
    "toll": {"toll": "pay"},
    "super": {"super_action": "done"},
    "duel": None,
}

_PENDING_STATUSES = {
    "task": RoomStatus.TASK_PENDING.value,
    "truth": RoomStatus.TASK_PENDING.value,
    "duel": RoomStatus.DUEL_PENDING.value,
    "toll": RoomStatus.TOLL_PENDING.value,
    "super": RoomStatus.SUPER_PENDING.value,
}

_IMMEDIATE_ACTION_PARAMS = {
    "swap": ("who",),
    "buy_card": ("who",),
    "use_card": ("who", "index"),
    "discard": ("who", "index"),
    "reroll_identity": ("who",),
    "reroll_task": ("who",),
    "id_event": ("who", "event"),
    "extra_task": ("who",),
    "guess_mark": ("guesser", "part"),
    "declare_persona": ("who", "persona"),
}


def _extract_game_id(payload: dict) -> str:
    for key in ("game_id", "id", "gameId"):
        if payload.get(key):
            return str(payload[key])
    raise RoomError("ENGINE_BAD_RESPONSE", "开局响应没有 game_id", status=502)


def _extract_token(payload: dict) -> str:
    for key in ("player_token", "token", "delete_token"):
        if payload.get(key):
            return str(payload[key])
    raise RoomError("ENGINE_BAD_RESPONSE", "开局响应没有删局令牌", status=502)


def _extract_state(payload: dict, fallback: dict | None = None) -> dict:
    state = payload.get("state")
    if isinstance(state, dict):
        return state
    if any(key in payload for key in ("turn", "turn_no", "round", "current_player", "active_player", "players", "board")):
        return payload
    return dict(fallback or {})


def _active_engine_player(state: dict) -> str | None:
    # spicy-monopoly returns ``next_turn`` from /roll and ``turn`` from
    # /state.  Keep the aliases for older/forked engine builds, but only
    # accept strings so numeric turn counters are never mistaken for names.
    for key in ("next_turn", "active_player", "current_player", "current_turn", "turn", "player"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _actor_for_engine_who(room: dict, who: str | None) -> str | None:
    if not who:
        return room.get("active_actor")
    for actor, engine_name in (room.get("_engine_players") or {}).items():
        if engine_name == who:
            return actor
    seats = room.get("seats") or {}
    if who in seats:
        return seats[who]
    for actor, engine_name in ((actor, seat) for seat, actor in seats.items()):
        if actor == who:
            return actor
    return who if who in {"haya", "cc", "codex"} else room.get("active_actor")


def _engine_who_for_actor(room: dict, actor: str) -> str:
    mapped = (room.get("_engine_players") or {}).get(actor)
    if mapped:
        return str(mapped)
    for seat, occupant in (room.get("seats") or {}).items():
        if occupant == actor and seat != "observer":
            return seat
    return actor


def _pending_actor(payload: dict, room: dict, roller: str) -> str:
    candidates = [
        payload.get("actor"), payload.get("who"), payload.get("player"),
        payload.get("task_owner"), payload.get("affected_player"),
    ]
    action_needed = payload.get("action_needed")
    if isinstance(action_needed, dict):
        candidates.extend([action_needed.get("actor"), action_needed.get("who"), action_needed.get("player")])
    for key in ("task", "truth", "duel", "toll", "super_task"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.extend([value.get("actor"), value.get("who"), value.get("player")])
    for candidate in candidates:
        mapped = _actor_for_engine_who(room, str(candidate)) if candidate else None
        if mapped in {"haya", "cc", "codex"}:
            return mapped
    return roller


def _pending_kind(payload: dict) -> str | None:
    action = payload.get("action_needed")
    if isinstance(action, dict):
        action = action.get("type") or action.get("kind") or action.get("action")
    normalized = str(action or "").strip().lower()
    aliases = {
        "duel_result": "duel", "duel": "duel", "toll": "toll", "pay_toll": "toll",
        "super": "super", "super_action": "super", "buyout": "super",
        "task": "task", "truth": "truth",
    }
    if normalized in aliases:
        return aliases[normalized]
    for key, kind in (
        ("duel", "duel"), ("toll", "toll"), ("super_task", "super"),
        ("truth", "truth"), ("task", "task"),
    ):
        value = payload.get(key)
        if value not in (None, False, "", [], {}):
            return kind
    event = payload.get("event") or payload.get("tile_type") or payload.get("type")
    return aliases.get(str(event or "").lower())


def _is_finished(payload: dict) -> bool:
    return bool(
        payload.get("game_over")
        or payload.get("finished")
        or str(payload.get("status") or "").lower() in {"finished", "game_over", "ended"}
    )


def _is_jailed(payload: dict) -> bool:
    return bool(payload.get("jailed") or payload.get("in_jail") or payload.get("jail_turn"))


def _safe_word_used(content: str, *, explicit: bool = False) -> bool:
    if explicit:
        return True
    safe_word = unicodedata.normalize(
        "NFKC", os.environ.get("MONOPOLY_SAFE_WORD", "404").strip()
    )
    if not safe_word:
        return False
    # The word must be a standalone line/message.  "HTTP 404" and room ids
    # containing 404 are ordinary chat and must not pause a game.
    return any(unicodedata.normalize("NFKC", line).strip() == safe_word for line in content.splitlines())


class MonopolyService:
    def __init__(
        self,
        *,
        db_path: str = store.DEFAULT_DB_PATH,
        engine: EngineClient | None = None,
        cipher: TokenCipher | None = None,
        locks: RoomLockRegistry | None = None,
    ):
        self.db_path = db_path
        self.engine = engine or EngineClient(os.environ.get("MONOPOLY_ENGINE_URL", "http://127.0.0.1:8069"))
        self.cipher = cipher or TokenCipher()
        self.locks = locks or RoomLockRegistry()
        store.ensure_schema(db_path)

    def create_room(self, *, mode: str = "two_player_three_chat", seats: dict | None = None) -> dict:
        if mode == "three_player":
            raise RoomError("MODE_NOT_IMPLEMENTED", "三人引擎要等第四阶段 Fork，当前只开双人棋局", status=409)
        if mode != "two_player_three_chat":
            raise RoomError("INVALID_MODE", "不支持的房间模式")
        fixed_seats = {"p1": "haya", "p2": "cc", "observer": "codex"}
        seats = seats or fixed_seats
        if seats != fixed_seats:
            raise RoomError("INVALID_SEATS", "第一阶段席位固定为 haya 对 CC，Codex 观察")
        room_id = uuid.uuid4().hex[:16]
        pair_code = "room-" + room_id
        room = store.create_room(room_id, mode, seats, pair_code, self.db_path)
        with store.transaction(self.db_path) as conn:
            store.append_event(conn, room_id, "room_created", None, {"mode": mode, "seats": seats})
        return self.snapshot(room_id)

    def snapshot(self, room_id: str) -> dict:
        room = store.get_room(room_id, self.db_path)
        if not room:
            raise RoomError("ROOM_NOT_FOUND", "房间不存在", status=404)
        return {
            "room": room,
            "state": room["state"],
            "pending": room["pending"],
            "messages": store.recent_messages(room_id, limit=120, db_path=self.db_path),
        }

    def setup(self, room_id: str, payload: dict, *, expected_seq: int | None = None) -> dict:
        with self.locks.hold(room_id):
            room = self._room(room_id, include_token=True)
            self._check_seq(room, expected_seq)
            if room["status"] not in {RoomStatus.LOBBY.value, RoomStatus.SETUP.value}:
                raise RoomError("ROOM_ALREADY_STARTED", "这个房间已经开局")
            engine_payload = dict(payload)
            engine_payload["pair_code"] = room["pair_code"]
            engine_payload.setdefault("setup_confirmed", True)
            try:
                if not engine_payload.get("rules_ack") and hasattr(self.engine, "help"):
                    help_payload = self.engine.help()
                    if help_payload.get("rules_ack"):
                        engine_payload["rules_ack"] = help_payload["rules_ack"]
                result = self.engine.new_game(engine_payload)
            except EngineValidationError as exc:
                latest = self._record_room_error(room_id, None, exc)
                raise RoomError(exc.code, exc.detail, status=exc.status, latest=latest) from exc
            except EngineError as exc:
                self._mark_engine_down(room_id, room, exc)
                raise RoomError(exc.code, exc.detail, status=exc.status) from exc

            game_id = _extract_game_id(result)
            try:
                raw_token = _extract_token(result)
            except RoomError:
                # A valid upstream response always includes a deletion token.
                # An empty-token cleanup can only remove an unprotected
                # malformed game; a protected game correctly returns 403.
                try:
                    self.engine.delete_game(game_id, "")
                except EngineError:
                    pass
                raise
            try:
                token_cipher = self.cipher.encrypt(raw_token)
            except RoomError:
                # Do not leak an unreachable engine game when local encrypted
                # persistence is misconfigured.
                try:
                    self.engine.delete_game(game_id, raw_token)
                except EngineError:
                    pass
                raise
            try:
                state = self._refresh_state(game_id, result, {})
            except EngineError as exc:
                try:
                    self.engine.delete_game(game_id, raw_token)
                except EngineError:
                    pass
                self._mark_engine_down(room_id, room, exc)
                raise RoomError(exc.code, exc.detail, status=exc.status) from exc
            engine_players = {
                "haya": str(engine_payload.get("p1_name") or "P1"),
                "cc": str(engine_payload.get("p2_name") or "P2"),
            }
            room["_engine_players"] = engine_players
            requested_first = str(engine_payload.get("first_player") or engine_players["haya"])
            active = (
                _actor_for_engine_who(room, _active_engine_player(state))
                or _actor_for_engine_who(room, requested_first)
                or "haya"
            )
            persisted_result = dict(result)
            for secret_key in ("player_token", "token", "delete_token"):
                persisted_result.pop(secret_key, None)
            agent_state = store.get_agent_state(room_id, self.db_path)
            agent_state["engine_players"] = engine_players
            with store.transaction(self.db_path) as conn:
                store.update_room(
                    conn, room_id,
                    status=RoomStatus.IDLE.value,
                    game_id=game_id,
                    player_token_cipher=token_cipher,
                    state_json=json.dumps(state, ensure_ascii=False),
                    pending_json="{}",
                    active_actor=active,
                    agent_state_json=json.dumps(agent_state, ensure_ascii=False),
                )
                store.append_event(conn, room_id, "game_started", active, persisted_result)
                safety = {
                    "active_limits": result.get("active_limits"),
                    "history_note": result.get("history_note"),
                }
                store.append_event(conn, room_id, "setup_confirmed", None, safety)
                store.append_event(conn, room_id, "state", active, state)
            return self.snapshot(room_id)

    def execute(self, room_id: str, intent: dict) -> dict:
        action = str(intent.get("action") or "").strip().lower()
        actor = str(intent.get("actor") or "haya").strip().lower()
        args = intent.get("args") or {}
        if not isinstance(args, dict):
            raise RoomError("INVALID_ARGS", "args 必须是对象")
        expected = intent.get("expected_seq", intent.get("expectedEventSeq"))
        with self.locks.hold(room_id):
            room = self._room(room_id, include_token=True)
            self._check_seq(room, expected)
            self._validate_room_action(room, action, actor)
            if action == "roll":
                return self._roll(room, actor, args)
            if action == "decide":
                if (room.get("pending") or {}).get("kind") == "duel" and actor == "haya":
                    return self._duel_result(room, actor, args)
                return self._decide(room, actor, args)
            if action == "duel_result":
                return self._duel_result(room, actor, args)
            if action in _IMMEDIATE_ACTION_PARAMS:
                return self._immediate(room, action, actor, args)
            raise RoomError("UNKNOWN_ACTION", f"不支持的动作：{action}")

    def _roll(self, room: dict, actor: str, args: dict) -> dict:
        pending = room.get("pending")
        if room["status"] == RoomStatus.DUEL_PENDING.value and not (pending or {}).get("chosen"):
            raise RoomError("DUEL_WINNER_REQUIRED", "必须先选出对决赢家")
        body: dict = {}
        if pending:
            selected = pending.get("chosen") or pending.get("default")
            if not selected:
                raise RoomError("PENDING_DECISION_REQUIRED", "这笔悬账没有默认决定")
            body.update(selected)
        decision = args.get("decision")
        if isinstance(decision, dict):
            body.update(decision)
        for key in ("task", "toll", "super_action", "duel_winner", "guess", "swap_identity"):
            if key in args:
                body[key] = args[key]
        try:
            reply = self.engine.roll(room["game_id"], body)
        except EngineValidationError as exc:
            latest = self._record_room_error(room["id"], actor, exc)
            raise RoomError(exc.code, exc.detail, status=exc.status, latest=latest) from exc
        except EngineError as exc:
            self._mark_engine_down(room["id"], room, exc)
            raise RoomError(exc.code, exc.detail, status=exc.status) from exc

        payload = reply.payload
        if reply.outcome_unknown:
            latest = self._freeze_unknown_roll(room, actor, body, payload)
            raise RoomError(
                "ROLL_OUTCOME_UNKNOWN",
                "掷骰请求已在引擎生效，但响应丢失；/state 无法还原新抽到的事件，棋盘已冻结等待人工对账",
                status=409,
                latest=latest,
            )
        refresh_error = None
        try:
            state = self._refresh_state(room["game_id"], payload, room.get("state") or {})
        except EngineError as exc:
            refresh_error = exc
            state = {**(room.get("state") or {}), **_extract_state(payload)}
        final_payload = self._fetch_final_result(room["game_id"]) if _is_finished(payload) else None
        with store.transaction(self.db_path) as conn:
            if pending:
                store.append_event(conn, room["id"], "settled", actor, {
                    "decision": body, "response": payload, "reconciled": reply.reconciled,
                })
            store.append_event(conn, room["id"], "rolled", actor, payload)
            self._persist_engine_result(conn, room, payload, state, actor)
            if final_payload is not None:
                store.append_event(conn, room["id"], "final_result", actor, final_payload)
        if refresh_error is not None:
            refreshed_room = self._room(room["id"], include_token=True)
            self._mark_engine_down(room["id"], refreshed_room, refresh_error)
            raise RoomError(refresh_error.code, refresh_error.detail, status=refresh_error.status)
        return self.snapshot(room["id"])

    def _freeze_unknown_roll(
        self, room: dict, actor: str, decision: dict, payload: dict
    ) -> int:
        state = _extract_state(payload, room.get("state"))
        active = (
            _actor_for_engine_who(room, _active_engine_player(state))
            or room.get("active_actor")
            or actor
        )
        agent_state = store.get_agent_state(room["id"], self.db_path)
        agent_state["reconciliation_required"] = True
        agent_state["engine_down_from"] = RoomStatus.IDLE.value
        with store.transaction(self.db_path) as conn:
            store.update_room(
                conn,
                room["id"],
                status=RoomStatus.ENGINE_DOWN.value,
                state_json=json.dumps(state, ensure_ascii=False),
                pending_json="{}",
                active_actor=active,
                agent_state_json=json.dumps(agent_state, ensure_ascii=False),
            )
            store.append_event(conn, room["id"], "roll_outcome_unknown", actor, {
                "decision": decision,
                "reconciled_state": state,
                "detail": "roll advanced but /state does not expose the rolled event or pending card",
            })
            store.append_event(conn, room["id"], "pending", actor, {})
            store.append_event(conn, room["id"], "state", active, state)
            event = store.append_event(conn, room["id"], "room_error", actor, {
                "code": "ROLL_OUTCOME_UNKNOWN",
                "detail": "引擎已推进，但新事件无法从 /state 无损恢复；禁止自动重掷或自动解冻",
                "status": 409,
            })
        return int(event["seq"])

    def _decide(self, room: dict, actor: str, args: dict) -> dict:
        pending = room.get("pending")
        if not pending:
            raise RoomError("NO_PENDING_DECISION", "当前没有悬账")
        if pending.get("actor") != actor:
            raise RoomError("NOT_PENDING_ACTOR", "只能由题目所属玩家做决定", status=403)
        kind = pending["kind"]
        chosen: dict
        if kind in {"task", "truth"}:
            value = args.get("task")
            if value not in {"done", "skip"}:
                raise RoomError("INVALID_DECISION", "task 只能是 done 或 skip")
            chosen = {"task": value}
        elif kind == "toll":
            value = args.get("toll")
            if value not in {"pay", "serve"}:
                raise RoomError("INVALID_DECISION", "toll 只能是 pay 或 serve")
            chosen = {"toll": value}
        elif kind == "super":
            value = args.get("super_action")
            if value not in {"done", "buyout"}:
                raise RoomError("INVALID_DECISION", "super_action 只能是 done 或 buyout")
            chosen = {"super_action": value}
        elif kind == "duel":
            winner = str(args.get("duel_winner") or "")
            if winner not in {"haya", "cc"}:
                raise RoomError("INVALID_DECISION", "对决赢家只能是 haya 或 cc")
            chosen = {"duel_winner": _engine_who_for_actor(room, winner)}
        else:
            raise RoomError("INVALID_PENDING_KIND", "未知悬账类型")
        pending["chosen"] = chosen
        with store.transaction(self.db_path) as conn:
            store.update_room(conn, room["id"], pending_json=json.dumps(pending, ensure_ascii=False))
            store.append_event(conn, room["id"], "pending", actor, pending)
        return self.snapshot(room["id"])

    def _duel_result(self, room: dict, actor: str, args: dict) -> dict:
        if room["status"] != RoomStatus.DUEL_PENDING.value:
            raise RoomError("NO_DUEL_PENDING", "当前没有待结算对决")
        winner = str(args.get("winner") or args.get("duel_winner") or "")
        if winner not in {"haya", "cc"}:
            raise RoomError("INVALID_DUEL_WINNER", "对决赢家只能是 haya 或 cc")
        # Keep lazy settlement semantics: selecting the winner only records it.
        return self._decide(room, room["pending"]["actor"], {"duel_winner": winner})

    def _immediate(self, room: dict, action: str, actor: str, args: dict) -> dict:
        engine_who = _engine_who_for_actor(room, actor)
        params = dict(args)
        for name in _IMMEDIATE_ACTION_PARAMS[action]:
            if name == "who":
                params[name] = engine_who
            elif name == "guesser":
                params[name] = engine_who
            elif name not in params:
                raise RoomError("MISSING_ACTION_ARG", f"{action} 缺少 {name}")
        params = {name: params[name] for name in _IMMEDIATE_ACTION_PARAMS[action]}
        try:
            payload = self.engine.action(action, room["game_id"], **params)
        except EngineValidationError as exc:
            latest = self._record_room_error(room["id"], actor, exc)
            raise RoomError(exc.code, exc.detail, status=exc.status, latest=latest) from exc
        except EngineError as exc:
            self._mark_engine_down(room["id"], room, exc)
            raise RoomError(exc.code, exc.detail, status=exc.status) from exc
        refresh_error = None
        try:
            state = self._refresh_state(room["game_id"], payload, room.get("state") or {})
        except EngineError as exc:
            refresh_error = exc
            state = {**(room.get("state") or {}), **_extract_state(payload)}
        final_payload = self._fetch_final_result(room["game_id"]) if _is_finished(payload) else None
        with store.transaction(self.db_path) as conn:
            store.append_event(conn, room["id"], action, actor, payload)
            self._persist_engine_result(
                conn,
                room,
                payload,
                state,
                actor,
                preserve_pending=action not in {"swap", "reroll_task", "extra_task"},
            )
            if final_payload is not None:
                store.append_event(conn, room["id"], "final_result", actor, final_payload)
        if refresh_error is not None:
            refreshed_room = self._room(room["id"], include_token=True)
            self._mark_engine_down(room["id"], refreshed_room, refresh_error)
            raise RoomError(refresh_error.code, refresh_error.detail, status=refresh_error.status)
        return self.snapshot(room["id"])

    def _persist_engine_result(
        self,
        conn,
        room: dict,
        payload: dict,
        state: dict,
        actor: str,
        *,
        preserve_pending: bool = False,
    ) -> None:
        current = dict(room)
        current["state"] = state
        kind = _pending_kind(payload)
        engine_who = _active_engine_player(state) or _active_engine_player(payload)
        active = _actor_for_engine_who(current, engine_who) or room.get("active_actor") or actor
        pending = None
        if kind:
            current_seq = int(conn.execute(
                "SELECT event_seq FROM monopoly_rooms WHERE id=?", (room["id"],)
            ).fetchone()[0])
            pending = PendingDecision(
                kind=kind,
                actor=_pending_actor(payload, current, actor),
                default=_PENDING_DEFAULTS[kind],
                chosen=None,
                created_seq=current_seq + 1,
            ).as_dict()
            status = _PENDING_STATUSES[kind]
        elif preserve_pending:
            pending = room.get("pending")
            status = room["status"]
        elif _is_finished(payload):
            status = RoomStatus.FINISHED.value
        elif _is_jailed(payload):
            status = RoomStatus.JAIL_TURN.value
        else:
            status = RoomStatus.IDLE.value
        store.update_room(
            conn, room["id"],
            status=status,
            state_json=json.dumps(state, ensure_ascii=False),
            pending_json=json.dumps(pending or {}, ensure_ascii=False),
            active_actor=active,
        )
        if kind:
            store.append_event(conn, room["id"], kind + "_drawn", active, payload)
        store.append_event(conn, room["id"], "pending", active, pending or {})
        store.append_event(conn, room["id"], "state", active, state)
        if status == RoomStatus.FINISHED.value:
            store.append_event(conn, room["id"], "game_over", active, payload)

    def _refresh_state(self, game_id: str, event_payload: dict, fallback: dict) -> dict:
        state = dict(self.engine.state(game_id))
        reminder = event_payload.get("identity_reminder") or fallback.get("identity_reminder")
        if reminder is not None:
            state["identity_reminder"] = reminder
        if hasattr(self.engine, "shop"):
            try:
                state["shop"] = self.engine.shop(game_id)
            except EngineError:
                # Older engine builds may not expose /shop.  The canonical
                # board state is still usable; hand details are optional.
                pass
        return state

    def post_message(
        self,
        room_id: str,
        *,
        author: str,
        content: str,
        reply_to: int | None = None,
        safe_word: bool = False,
    ) -> dict:
        room = self._room(room_id)
        content = (content or "").strip()
        if not content:
            raise RoomError("EMPTY_MESSAGE", "消息不能为空")
        if len(content) > 12000:
            raise RoomError("MESSAGE_TOO_LONG", "消息太长")
        message = store.add_message(room_id, author, content, reply_to=reply_to, db_path=self.db_path)
        if _safe_word_used(content, explicit=safe_word):
            with self.locks.hold(room_id):
                latest = self._room(room_id)
                if latest["status"] != RoomStatus.PAUSED.value:
                    agent_state = store.get_agent_state(room_id, self.db_path)
                    agent_state["paused_from"] = latest["status"]
                    store.save_agent_state(room_id, agent_state, self.db_path)
                    with store.transaction(self.db_path) as conn:
                        store.update_room(conn, room_id, status=RoomStatus.PAUSED.value)
                        store.append_event(conn, room_id, "game_paused", author, {
                            "reason": "safe_word", "message_id": message["id"],
                        })
                    store.add_message(room_id, "system", "游戏已停", db_path=self.db_path)
        return {"message": message, "snapshot": self.snapshot(room_id)}

    def pause(self, room_id: str, *, expected_seq: int | None = None, reason: str = "manual") -> dict:
        with self.locks.hold(room_id):
            room = self._room(room_id)
            self._check_seq(room, expected_seq)
            if room["status"] == RoomStatus.PAUSED.value:
                return self.snapshot(room_id)
            agent_state = store.get_agent_state(room_id, self.db_path)
            agent_state["paused_from"] = room["status"]
            store.save_agent_state(room_id, agent_state, self.db_path)
            with store.transaction(self.db_path) as conn:
                store.update_room(conn, room_id, status=RoomStatus.PAUSED.value)
                store.append_event(conn, room_id, "game_paused", None, {"reason": reason})
            return self.snapshot(room_id)

    def resume(self, room_id: str, *, expected_seq: int | None = None) -> dict:
        with self.locks.hold(room_id):
            room = self._room(room_id)
            self._check_seq(room, expected_seq)
            if room["status"] not in {RoomStatus.PAUSED.value, RoomStatus.ENGINE_DOWN.value}:
                return self.snapshot(room_id)
            if room.get("game_id"):
                try:
                    state = self.engine.state(room["game_id"])
                except EngineError as exc:
                    raise RoomError(exc.code, exc.detail, status=exc.status) from exc
            else:
                state = room["state"]
            pending = room.get("pending")
            agent_state = store.get_agent_state(room_id, self.db_path)
            if agent_state.get("reconciliation_required"):
                raise RoomError(
                    "ROLL_OUTCOME_UNKNOWN",
                    "上一次 roll 的响应丢失，当前 /state 无法还原事件或悬账；禁止自动解冻",
                    status=409,
                )
            paused_from = agent_state.pop("paused_from", None)
            engine_down_from = agent_state.pop("engine_down_from", None)
            restored = (
                _PENDING_STATUSES.get((pending or {}).get("kind"))
                or paused_from
                or engine_down_from
                or (RoomStatus.FINISHED.value if _is_finished(state) else None)
                or (RoomStatus.JAIL_TURN.value if _is_jailed(state) else None)
                or RoomStatus.IDLE.value
            )
            if restored in {RoomStatus.PAUSED.value, RoomStatus.ENGINE_DOWN.value}:
                restored = RoomStatus.IDLE.value
            store.save_agent_state(room_id, agent_state, self.db_path)
            with store.transaction(self.db_path) as conn:
                store.update_room(conn, room_id, status=restored, state_json=json.dumps(state, ensure_ascii=False))
                store.append_event(conn, room_id, "game_resumed", None, {"status": restored})
                store.append_event(conn, room_id, "state", room.get("active_actor"), state)
            return self.snapshot(room_id)

    def delete(self, room_id: str) -> None:
        with self.locks.hold(room_id):
            room = self._room(room_id, include_token=True)
            if room.get("game_id") and room.get("player_token_cipher"):
                token = self.cipher.decrypt(room["player_token_cipher"])
                try:
                    self.engine.delete_game(room["game_id"], token)
                except EngineError as exc:
                    raise RoomError(exc.code, exc.detail, status=exc.status) from exc
            store.delete_room(room_id, self.db_path)

    def _mark_engine_down(self, room_id: str, room: dict, exc: EngineError) -> None:
        agent_state = store.get_agent_state(room_id, self.db_path)
        agent_state["engine_down_from"] = room.get("status")
        store.save_agent_state(room_id, agent_state, self.db_path)
        with store.transaction(self.db_path) as conn:
            store.update_room(conn, room_id, status=RoomStatus.ENGINE_DOWN.value)
            store.append_event(conn, room_id, "engine_down", None, {"code": exc.code, "detail": exc.detail})

    def _record_room_error(self, room_id: str, actor: str | None, exc: EngineError) -> int:
        with store.transaction(self.db_path) as conn:
            event = store.append_event(conn, room_id, "room_error", actor, {
                "code": exc.code,
                "detail": exc.detail,
                "status": exc.status,
                "engine_payload": exc.payload,
            })
        return int(event["seq"])

    def _fetch_final_result(self, game_id: str) -> dict:
        try:
            return self.engine.final_result(game_id)
        except EngineError as exc:
            return {"unavailable": True, "code": exc.code, "detail": exc.detail}

    def _room(self, room_id: str, *, include_token: bool = False) -> dict:
        room = store.get_room(room_id, self.db_path, include_token=include_token)
        if not room:
            raise RoomError("ROOM_NOT_FOUND", "房间不存在", status=404)
        room["_engine_players"] = store.get_agent_state(room_id, self.db_path).get("engine_players", {})
        return room

    @staticmethod
    def _check_seq(room: dict, expected: Any) -> None:
        if expected is None:
            return
        try:
            expected_int = int(expected)
        except (TypeError, ValueError) as exc:
            raise RoomError("INVALID_EVENT_SEQ", "expectedEventSeq 必须是整数") from exc
        latest = int(room["event_seq"])
        if expected_int != latest:
            raise RoomError("STALE_ROOM_STATE", "房间状态已经变化", status=409, latest=latest)

    def _validate_room_action(self, room: dict, action: str, actor: str) -> None:
        if actor not in {"haya", "cc", "codex"}:
            raise RoomError("INVALID_ACTOR", "未知行动者")
        if room["status"] in {RoomStatus.LOBBY.value, RoomStatus.SETUP.value}:
            raise RoomError("GAME_NOT_STARTED", "棋局还没开始")
        if room["status"] == RoomStatus.FINISHED.value:
            raise RoomError("GAME_FINISHED", "棋局已经结束")
        if room["status"] == RoomStatus.PAUSED.value:
            raise RoomError("GAME_PAUSED", "棋局已暂停", status=409)
        if room["status"] == RoomStatus.ENGINE_DOWN.value:
            raise RoomError("ENGINE_DOWN", "游戏服务暂时离线，局面已保存", status=503)
        if actor == "codex" and (room.get("seats") or {}).get("observer") == "codex":
            raise RoomError("OBSERVER_CANNOT_ACT", "观察席不能改变棋盘", status=403)
        if action == "roll" and actor != room.get("active_actor"):
            raise RoomError("NOT_YOUR_TURN", "还没轮到这个玩家", status=403)
        if action in {"swap", "reroll_task"} and (room.get("pending") or {}).get("actor") != actor:
            raise RoomError("NOT_PENDING_ACTOR", "只能更换自己的悬账卡", status=403)
        if (
            room["status"] == RoomStatus.DUEL_PENDING.value
            and not (room.get("pending") or {}).get("chosen")
            and action not in {"decide", "duel_result"}
        ):
            raise RoomError("DUEL_WINNER_REQUIRED", "必须先选出对决赢家")

