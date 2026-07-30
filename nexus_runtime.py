"""In-process Nexus runtime: serial turns, interrupt, event buffer."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional

from nexus_adapters import (
    CLAUDE_HARD_CONFINEMENT_AVAILABLE,
    BaseNexusAdapter,
    ClaudeNexusAdapter,
    CodexNexusAdapter,
)
from nexus_events import is_terminal, make_event, utc_now_iso
from nexus_git import git_summary
from nexus_paths import NexusPathError, resolve_nexus_workspace

TURN_HISTORY_LIMIT = 50
EVENT_BUFFER_LIMIT = 500
SUBSCRIBER_LIMIT = 16
VALID_AGENTS = frozenset({"claude", "codex"})
_DROP_ON_INTERRUPT = frozenset({"text", "think", "tool_use", "tool_result", "git"})

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_INTERRUPTING = "interrupting"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_INTERRUPTED = "interrupted"

CLAUDE_BLOCKED_DETAIL = (
    "ENVIRONMENT_BLOCKED: no reusable hard workspace confinement for Claude Code; "
    "cwd/prompt are not isolation"
)


class NexusBusyError(RuntimeError):
    code = "nexus_busy"


class NexusTurnError(RuntimeError):
    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status


@dataclass
class TurnRecord:
    turn_id: str
    agent: str
    instruction: str
    state: str
    created_at: str
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    events: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER_LIMIT))
    sequence: int = 0
    terminal: bool = False
    interrupt_requested: bool = False
    cond: threading.Condition = field(default_factory=threading.Condition)


class NexusRuntime:
    """Process-memory-only runtime. Restart forgets all turns."""

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        workspace_override: str | None = None,
        adapters: Dict[str, BaseNexusAdapter] | None = None,
        context_usage_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._workspace_override = workspace_override
        if workspace is not None:
            self._workspace = workspace
        else:
            self._workspace = None
        self._adapters = adapters
        self._context_usage_getter = context_usage_getter
        self._state = STATE_IDLE
        self._active_turn_id: Optional[str] = None
        self._turns: Dict[str, TurnRecord] = {}
        self._history: Deque[str] = deque(maxlen=TURN_HISTORY_LIMIT)
        self._subscriber_counts: Dict[str, int] = {}

    @property
    def workspace(self) -> Path:
        if self._workspace is None:
            self._workspace = resolve_nexus_workspace(override=self._workspace_override)
        return self._workspace

    def try_workspace(self) -> tuple[Optional[Path], Optional[str]]:
        try:
            return self.workspace, None
        except NexusPathError as exc:
            return None, exc.code

    def _ensure_adapters(self) -> Dict[str, BaseNexusAdapter]:
        if self._adapters is None:
            ws = self.workspace
            self._adapters = {
                "claude": ClaudeNexusAdapter(ws),
                "codex": CodexNexusAdapter(ws),
            }
        return self._adapters

    def get_adapter(self, agent: str) -> BaseNexusAdapter:
        adapters = self._ensure_adapters()
        if agent not in adapters:
            raise NexusTurnError("unknown_agent", f"unknown agent: {agent}", 400)
        return adapters[agent]

    def _claude_available(self, adapter: BaseNexusAdapter | None = None) -> bool:
        if adapter is not None:
            return bool(getattr(adapter, "hard_workspace_confinement", False))
        if self._adapters and "claude" in self._adapters:
            return bool(getattr(self._adapters["claude"], "hard_workspace_confinement", False))
        return bool(CLAUDE_HARD_CONFINEMENT_AVAILABLE)

    def _capabilities(self, *, claude_available: bool) -> dict[str, Any]:
        return {
            "agents": ["claude", "codex"],
            "agent_availability": {
                "claude": {
                    "available": bool(claude_available),
                    "reason": None if claude_available else "ENVIRONMENT_BLOCKED",
                    "detail": None if claude_available else CLAUDE_BLOCKED_DETAIL,
                },
                "codex": {
                    "available": True,
                    "sandbox": "workspace-write",
                },
            },
            "interrupt": True,
            "sse": True,
            "last_event_id": False,
            "clear": False,
            "rewind": False,
            "push": False,
            "merge": False,
            "deploy": False,
            "shell": False,
            "busy_http_status": 423,
            "instruction_max_chars": 8000,
            "turns_history_limit": TURN_HISTORY_LIMIT,
        }

    def status(self) -> dict[str, Any]:
        """Always return UI-readable JSON; degrade when workspace is missing."""
        with self._lock:
            active = self._active_turn_id
            active_agent = self._turns[active].agent if active and active in self._turns else None
            runtime_state = (
                self._state
                if self._state in {STATE_IDLE, STATE_RUNNING, STATE_INTERRUPTING}
                else (STATE_IDLE if self._active_turn_id is None else self._state)
            )

        ws, ws_error = self.try_workspace()
        workspace_ok = ws is not None
        git = None
        git_error = None
        if ws is not None:
            try:
                git = git_summary(ws)
            except NexusPathError as exc:
                git = None
                git_error = exc.code

        sessions = {
            "claude": {"exists": False, "session_id": None},
            "codex": {"exists": False, "session_id": None},
        }
        claude_available = False
        if workspace_ok:
            try:
                with self._lock:
                    adapters = self._ensure_adapters()
                    sessions = {
                        "claude": {
                            "exists": adapters["claude"].has_session,
                            "session_id": adapters["claude"].session_id,
                        },
                        "codex": {
                            "exists": adapters["codex"].has_session,
                            "session_id": adapters["codex"].session_id,
                        },
                    }
                    claude_available = self._claude_available(adapters["claude"])
            except Exception:
                # Never let adapter construction break status JSON.
                claude_available = False
        else:
            claude_available = False

        usage = None
        if self._context_usage_getter:
            try:
                usage = self._context_usage_getter()
            except Exception:
                usage = None

        return {
            "runtime_state": runtime_state,
            "active_turn_id": active,
            "active_agent": active_agent,
            "sessions": sessions,
            "context_usage": usage,
            "workspace": {
                "root": str(ws) if workspace_ok else None,
                "ok": workspace_ok,
                "error": ws_error,
                "git_error": git_error,
            },
            "git": git,
            "capabilities": self._capabilities(claude_available=claude_available),
        }

    def list_turns(self) -> list[dict[str, Any]]:
        with self._lock:
            out: List[dict[str, Any]] = []
            for turn_id in list(self._history)[-TURN_HISTORY_LIMIT:]:
                turn = self._turns.get(turn_id)
                if not turn:
                    continue
                out.append(
                    {
                        "turn_id": turn.turn_id,
                        "agent": turn.agent,
                        "state": turn.state,
                        "created_at": turn.created_at,
                        "finished_at": turn.finished_at,
                        "error_code": turn.error_code,
                        "instruction_chars": len(turn.instruction),
                    }
                )
            return list(reversed(out))

    def start_turn(self, agent: str, instruction: str) -> dict[str, Any]:
        agent = str(agent or "").strip()
        if agent not in VALID_AGENTS:
            raise NexusTurnError("invalid_agent", "agent must be claude or codex", 400)

        # Fail-closed workspace before accepting.
        try:
            _ = self.workspace
        except NexusPathError as exc:
            raise NexusTurnError(exc.code, exc.detail, 503) from exc

        with self._lock:
            if self._active_turn_id is not None or self._state in {STATE_RUNNING, STATE_INTERRUPTING}:
                raise NexusBusyError("runtime is processing another turn")

            adapter = self.get_adapter(agent)
            if agent == "claude" and not self._claude_available(adapter):
                raise NexusTurnError("claude_unavailable", CLAUDE_BLOCKED_DETAIL, 503)

            turn_id = uuid.uuid4().hex
            turn = TurnRecord(
                turn_id=turn_id,
                agent=agent,
                instruction=instruction,
                state=STATE_RUNNING,
                created_at=utc_now_iso(),
            )
            self._turns[turn_id] = turn
            self._history.append(turn_id)
            self._active_turn_id = turn_id
            self._state = STATE_RUNNING
            self._trim_turns_locked()

            worker = threading.Thread(
                target=self._run_turn,
                args=(turn_id,),
                name=f"nexus-turn-{turn_id[:8]}",
                daemon=True,
            )
            worker.start()
            return {
                "turn_id": turn_id,
                "accepted": True,
                "events_url": f"/api/nexus/turn/{turn_id}/events",
            }

    def _trim_turns_locked(self) -> None:
        keep = set(self._history)
        if self._active_turn_id:
            keep.add(self._active_turn_id)
        for turn_id in list(self._turns.keys()):
            if turn_id not in keep:
                if self._subscriber_counts.get(turn_id, 0) <= 0:
                    self._turns.pop(turn_id, None)
                    self._subscriber_counts.pop(turn_id, None)

    def _emit(self, turn: TurnRecord, event: str, data: Any) -> None:
        with turn.cond:
            if turn.terminal:
                return
            turn.sequence += 1
            payload = make_event(
                event=event,
                turn_id=turn.turn_id,
                agent=turn.agent,
                sequence=turn.sequence,
                data=data if isinstance(data, dict) else {"value": data},
            )
            turn.events.append(payload)
            if is_terminal(payload["event"]):
                turn.terminal = True
                if payload["event"] == "done":
                    turn.state = STATE_DONE
                elif (payload.get("data") or {}).get("code") == "interrupted":
                    turn.state = STATE_INTERRUPTED
                    turn.error_code = "interrupted"
                else:
                    turn.state = STATE_ERROR
                    turn.error_code = str((payload.get("data") or {}).get("code") or "error")
                turn.finished_at = payload["timestamp"]
            turn.cond.notify_all()

    def _run_turn(self, turn_id: str) -> None:
        with self._lock:
            turn = self._turns.get(turn_id)
            adapter = self.get_adapter(turn.agent) if turn else None
        if not turn or not adapter:
            return
        try:
            self._emit(turn, "meta", {"phase": "accepted"})
            for event, data in adapter.stream_turn(turn.instruction):
                with self._lock:
                    interrupting = (
                        turn.interrupt_requested
                        or turn.state == STATE_INTERRUPTING
                        or self._state == STATE_INTERRUPTING
                    )
                if turn.terminal:
                    break
                if interrupting:
                    if event in _DROP_ON_INTERRUPT:
                        continue
                    if event == "done":
                        # Competitive provider completion cannot mark interrupted turn done.
                        self._emit(turn, "status", {"phase": "interrupted"})
                        self._emit(
                            turn,
                            "err",
                            {"code": "interrupted", "message": "turn interrupted"},
                        )
                        break
                    if event == "err":
                        payload = dict(data) if isinstance(data, dict) else {"value": data}
                        payload["code"] = "interrupted"
                        payload.setdefault("message", "turn interrupted")
                        self._emit(turn, "err", payload)
                        break
                    if event == "status":
                        self._emit(turn, event, data)
                        continue
                    continue
                self._emit(turn, event, data)
                if turn.terminal:
                    break
            if not turn.terminal:
                if turn.interrupt_requested or turn.state == STATE_INTERRUPTING:
                    self._emit(turn, "status", {"phase": "interrupted"})
                    self._emit(
                        turn,
                        "err",
                        {"code": "interrupted", "message": "turn interrupted"},
                    )
                else:
                    self._emit(
                        turn,
                        "err",
                        {
                            "code": "missing_terminal",
                            "message": "adapter ended without terminal event",
                        },
                    )
        except Exception as exc:
            if not turn.terminal:
                if turn.interrupt_requested or turn.state == STATE_INTERRUPTING:
                    self._emit(turn, "status", {"phase": "interrupted"})
                    self._emit(
                        turn,
                        "err",
                        {"code": "interrupted", "message": "turn interrupted"},
                    )
                else:
                    self._emit(turn, "err", {"code": "runtime_error", "message": str(exc)[:500]})
        finally:
            with self._lock:
                if self._active_turn_id == turn_id:
                    self._active_turn_id = None
                    self._state = STATE_IDLE

    def interrupt(self, turn_id: str) -> dict[str, Any]:
        adapter: BaseNexusAdapter | None = None
        with self._lock:
            turn = self._turns.get(turn_id)
            if not turn:
                raise NexusTurnError("turn_not_found", "turn not found", 404)
            if self._active_turn_id != turn_id:
                return {
                    "ok": True,
                    "turn_id": turn_id,
                    "interrupted": False,
                    "state": turn.state,
                    "detail": "not_active",
                }
            self._state = STATE_INTERRUPTING
            turn.state = STATE_INTERRUPTING
            turn.interrupt_requested = True
            adapter = self.get_adapter(turn.agent)
            result = {
                "ok": True,
                "turn_id": turn_id,
                "interrupted": True,
                "state": turn.state,
                "detail": "interrupt_requested",
            }
        # Never hold the runtime lock across a potentially blocking adapter interrupt.
        if adapter is not None:
            adapter.request_interrupt()
        return result

    def reserve_event_subscription(self, turn_id: str) -> TurnRecord:
        """Synchronously validate turn + subscriber quota before SSE Response."""
        with self._lock:
            turn = self._turns.get(turn_id)
            if not turn:
                raise NexusTurnError("turn_not_found", "turn not found", 404)
            count = self._subscriber_counts.get(turn_id, 0)
            if count >= SUBSCRIBER_LIMIT:
                raise NexusTurnError("too_many_subscribers", "subscriber limit reached", 429)
            self._subscriber_counts[turn_id] = count + 1
            return turn

    def release_event_subscription(self, turn_id: str) -> None:
        with self._lock:
            self._subscriber_counts[turn_id] = max(0, self._subscriber_counts.get(turn_id, 1) - 1)
            self._trim_turns_locked()

    def iter_events(self, turn_id: str, *, after_sequence: int = 0) -> Iterator[dict[str, Any]]:
        """Reserve then stream. Prefer reserve_event_subscription + iter_events_for_turn for HTTP."""
        turn = self.reserve_event_subscription(turn_id)
        try:
            yield from self.iter_events_for_turn(turn, after_sequence=after_sequence)
        finally:
            self.release_event_subscription(turn_id)

    def iter_events_for_turn(
        self, turn: TurnRecord, *, after_sequence: int = 0
    ) -> Iterator[dict[str, Any]]:
        last = after_sequence
        while True:
            with turn.cond:
                while True:
                    pending = [e for e in list(turn.events) if e["sequence"] > last]
                    terminal = turn.terminal
                    if pending or terminal:
                        break
                    turn.cond.wait(timeout=15)
                batch = list(pending)
                finished = terminal and not batch
            # Yield outside the condition lock so slow subscribers cannot block _emit.
            if finished:
                return
            for event in batch:
                last = event["sequence"]
                yield event
                if is_terminal(event["event"]):
                    return
