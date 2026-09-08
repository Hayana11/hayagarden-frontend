"""Nexus Claude / Codex adapters and test fakes.

Live adapters own Nexus-only processes/sessions. They never touch the formal
Claude resident or the global Codex app-server singleton.

Claude hard workspace confinement: the repository has no reusable mechanism
(beyond cwd / DAC that still allows writes outside the workspace root). Live
Claude therefore stays unavailable (ENVIRONMENT_BLOCKED) until a verifiable
confinement path exists. Test fakes may set hard_workspace_confinement=True.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from nexus_events import normalize_event_name, redact_value
from nexus_git import git_summary
from nexus_paths import NexusPathError, resolve_nexus_codex_home

EventTuple = tuple[str, dict[str, Any]]

# Verified: no bubblewrap/landlock/Claude --sandbox reuse path for hard root confinement.
CLAUDE_HARD_CONFINEMENT_AVAILABLE = False
# workspace-write limits outbound writes only — it is NOT read isolation.
CODEX_HARD_CONFINEMENT_AVAILABLE = False

CODEX_BLOCKED_DETAIL = (
    "ENVIRONMENT_BLOCKED: Codex requires an external NEXUS_CODEX_HOME, "
    "authentication in that home, and native permission-profile isolation"
)


class AdapterError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class BaseNexusAdapter:
    agent: str = ""
    hard_workspace_confinement: bool = False

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

    def begin_turn(self) -> None:
        """Reset cancel state when a turn is accepted — before the worker starts.

        Must not be called from stream_turn() after an interrupt may already be pending.
        """
        self._cancel.clear()

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        raise NotImplementedError


def _codex_supports_permission_profiles(binary: str, env: dict[str, str]) -> bool:
    """Recognize Codex 0.138+ or an explicit permission-profile CLI surface."""
    try:
        version = subprocess.run(
            [binary, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=6,
            env=env,
        )
        output = str(version.stdout or "")
        match = re.search(r"(?<!\d)(\d+)\.(\d+)(?:\.\d+)?", output)
        if version.returncode == 0 and match:
            major, minor = int(match.group(1)), int(match.group(2))
            if major > 0 or minor >= 138:
                return True
        if "permissionProfile/list" in output:
            return True
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    for args in (["sandbox", "--help"], ["app-server", "--help"]):
        try:
            probe = subprocess.run(
                [binary, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=6,
                env=env,
            )
            output = str(probe.stdout or "")
            if "permissionProfile/list" in output or re.search(
                r"(^|\s)-P(?:[,\s]|$)", output
            ):
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class ClaudeStreamNormalizer:
    """Stateful Claude stream-json normalizer.

    - Emits every content block from an assistant/user message (no early return).
    - When partial deltas were already streamed for a block index, skips the
      duplicate final full text/thinking on the completed assistant message.
    - Preserves encounter order: thinking → text → tool_use within a message;
      tool_result blocks from user messages in order.
    """

    def __init__(self) -> None:
        self._delta_text_indexes: set[int] = set()
        self._delta_think_indexes: set[int] = set()

    def feed(self, line: str) -> list[EventTuple]:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return [("status", {"raw_kind": "non_json"})]
        if not isinstance(d, dict):
            return [("status", {"raw_kind": "non_object"})]

        typ = d.get("type")
        if typ == "system" and d.get("subtype") == "init":
            sid = d.get("session_id") or (d.get("session") or {}).get("id")
            return [("meta", {"session_id": sid, "phase": "init"})]
        if typ == "rate_limit_event":
            return [("status", {"phase": "rate_limit"})]

        if typ == "stream_event":
            event = d.get("event") or {}
            et = event.get("type")
            idx = event.get("index")
            try:
                index = int(idx) if idx is not None else None
            except (TypeError, ValueError):
                index = None
            delta = event.get("delta") or {}
            if et == "content_block_delta":
                dt = delta.get("type")
                if dt == "text_delta":
                    if index is not None:
                        self._delta_text_indexes.add(index)
                    text = str(delta.get("text") or "")
                    return [("text", {"text": text})] if text else []
                if dt == "thinking_delta":
                    if index is not None:
                        self._delta_think_indexes.add(index)
                    think = str(delta.get("thinking") or delta.get("text") or "")
                    return [("think", {"text": think})] if think else []
            return [("status", {"raw_kind": et or "stream_event"})]

        if typ == "assistant":
            message = d.get("message") or {}
            out: list[EventTuple] = []
            for i, block in enumerate(message.get("content") or []):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "thinking":
                    if i in self._delta_think_indexes:
                        continue
                    think = str(block.get("thinking") or "")
                    if think:
                        out.append(("think", {"text": think}))
                elif btype == "text":
                    if i in self._delta_text_indexes:
                        continue
                    text = str(block.get("text") or "")
                    if text:
                        out.append(("text", {"text": text}))
                elif btype == "tool_use":
                    out.append(
                        (
                            "tool_use",
                            {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": redact_value(block.get("input") or {}),
                            },
                        )
                    )
            return out or [("status", {"raw_kind": "assistant"})]

        if typ == "user":
            message = d.get("message") or {}
            out = []
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    out.append(
                        (
                            "tool_result",
                            {
                                "tool_use_id": block.get("tool_use_id"),
                                "content": redact_value(block.get("content")),
                                "is_error": bool(block.get("is_error")),
                            },
                        )
                    )
            return out or [("status", {"raw_kind": "user"})]

        if typ == "result":
            if d.get("is_error"):
                return [
                    (
                        "err",
                        {
                            "code": "claude_result_error",
                            "message": str(d.get("result") or "error")[:500],
                        },
                    )
                ]
            sid = d.get("session_id")
            return [("meta", {"session_id": sid, "phase": "result"})]

        return [("status", {"raw_kind": str(typ or "unknown")})]


def _parse_claude_stream_line(line: str) -> list[EventTuple]:
    """Stateless helper used by unit tests; prefer ClaudeStreamNormalizer for live streams."""
    return ClaudeStreamNormalizer().feed(line)


class ClaudeNexusAdapter(BaseNexusAdapter):
    """Independent Claude Code process for Nexus only.

    Live launches are refused: hard_workspace_confinement is False until a
    reusable OS/provider confinement mechanism exists in this repository.
    """

    agent = "claude"
    hard_workspace_confinement = CLAUDE_HARD_CONFINEMENT_AVAILABLE

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
        if not self.hard_workspace_confinement:
            yield "err", {
                "code": "claude_unavailable",
                "message": (
                    "ENVIRONMENT_BLOCKED: Claude hard workspace confinement is unavailable; "
                    "refusing to launch"
                ),
            }
            return

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
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "TERM"):
            if key in os.environ:
                env[key] = os.environ[key]

        yield "meta", {"phase": "start", "agent": "claude"}
        normalizer = ClaudeStreamNormalizer()
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
                for event, data in normalizer.feed(line):
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
    """Nexus-owned CodexAppServer instance (never the global singleton).

    Availability requires a pre-existing external NEXUS_CODEX_HOME, auth in
    that same home, and a Codex release with native permission profiles.

    Ephemeral thread ids are process-local only: the same Nexus app-server
    process may resume the prior id; a restart must not recover from disk.
    """

    agent = "codex"
    hard_workspace_confinement = CODEX_HARD_CONFINEMENT_AVAILABLE

    def __init__(self, workspace: Path, *, server_factory: Callable[..., Any] | None = None) -> None:
        super().__init__(workspace)
        self._server_factory = server_factory
        self._server = None
        self._active_codex_turn_id: Optional[str] = None
        self._active_codex_thread_id: Optional[str] = None
        self._codex_home: Path | None = None
        self.availability_detail = CODEX_BLOCKED_DETAIL
        self.hard_workspace_confinement = False

        # Explicit factories are test/integration injection points. A subclass
        # must opt in to confinement; no live process readiness is claimed.
        if server_factory is not None:
            self.hard_workspace_confinement = bool(
                getattr(type(self), "hard_workspace_confinement", False)
            )
            return

        try:
            self._codex_home = resolve_nexus_codex_home(workspace)
        except NexusPathError as exc:
            self.availability_detail = exc.detail
            return

        from codex_app_server import CodexAppServer, find_codex

        binary = find_codex()
        if not binary:
            self.availability_detail = (
                "ENVIRONMENT_BLOCKED: Codex binary is not installed or executable"
            )
            return

        probe_server = CodexAppServer(
            cwd=str(self.workspace),
            db_path=os.devnull,
            sandbox="workspace-write",
            env_mode="nexus_allowlist",
            codex_home=str(self._codex_home),
            ephemeral_threads=True,
            service_name="hayagarden_nexus",
        )
        try:
            auth = probe_server._nexus_auth_status()
        except Exception as exc:
            self.availability_detail = (
                f"ENVIRONMENT_BLOCKED: Nexus CODEX_HOME auth probe failed: {exc}"
            )
            return
        if not auth.get("authenticated"):
            self.availability_detail = (
                "ENVIRONMENT_BLOCKED: Nexus CODEX_HOME is not authenticated"
            )
            return
        try:
            env = probe_server._environment()
        except Exception as exc:
            self.availability_detail = (
                f"ENVIRONMENT_BLOCKED: Nexus Codex environment is invalid: {exc}"
            )
            return
        if not _codex_supports_permission_profiles(binary, env):
            self.availability_detail = (
                "ENVIRONMENT_BLOCKED: installed Codex lacks native permission-profile "
                "isolation (requires 0.138+ or an equivalent profile API)"
            )
            return
        self.hard_workspace_confinement = True
        self.availability_detail = ""

    def begin_turn(self) -> None:
        # Clear previous turn ids at accept time; keep session_id for in-process
        # ephemeral continuity until ensure_provider_stopped / process death.
        super().begin_turn()
        self._active_codex_turn_id = None
        self._active_codex_thread_id = None

    def _get_server(self):
        if self._server is None:
            if self._server_factory:
                self._server = self._server_factory(cwd=str(self.workspace))
            else:
                from codex_app_server import CodexAppServer

                if self._codex_home is None:
                    raise AdapterError(
                        "codex_unavailable", self.availability_detail
                    )
                self._server = CodexAppServer(
                    cwd=str(self.workspace),
                    db_path=os.devnull,
                    sandbox="workspace-write",
                    env_mode="nexus_allowlist",
                    codex_home=str(self._codex_home),
                    ephemeral_threads=True,
                    service_name="hayagarden_nexus",
                )
        return self._server

    def request_interrupt(self) -> None:
        super().request_interrupt()
        server = self._server
        if server is None:
            return
        try:
            # Always interrupt the server's current active turn — never prefer
            # a stale adapter-cached id from a previous turn.
            if hasattr(server, "interrupt_active_turn"):
                server.interrupt_active_turn()
            elif hasattr(server, "interrupt_turn"):
                turn_id = getattr(server, "_active_turn_id", None)
                thread_id = getattr(server, "_active_thread_id", None)
                if turn_id:
                    server.interrupt_turn(turn_id, thread_id)
        except Exception:
            pass

    def ensure_provider_stopped(self) -> bool:
        """Confirm the Nexus app-server process tree stopped before forgetting it."""
        server = self._server
        if server is None:
            self.session_id = None
            self._active_codex_turn_id = None
            self._active_codex_thread_id = None
            return True
        confirmed = False
        try:
            if hasattr(server, "stop_process_tree"):
                confirmed = bool(server.stop_process_tree())
            elif hasattr(server, "stop"):
                confirmed = bool(server.stop())
            elif hasattr(server, "close"):
                confirmed = bool(server.close())
            elif hasattr(server, "_stop_locked"):
                lock = getattr(server, "_lock", None)
                if lock is not None:
                    with lock:
                        confirmed = bool(server._stop_locked())
                else:
                    confirmed = bool(server._stop_locked())
        except Exception:
            confirmed = False
        if confirmed:
            self._server = None
            self.session_id = None
            self._active_codex_turn_id = None
            self._active_codex_thread_id = None
        return confirmed

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        # Do NOT clear cancel here — begin_turn() runs at accept time only.
        if not self.hard_workspace_confinement:
            yield "err", {
                "code": "codex_unavailable",
                "message": self.availability_detail or CODEX_BLOCKED_DETAIL,
            }
            return

        yield "meta", {"phase": "start", "agent": "codex"}
        server = self._get_server()
        developer = (
            "You are the Nexus construction agent. Stay inside the current workspace. "
            "Do not push, merge, deploy, or leave the workspace."
        )
        # Resume ephemeral thread id within the same app-server process only.
        resume_thread_id = self.session_id
        try:
            stream = server.stream_bound_turn(
                resume_thread_id,
                developer,
                instruction,
                timeout=360,
                cancel_event=self._cancel if _supports_cancel(server.stream_bound_turn) else None,
            )
        except TypeError:
            stream = server.stream_bound_turn(
                resume_thread_id,
                developer,
                instruction,
                timeout=360,
            )

        try:
            for kind, payload in stream:
                if (
                    kind == "meta"
                    and isinstance(payload, dict)
                    and payload.get("phase") == "turn_started"
                ):
                    # Sync active ids immediately after provider turn/start.
                    if payload.get("thread_id"):
                        self._active_codex_thread_id = str(payload["thread_id"])
                        self.session_id = str(payload["thread_id"])
                    if payload.get("turn_id"):
                        self._active_codex_turn_id = str(payload["turn_id"])
                    yield "meta", payload
                    continue
                if kind == "text":
                    if self._cancel.is_set():
                        continue
                    yield "text", {"text": str(payload)}
                elif kind == "done":
                    meta = payload if isinstance(payload, dict) else {}
                    if meta.get("thread_id"):
                        self._active_codex_thread_id = str(meta["thread_id"])
                        self.session_id = str(meta["thread_id"])
                    if meta.get("turn_id"):
                        self._active_codex_turn_id = str(meta["turn_id"])
                    summary = git_summary(self.workspace)
                    yield "git", summary
                    yield "done", {"ok": True, "session_id": self.session_id}
                    return
                elif kind == "err":
                    data = payload if isinstance(payload, dict) else {"message": str(payload)[:500]}
                    code = str(data.get("code") or "codex_error")
                    yield "err", {
                        "code": code,
                        "message": str(data.get("message") or payload)[:500],
                    }
                    return
                else:
                    yield normalize_event_name(kind), redact_value(
                        payload if isinstance(payload, dict) else {"value": payload}
                    )
            if self._cancel.is_set():
                yield "err", {
                    "code": "interrupt_unconfirmed",
                    "message": "cancel set but provider did not confirm interrupted",
                }
                return
            summary = git_summary(self.workspace)
            yield "git", summary
            yield "done", {"ok": True, "session_id": self.session_id}
        except Exception as exc:
            if self._cancel.is_set():
                yield "err", {
                    "code": "interrupt_unconfirmed",
                    "message": str(exc)[:500],
                }
                return
            yield "err", {"code": "codex_error", "message": str(exc)[:500]}


def _supports_cancel(fn: Callable[..., Any]) -> bool:
    try:
        import inspect

        return "cancel_event" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


class FakeClaudeAdapter(BaseNexusAdapter):
    """Deterministic adapter for verification without live credentials."""

    agent = "claude"
    # Test double only — does not claim production hard confinement exists.
    hard_workspace_confinement = True

    def __init__(self, workspace: Path, *, fail: bool = False, hang: bool = False) -> None:
        super().__init__(workspace)
        self.fail = fail
        self.hang = hang
        self.turns = 0
        self.emit_after_interrupt: list[EventTuple] = []
        self.interrupt_err_code: Optional[str] = None
        self.ensure_stopped_calls = 0
        self.stop_confirmed = True

    def ensure_provider_stopped(self) -> bool:
        self.ensure_stopped_calls += 1
        return self.stop_confirmed

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        # Do NOT clear cancel here — begin_turn() runs at accept time.
        self.turns += 1
        if not self.session_id:
            self.session_id = f"fake-claude-session-{id(self)}"
        yield "meta", {"phase": "start", "session_id": self.session_id, "turn": self.turns}
        yield "think", {"text": "planning edit"}
        target = self.workspace / "nexus_fixture.txt"
        if self.hang:
            for _ in range(40):
                if self._cancel.is_set():
                    # Optionally emit competitive content then done — runtime must drop/convert.
                    for item in self.emit_after_interrupt:
                        yield item
                    code = self.interrupt_err_code or "interrupted"
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": code, "message": f"turn {code}"}
                    return
                time.sleep(0.02)
        if self.fail:
            yield "err", {"code": "fake_failure", "message": "injected failure"}
            return
        if self._cancel.is_set():
            for item in self.emit_after_interrupt:
                yield item
            code = self.interrupt_err_code or "interrupted"
            yield "status", {"phase": "interrupted"}
            yield "err", {"code": code, "message": f"turn {code}"}
            return
        target.write_text(f"claude:{instruction[:200]}\n", encoding="utf-8")
        yield "tool_use", {"name": "write", "path": "nexus_fixture.txt"}
        yield "tool_result", {"ok": True, "path": "nexus_fixture.txt"}
        yield "text", {"text": f"updated nexus_fixture.txt (turn {self.turns})"}
        yield "git", git_summary(self.workspace)
        yield "done", {"ok": True, "session_id": self.session_id}


class FakeCodexAdapter(BaseNexusAdapter):
    agent = "codex"
    hard_workspace_confinement = True

    def __init__(self, workspace: Path, *, fail: bool = False, hang: bool = False) -> None:
        super().__init__(workspace)
        self.fail = fail
        self.hang = hang
        self.turns = 0
        self.block_interrupt = threading.Event()
        self.interrupt_entered = threading.Event()
        self._interrupt_delay = 0.0
        self.interrupt_err_code: Optional[str] = None
        self.ensure_stopped_calls = 0
        self.stop_confirmed = True

    def request_interrupt(self) -> None:
        self.interrupt_entered.set()
        if self._interrupt_delay:
            time.sleep(self._interrupt_delay)
        if self.block_interrupt.is_set():
            # Wait until test clears the block — used to prove runtime lock is released.
            while self.block_interrupt.is_set():
                time.sleep(0.01)
        super().request_interrupt()

    def ensure_provider_stopped(self) -> bool:
        self.ensure_stopped_calls += 1
        return self.stop_confirmed

    def stream_turn(self, instruction: str) -> Iterator[EventTuple]:
        # Do NOT clear cancel here — begin_turn() runs at accept time.
        self.turns += 1
        if not self.session_id:
            self.session_id = f"fake-codex-session-{id(self)}"
        yield "meta", {"phase": "start", "session_id": self.session_id, "turn": self.turns}
        yield "think", {"text": "codex planning"}
        target = self.workspace / "nexus_fixture_codex.txt"
        if self.hang:
            for _ in range(40):
                if self._cancel.is_set():
                    code = self.interrupt_err_code or "interrupted"
                    yield "status", {"phase": "interrupted"}
                    yield "err", {"code": code, "message": f"turn {code}"}
                    return
                time.sleep(0.02)
        if self.fail:
            yield "err", {"code": "fake_failure", "message": "injected failure"}
            return
        if self._cancel.is_set():
            code = self.interrupt_err_code or "interrupted"
            yield "status", {"phase": "interrupted"}
            yield "err", {"code": code, "message": f"turn {code}"}
            return
        target.write_text(f"codex:{instruction[:200]}\n", encoding="utf-8")
        yield "tool_use", {"name": "write", "path": "nexus_fixture_codex.txt"}
        yield "tool_result", {"ok": True, "path": "nexus_fixture_codex.txt"}
        yield "text", {"text": f"updated nexus_fixture_codex.txt (turn {self.turns})"}
        yield "status", {"raw_kind": "weird_provider_event", "token": "SECRET_TOKEN_VALUE"}
        yield "git", git_summary(self.workspace)
        yield "done", {"ok": True, "session_id": self.session_id}
