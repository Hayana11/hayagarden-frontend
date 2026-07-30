"""Nexus Claude / Codex adapters and test fakes.

Live adapters own Nexus-only processes/sessions. They never touch the formal
Claude resident or the global Codex app-server singleton.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from nexus_events import normalize_event_name, redact_value
from nexus_git import git_summary

EventTuple = tuple[str, dict[str, Any]]


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class BaseNexusAdapter:
    agent: str = ""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.session_id: Optional[str] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    @property
    def has_session(self) -> bool:
        return bool(self.session_id)

    def request_interrupt(self) -> None:
        self._cancel.set()

    def clear_interrupt(self) -> None:
        self._cancel.clear()

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        raise NotImplementedError


def _parse_claude_stream_line(line: str) -> Optional[EventTuple]:
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return "status", {"raw_kind": "non_json"}

    if not isinstance(d, dict):
        return "status", {"raw_kind": "non_object"}

    typ = d.get("type")
    if typ == "system" and d.get("subtype") == "init":
        sid = d.get("session_id") or (d.get("session") or {}).get("id")
        return "meta", {"session_id": sid, "phase": "init"}
    if typ == "rate_limit_event":
        return "status", {"phase": "rate_limit"}
    if typ == "stream_event":
        event = d.get("event") or {}
        et = event.get("type")
        delta = event.get("delta") or {}
        if et == "content_block_delta":
            dt = delta.get("type")
            if dt == "text_delta":
                return "text", {"text": str(delta.get("text") or "")}
            if dt == "thinking_delta":
                return "think", {"text": str(delta.get("thinking") or delta.get("text") or "")}
        return "status", {"raw_kind": et or "stream_event"}
    if typ == "assistant":
        message = d.get("message") or {}
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                return "tool_use", {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": redact_value(block.get("input") or {}),
                }
            if block.get("type") == "text" and block.get("text"):
                return "text", {"text": str(block.get("text"))}
            if block.get("type") == "thinking" and block.get("thinking"):
                return "think", {"text": str(block.get("thinking"))}
        return "status", {"raw_kind": "assistant"}
    if typ == "user":
        message = d.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return "tool_result", {
                    "tool_use_id": block.get("tool_use_id"),
                    "content": redact_value(block.get("content")),
                    "is_error": bool(block.get("is_error")),
                }
        return "status", {"raw_kind": "user"}
    if typ == "result":
        if d.get("is_error"):
            return "err", {"code": "claude_result_error", "message": str(d.get("result") or "error")[:500]}
        sid = d.get("session_id")
        return "meta", {"session_id": sid, "phase": "result"}
    return "status", {"raw_kind": str(typ or "unknown")}


class ClaudeNexusAdapter(BaseNexusAdapter):
    """Independent Claude Code process for Nexus only."""

    agent = "claude"

    def __init__(self, workspace: Path, *, claude_bin: str | None = None) -> None:
        super().__init__(workspace)
        self._claude_bin = claude_bin or shutil.which("claude") or "claude"
        self._proc: subprocess.Popen | None = None

    def request_interrupt(self) -> None:
        super().request_interrupt()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except Exception:
                    pass

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        self.clear_interrupt()
        args = [
            self._claude_bin,
            "-p",
            instruction,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self.session_id:
            args.extend(["--resume", self.session_id])

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        # Do not forward arbitrary caller env; keep a minimal safe subset.
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TERM"):
            if key in os.environ:
                env[key] = os.environ[key]

        yield "meta", {"phase": "start", "agent": "claude"}
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.workspace),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise AdapterError("claude_spawn_failed", str(exc)) from exc

        assert self._proc.stdout is not None
        saw_terminal = False
        try:
            for raw in self._proc.stdout:
                if self._cancel.is_set():
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": "interrupted", "message": "turn interrupted"}
                    saw_terminal = True
                    return
                line = raw.strip()
                if not line:
                    continue
                parsed = _parse_claude_stream_line(line)
                if not parsed:
                    continue
                event, data = parsed
                if event == "meta" and data.get("session_id"):
                    self.session_id = str(data["session_id"])
                if event == "err":
                    yield event, data
                    saw_terminal = True
                    return
                yield event, data
            rc = self._proc.wait(timeout=5)
            if self._cancel.is_set():
                yield "status", {"phase": "interrupted"}
                yield "err", {"code": "interrupted", "message": "turn interrupted"}
                return
            if rc != 0 and not saw_terminal:
                err = ""
                if self._proc.stderr:
                    err = (self._proc.stderr.read() or "")[:300]
                yield "err", {"code": "claude_exit", "message": err or f"exit {rc}"}
                return
            summary = git_summary(self.workspace)
            yield "git", summary
            yield "done", {"ok": True, "session_id": self.session_id}
        except AdapterError:
            raise
        except Exception as exc:
            if self._cancel.is_set():
                yield "status", {"phase": "interrupted"}
                yield "err", {"code": "interrupted", "message": "turn interrupted"}
                return
            yield "err", {"code": "claude_error", "message": str(exc)[:500]}
        finally:
            proc = self._proc
            self._proc = None
            if proc and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass


class CodexNexusAdapter(BaseNexusAdapter):
    """Nexus-owned CodexAppServer instance (never the global singleton)."""

    agent = "codex"

    def __init__(self, workspace: Path, *, server_factory: Callable[..., Any] | None = None) -> None:
        super().__init__(workspace)
        self._server_factory = server_factory
        self._server = None
        self._active_codex_turn_id: Optional[str] = None
        self._thread_local = threading.local()

    def _get_server(self):
        if self._server is None:
            if self._server_factory:
                self._server = self._server_factory(cwd=str(self.workspace))
            else:
                from codex_app_server import CodexAppServer

                # Dedicated instance: workspace-write sandbox, no group-chat DB use.
                self._server = CodexAppServer(
                    cwd=str(self.workspace),
                    db_path=os.devnull,
                    sandbox="workspace-write",
                )
        return self._server

    def request_interrupt(self) -> None:
        super().request_interrupt()
        server = self._server
        turn_id = self._active_codex_turn_id
        if server is None:
            return
        # Best-effort turn cancel on the Nexus-owned server only.
        try:
            if turn_id and hasattr(server, "interrupt_turn"):
                server.interrupt_turn(turn_id)
            elif hasattr(server, "interrupt_active_turn"):
                server.interrupt_active_turn()
        except Exception:
            pass

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        self.clear_interrupt()
        yield "meta", {"phase": "start", "agent": "codex"}
        server = self._get_server()
        developer = (
            "You are the Nexus construction agent. Stay inside the current workspace. "
            "Do not push, merge, deploy, or leave the workspace."
        )
        try:
            stream = server.stream_bound_turn(
                self.session_id,
                developer,
                instruction,
                timeout=360,
                cancel_event=self._cancel if _supports_cancel(server.stream_bound_turn) else None,
            )
        except TypeError:
            stream = server.stream_bound_turn(
                self.session_id,
                developer,
                instruction,
                timeout=360,
            )

        try:
            for kind, payload in stream:
                if self._cancel.is_set():
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": "interrupted", "message": "turn interrupted"}
                    return
                if kind == "text":
                    yield "text", {"text": str(payload)}
                elif kind == "done":
                    meta = payload if isinstance(payload, dict) else {}
                    if meta.get("thread_id"):
                        self.session_id = str(meta["thread_id"])
                    if meta.get("turn_id"):
                        self._active_codex_turn_id = str(meta["turn_id"])
                    summary = git_summary(self.workspace)
                    yield "git", summary
                    yield "done", {"ok": True, "session_id": self.session_id}
                    return
                elif kind == "err":
                    yield "err", {"code": "codex_error", "message": str(payload)[:500]}
                    return
                else:
                    yield normalize_event_name(kind), redact_value(
                        payload if isinstance(payload, dict) else {"value": payload}
                    )
            if self._cancel.is_set():
                yield "status", {"phase": "interrupted"}
                yield "err", {"code": "interrupted", "message": "turn interrupted"}
                return
            summary = git_summary(self.workspace)
            yield "git", summary
            yield "done", {"ok": True, "session_id": self.session_id}
        except Exception as exc:
            if self._cancel.is_set():
                yield "status", {"phase": "interrupted"}
                yield "err", {"code": "interrupted", "message": "turn interrupted"}
                return
            yield "err", {"code": "codex_error", "message": str(exc)[:500]}


def _supports_cancel(fn: Callable[..., Any]) -> bool:
    try:
        import inspect

        return "cancel_event" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class FakeClaudeAdapter(BaseNexusAdapter):
    """Deterministic adapter for M5 verification without live credentials."""

    agent = "claude"

    def __init__(self, workspace: Path, *, fail: bool = False, hang: bool = False) -> None:
        super().__init__(workspace)
        self.fail = fail
        self.hang = hang
        self.turns = 0

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        self.clear_interrupt()
        self.turns += 1
        if not self.session_id:
            self.session_id = f"fake-claude-session-{id(self)}"
        yield "meta", {"phase": "start", "session_id": self.session_id, "turn": self.turns}
        yield "think", {"text": "planning edit"}
        target = self.workspace / "nexus_fixture.txt"
        if self.hang:
            for _ in range(40):
                if self._cancel.is_set():
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": "interrupted", "message": "turn interrupted"}
                    return
                time.sleep(0.02)
        if self.fail:
            yield "err", {"code": "fake_failure", "message": "injected failure"}
            return
        if self._cancel.is_set():
            yield "status", {"phase": "interrupted"}
            yield "err", {"code": "interrupted", "message": "turn interrupted"}
            return
        target.write_text(f"claude:{instruction[:200]}\n", encoding="utf-8")
        yield "tool_use", {"name": "write", "path": "nexus_fixture.txt"}
        yield "tool_result", {"ok": True, "path": "nexus_fixture.txt"}
        yield "text", {"text": f"updated nexus_fixture.txt (turn {self.turns})"}
        yield "git", git_summary(self.workspace)
        yield "done", {"ok": True, "session_id": self.session_id}


class FakeCodexAdapter(BaseNexusAdapter):
    agent = "codex"

    def __init__(self, workspace: Path, *, fail: bool = False, hang: bool = False) -> None:
        super().__init__(workspace)
        self.fail = fail
        self.hang = hang
        self.turns = 0

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        self.clear_interrupt()
        self.turns += 1
        if not self.session_id:
            self.session_id = f"fake-codex-session-{id(self)}"
        yield "meta", {"phase": "start", "session_id": self.session_id, "turn": self.turns}
        yield "think", {"text": "codex planning"}
        target = self.workspace / "nexus_fixture_codex.txt"
        if self.hang:
            for _ in range(40):
                if self._cancel.is_set():
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": "interrupted", "message": "turn interrupted"}
                    return
                time.sleep(0.02)
        if self.fail:
            yield "err", {"code": "fake_failure", "message": "injected failure"}
            return
        if self._cancel.is_set():
            yield "status", {"phase": "interrupted"}
            yield "err", {"code": "interrupted", "message": "turn interrupted"}
            return
        target.write_text(f"codex:{instruction[:200]}\n", encoding="utf-8")
        yield "tool_use", {"name": "write", "path": "nexus_fixture_codex.txt"}
        yield "tool_result", {"ok": True, "path": "nexus_fixture_codex.txt"}
        yield "text", {"text": f"updated nexus_fixture_codex.txt (turn {self.turns})"}
        # Unknown raw event → caller/runtime normalizes; emit status-shaped unknown.
        yield "status", {"raw_kind": "weird_provider_event", "token": "SECRET_TOKEN_VALUE"}
        yield "git", git_summary(self.workspace)
        yield "done", {"ok": True, "session_id": self.session_id}
