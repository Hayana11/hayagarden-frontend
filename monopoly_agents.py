"""AI adapters and serialized speaking scheduler for monopoly rooms."""

from __future__ import annotations

import json
import hmac
import os
import re
import threading
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import cc_resident
import monopoly_store as store
from monopoly_rooms import MonopolyService, RoomError, RoomStatus


_INTENT_RE = re.compile(r"\[game_intent\]\s*(\{.*?\})\s*\[/game_intent\]", re.I | re.S)
_REFUSAL_RE = re.compile(
    r"^\s*(?:抱歉[，,。\s]*)?(?:"
    r"我(?:不能|无法)(?:协助|参与|继续|提供)(?:这|该|此)?(?:个)?(?:请求|内容|游戏)|"
    r"I\s+(?:can't|cannot|am\s+unable\s+to)\s+(?:help|assist|comply|continue)(?:\s+with)?\s+(?:this|that)"
    r")",
    re.I | re.S,
)


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


def _persona_parts(value: Any) -> tuple[str, str]:
    if isinstance(value, list):
        texts = [
            str(block.get("text") or "") for block in value
            if isinstance(block, dict) and block.get("text")
        ]
        return (texts[0] if texts else "", "\n\n".join(texts[1:]))
    return str(value or ""), ""


def _event(room_id: str, event_type: str, actor: str | None, payload: dict, db_path: str) -> dict:
    with store.transaction(db_path) as conn:
        return store.append_event(conn, room_id, event_type, actor, payload)


def _live_event(room_id: str, event_type: str, actor: str | None, payload: dict, db_path: str) -> dict:
    return store.append_live_event(room_id, event_type, actor, payload, db_path)


def _parse_output(raw: str) -> tuple[str, dict | None]:
    match = _INTENT_RE.search(raw or "")
    intent = None
    if match:
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                intent = parsed
        except json.JSONDecodeError:
            intent = None
    text = _INTENT_RE.sub("", raw or "").strip()
    return text, intent


def _allowed_actions(snapshot: dict, actor: str) -> list[dict]:
    room = snapshot["room"]
    pending = snapshot.get("pending")
    if room["status"] in {RoomStatus.PAUSED.value, RoomStatus.ENGINE_DOWN.value, RoomStatus.FINISHED.value}:
        return []
    if actor == "codex" and (room.get("seats") or {}).get("observer") == "codex":
        return []
    actions: list[dict] = []
    if pending and pending.get("actor") == actor:
        kind = pending.get("kind")
        decisions = {
            "task": [{"task": "done"}, {"task": "skip"}],
            "truth": [{"task": "done"}, {"task": "skip"}],
            "toll": [{"toll": "pay"}, {"toll": "serve"}],
            "super": [{"super_action": "done"}, {"super_action": "buyout"}],
            "duel": [{"duel_winner": "haya"}, {"duel_winner": "cc"}],
        }
        actions.append({"action": "decide", "args": decisions.get(kind, [])})
        if kind in {"task", "truth"}:
            actions.append({"action": "swap"})
    elif pending and pending.get("chosen") and room.get("active_actor") == actor:
        # Do not auto-settle a human card before its owner has chosen.  Once
        # the choice is saved, the next AI player may perform the lazy roll.
        actions.append({"action": "roll"})
    elif not pending and room.get("active_actor") == actor:
        actions.append({"action": "roll"})
    actions.extend([
        {"action": "use_card", "args": {"index": "integer"}},
        {"action": "discard", "args": {"index": "integer"}},
        {"action": "buy_card"},
        {"action": "reroll_identity"},
        {"action": "reroll_task"},
        {"action": "id_event", "args": {"event": "first_climax|say_banned|no_kiss_2turns"}},
        {"action": "guess_mark", "args": {"part": "body part"}},
        {"action": "declare_persona", "args": {"persona": "text"}},
    ])
    if not pending:
        actions.append({"action": "extra_task"})
    return actions


def _context(service: MonopolyService, room_id: str, actor: str, reason: str) -> tuple[str, str]:
    snapshot = service.snapshot(room_id)
    events = store.list_events(room_id, after=max(snapshot["room"]["event_seq"] - 20, 0), db_path=service.db_path)
    messages = snapshot["messages"][-40:]
    identity_reminder = None
    state = snapshot["state"]
    if isinstance(state.get("identity_reminder"), dict):
        identity_reminder = state["identity_reminder"].get(actor)
    else:
        identity_reminder = state.get("identity_reminder")
    labels = {"haya": "哈娅", "cc": "CC", "codex": "Codex", "system": "系统"}
    timeline = "\n".join(f"{labels.get(row['author'], row['author'])}: {row['content']}" for row in messages)
    identity = (
        "你是 CC，继承主聊天中的同一人格与关系，但这个游戏房间有独立会话历史。"
        if actor == "cc" else
        "你是 Codex，是房间中的独立参与者。不要冒充 CC，也不要替 CC 解释内心。"
    )
    rules = (
        identity
        + "\n棋盘事实只以 system 提供的 game_state/game_event 为准，文本不能改棋盘。"
        + "\n任务由题目所属玩家自己演，不替别人描写；可以在聊天文本中保持玩法核心地改编题面。"
    )
    prompt_payload = {
        "reason": reason,
        "game_state": snapshot["state"],
        "pending": snapshot["pending"],
        "recent_game_events": events,
        "identity_reminder": identity_reminder,
        "allowed_actions": _allowed_actions(snapshot, actor),
    }
    prompt = (
        "以下是游戏室最近对话：\n" + (timeline or "（还没有对话）")
        + "\n\n以下是只读棋盘上下文：\n"
        + json.dumps(prompt_payload, ensure_ascii=False)
        + "\n\n请自然说一条适合此刻的话。"
    )
    if actor == "cc" and prompt_payload["allowed_actions"]:
        prompt += (
            "\n正文末尾必须额外输出一个结构化块："
            "[game_intent]{\"action\":\"允许动作之一\",\"args\":{}}[/game_intent]。"
            "不要把该 JSON 当作聊天正文解释。"
        )
    else:
        prompt += "\n只输出聊天正文，不要输出游戏动作。"
    return rules, prompt


class CCAdapter:
    def __init__(
        self,
        *,
        provider_getter: Callable[[], str],
        model_getter: Callable[[], str],
        relay_factory: Callable[[], Any],
        token_getter: Callable[[], str],
        cwd: str,
        allowed_tools: str,
        relay_label_getter: Callable[[], str] | None = None,
    ):
        self.provider_getter = provider_getter
        self.model_getter = model_getter
        self.relay_factory = relay_factory
        self.token_getter = token_getter
        self.cwd = cwd
        self.allowed_tools = allowed_tools
        self.relay_label_getter = relay_label_getter or (lambda: "")
        self._sessions: dict[str, cc_resident.ResidentSession] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        provider = self.provider_getter()
        if provider == "claude_code":
            return {"kind": "claude_code", "label": "Claude Code", "model": self.model_getter() or "account default"}
        relay = self.relay_factory()
        host = (urlparse(str(getattr(relay, "api_url", "") or "")).hostname or "").lower()
        official = provider in {"official", "api_official", "anthropic_official"} or host == "api.anthropic.com"
        if official:
            return {"kind": "official", "label": "Anthropic Official", "model": relay.model or self.model_getter() or ""}
        label = (self.relay_label_getter() or "").strip() or host or "当前中转"
        return {"kind": "relay", "label": label, "model": relay.model or self.model_getter() or ""}

    def stream(self, room_id: str, system: str, prompt: str) -> Iterable[str]:
        provider = self.provider_getter()
        if provider == "claude_code":
            token = self.token_getter()
            if not token:
                raise RuntimeError("CC 官方登录令牌未配置")
            with self._lock:
                session = self._sessions.get(room_id)
                if session is None:
                    session = cc_resident.ResidentSession(
                        self.cwd, self.allowed_tools, os.path.join(self.cwd, "cc-tools.json")
                    )
                    self._sessions[room_id] = session
            env = dict(os.environ)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env.pop("ANTHROPIC_API_KEY", None)
            os.makedirs(self.cwd, exist_ok=True)
            session.ensure_alive(system, env)
            for event, data in session.send_turn(prompt):
                if event == "text":
                    yield str(data)
            return

        if provider not in {"api_relay", "official", "api_official", "anthropic_official"}:
            raise RuntimeError(f"不支持的 CC provider：{provider}")

        relay = self.relay_factory()
        payload = {
            "model": self.model_getter(),
            "max_tokens": 1200,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = relay.call_stream(payload, timeout=180)
        for raw in response:
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            for part in line.splitlines():
                part = part.strip()
                if not part.startswith("data:"):
                    continue
                data = part[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    item = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = item.get("delta") or {}
                text = delta.get("text") if isinstance(delta, dict) else ""
                if not text:
                    choices = item.get("choices") or []
                    if choices:
                        content = (choices[0].get("delta") or {}).get("content")
                        text = content if isinstance(content, str) else ""
                if text:
                    yield text


class CodexAdapter:
    def __init__(self, client: Any):
        self.client = client

    @staticmethod
    def snapshot() -> dict:
        return {"kind": "official", "label": "OpenAI Official", "model": "Codex"}

    def stream(self, thread_id: str | None, system: str, prompt: str):
        return self.client.stream_bound_turn(thread_id, system, prompt)


class MonopolyAgentScheduler:
    def __init__(self, service: MonopolyService, *, persona_builder, cc: CCAdapter, codex: CodexAdapter):
        self.service = service
        self.persona_builder = persona_builder
        self.cc = cc
        self.codex = codex

    def dispatch(self, room_id: str, *, reason: str, targets: list[str], reply_to: int | None = None) -> bool:
        with self.service.locks.hold(room_id):
            snapshot = self.service.snapshot(room_id)
            if snapshot["room"]["status"] == RoomStatus.PAUSED.value:
                return False
            agent_state = store.get_agent_state(room_id, self.service.db_path)
            started = float(agent_state.get("generating_since") or 0)
            if started and time.time() - started < 600:
                return False
            store.patch_agent_state(
                room_id,
                {"generating_since": time.time()},
                db_path=self.service.db_path,
            )

        try:
            ordered = [actor for actor in targets if actor in {"cc", "codex"}]
            current = snapshot["room"].get("active_actor")
            if current in ordered:
                ordered.remove(current)
                ordered.insert(0, current)
            # At most one line per actor per dispatch; this is the hard budget.
            for actor in ordered[:2]:
                self._generate_one(room_id, actor, reason, reply_to)
            return True
        finally:
            with self.service.locks.hold(room_id):
                store.patch_agent_state(
                    room_id,
                    remove=("generating_since",),
                    db_path=self.service.db_path,
                )

    def _generate_one(
        self,
        room_id: str,
        actor: str,
        reason: str,
        reply_to: int | None,
        *,
        retry_after_swap: bool = True,
        followup_budget: int = 1,
    ) -> None:
        snapshot = self.service.snapshot(room_id)
        if snapshot["room"]["status"] == RoomStatus.PAUSED.value:
            return
        system_rules, prompt = _context(self.service, room_id, actor, reason)
        static_persona, dynamic_persona = _persona_parts(self.persona_builder())
        system = static_persona + "\n\n" + system_rules
        if dynamic_persona:
            prompt = "【当前共享状态】\n" + dynamic_persona + "\n\n" + prompt
        _live_event(room_id, "chat_start", actor, {"reason": reason}, self.service.db_path)
        chunks: list[str] = []
        delta_buffer: list[str] = []
        last_delta_flush = time.monotonic()
        provider_meta: dict = {}

        def collect_delta(chunk: str, *, force: bool = False) -> None:
            nonlocal last_delta_flush
            if chunk:
                chunks.append(chunk)
                delta_buffer.append(chunk)
            now = time.monotonic()
            buffered = "".join(delta_buffer)
            if buffered and (force or len(buffered) >= 256 or now - last_delta_flush >= 0.08):
                _live_event(room_id, "chat_delta", actor, {"delta": buffered}, self.service.db_path)
                delta_buffer.clear()
                last_delta_flush = now

        try:
            if actor == "cc":
                provider_meta = self.cc.snapshot()
                agent_state = store.get_agent_state(room_id, self.service.db_path)
                previous = agent_state.get("cc_provider")
                current = [provider_meta.get("kind"), provider_meta.get("label"), provider_meta.get("model")]
                if previous and previous != current:
                    store.add_message(
                        room_id, "system",
                        "CC 已切换至：" + " · ".join(str(value) for value in current[1:] if value),
                        db_path=self.service.db_path,
                    )
                store.patch_agent_state(
                    room_id,
                    {"cc_provider": current},
                    db_path=self.service.db_path,
                )
                for chunk in self.cc.stream(room_id, system, prompt):
                    collect_delta(str(chunk))
            else:
                state = store.get_agent_state(room_id, self.service.db_path)
                thread_id = state.get("codex_thread_id")
                provider_meta = self.codex.snapshot()
                for event, data in self.codex.stream(thread_id, system, prompt):
                    if event == "text":
                        collect_delta(str(data))
                    elif event == "done":
                        store.patch_agent_state(
                            room_id,
                            {"codex_thread_id": data.get("thread_id")},
                            db_path=self.service.db_path,
                        )
            collect_delta("", force=True)
            raw = "".join(chunks).strip()
            text, intent = _parse_output(raw)
            if not text:
                raise RuntimeError("模型没有返回聊天正文")
            if actor == "cc" and _REFUSAL_RE.search(text):
                _live_event(room_id, "chat_done", actor, {"discarded": True, "reason": "content_fallback"}, self.service.db_path)
                swapped = self._cc_content_fallback(room_id, allow_swap=retry_after_swap)
                if swapped and retry_after_swap:
                    self._generate_one(
                        room_id, actor, "turn", reply_to,
                        retry_after_swap=False, followup_budget=followup_budget,
                    )
                return
            if self.service.snapshot(room_id)["room"]["status"] == RoomStatus.PAUSED.value:
                _live_event(
                    room_id,
                    "chat_done",
                    actor,
                    {"discarded": True, "reason": "game_paused"},
                    self.service.db_path,
                )
                return
            saved = self.service.post_message(room_id, author=actor, content=text, reply_to=reply_to)
            message = saved["message"]
            # post_message intentionally owns the 404 pause check.  Provider
            # metadata is attached afterward without routing text around it.
            with store.transaction(self.service.db_path) as conn:
                conn.execute(
                    "UPDATE monopoly_room_messages SET provider_meta=? WHERE id=?",
                    (json.dumps(provider_meta, ensure_ascii=False), message["id"]),
                )
            _live_event(room_id, "chat_done", actor, {"message_id": message["id"]}, self.service.db_path)
            if actor == "cc" and saved["snapshot"]["room"]["status"] != RoomStatus.PAUSED.value:
                needs_followup = self._apply_cc_intent(room_id, intent)
                if needs_followup and followup_budget > 0:
                    self._generate_one(
                        room_id, actor, "turn", reply_to,
                        retry_after_swap=retry_after_swap,
                        followup_budget=followup_budget - 1,
                    )
        except RoomError as exc:
            collect_delta("", force=True)
            reason = "game_paused" if exc.code == "GAME_PAUSED" else "generation_failed"
            _live_event(
                room_id,
                "chat_done",
                actor,
                {"discarded": True, "reason": reason},
                self.service.db_path,
            )
        except Exception as exc:
            collect_delta("", force=True)
            _live_event(room_id, "chat_done", actor, {"discarded": True, "reason": "generation_failed"}, self.service.db_path)
            if actor == "codex":
                _live_event(room_id, "agent_status", actor, {
                    "ready": False, "detail": "等待官方线路登录",
                }, self.service.db_path)
            if actor == "cc":
                _live_event(room_id, "agent_status", actor, {
                    "ready": False, "detail": "CC 线路暂时不可用，可稍后重试",
                }, self.service.db_path)
        finally:
            store.prune_live_events(room_id, db_path=self.service.db_path)

    def _apply_cc_intent(self, room_id: str, intent: dict | None) -> bool:
        snapshot = self.service.snapshot(room_id)
        if snapshot["room"].get("active_actor") != "cc":
            if not (snapshot.get("pending") or {}).get("actor") == "cc":
                return False
        allowed = {item["action"] for item in _allowed_actions(snapshot, "cc")}
        action = str((intent or {}).get("action") or "")
        args = (intent or {}).get("args") or {}
        if action not in allowed:
            if "decide" in allowed and snapshot.get("pending"):
                action = "decide"
                args = dict(snapshot["pending"].get("default") or {})
            else:
                action = "roll" if "roll" in allowed else ""
        if not action:
            return False
        if action == "roll" and isinstance((intent or {}).get("decision"), dict):
            args = {**args, "decision": intent["decision"]}
        result = self.service.execute(room_id, {
            "actor": "cc", "action": action, "args": args,
            "expected_seq": self.service.snapshot(room_id)["room"]["event_seq"],
        })
        pending = result.get("pending") or {}
        return action == "roll" and pending.get("actor") == "cc"

    def _cc_content_fallback(self, room_id: str, *, allow_swap: bool) -> bool:
        snapshot = self.service.snapshot(room_id)
        room, pending, state = snapshot["room"], snapshot.get("pending"), snapshot["state"]
        if room.get("active_actor") != "cc":
            return False
        if pending and pending.get("actor") == "cc" and pending.get("kind") in {"task", "truth"}:
            remaining = state.get("swap_remaining")
            players = state.get("players") or {}
            if remaining is None and isinstance(players, dict):
                p2 = players.get("p2") or players.get("cc") or {}
                if isinstance(p2, dict):
                    remaining = p2.get("swap_remaining", p2.get("swaps_left"))
            if allow_swap and isinstance(remaining, (int, float)) and remaining > 0:
                try:
                    self.service.execute(room_id, {
                        "actor": "cc", "action": "swap",
                        "expected_seq": room["event_seq"],
                    })
                    store.add_message(room_id, "system", "CC 换了一张卡", db_path=self.service.db_path)
                    return True
                except RoomError:
                    pass
            self.service.execute(room_id, {
                "actor": "cc", "action": "roll", "args": {"decision": {"task": "skip"}},
                "expected_seq": self.service.snapshot(room_id)["room"]["event_seq"],
            })
            store.add_message(room_id, "system", "CC 跳过了这张卡", db_path=self.service.db_path)
            return False
        if room.get("active_actor") == "cc" and room["status"] in {RoomStatus.IDLE.value, RoomStatus.JAIL_TURN.value}:
            self.service.execute(room_id, {
                "actor": "cc", "action": "roll", "expected_seq": room["event_seq"],
            })
        return False


def create_monopoly_agent_blueprint(scheduler: MonopolyAgentScheduler, *, internal_token: str | None = None):
    from flask import Blueprint, jsonify, request

    bp = Blueprint("monopoly_agents", __name__)
    expected_token = internal_token if internal_token is not None else _load_internal_token()

    @bp.post("/monopoly/generate")
    def generate():
        supplied = request.headers.get("X-Monopoly-Internal-Token", "")
        if not expected_token:
            return jsonify({"ok": False, "error": "MONOPOLY_INTERNAL_TOKEN is not configured"}), 503
        if not hmac.compare_digest(supplied, expected_token):
            return jsonify({"ok": False, "error": "forbidden"}), 403
        data = request.get_json(silent=True) or {}
        room_id = str(data.get("room_id") or "")
        targets = data.get("targets") if isinstance(data.get("targets"), list) else ["cc", "codex"]

        def run() -> None:
            try:
                scheduler.dispatch(
                    room_id,
                    reason=str(data.get("reason") or "turn"),
                    targets=targets,
                    reply_to=data.get("reply_to"),
                )
            except Exception as exc:
                try:
                    _event(room_id, "room_error", None, {
                        "code": "AGENT_DISPATCH_FAILED", "detail": str(exc),
                    }, scheduler.service.db_path)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True, name=f"monopoly-agents-{room_id[:8]}").start()
        return jsonify({"ok": True}), 202

    return bp

